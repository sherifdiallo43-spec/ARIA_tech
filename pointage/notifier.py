"""Alertes WhatsApp aux parents, avec file d'attente.

Deux étapes bien séparées :
1. creer_notification : à chaque pointage, on décide s'il faut prévenir le
   parent et on range le message dans la table `notifications`.
   creer_alertes_absence : une fois par jour, après `absence_apres`, on range
   une alerte pour chaque élève qui n'a pas pointé.
2. envoyer_en_attente : on envoie ce qui attend. Sans internet, les messages
   restent en attente et partent au cycle suivant. Rien n'est perdu.

Ce qui est envoyé dépend de `types_notifies` dans config.toml :
- "arrivee", "sortie" : à chaque passage (formule Complet) ;
- "retard"            : seulement les arrivées en retard ;
- "absence"           : les élèves qui n'ont pas pointé à `absence_apres`.
La formule Essentiel = ["retard", "absence"].
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from .config import Config, lire_heure
from .db import dt_texte, ecrire_etat, lire_etat, texte_dt
from .subscriptions import alertes_autorisees

log = logging.getLogger(__name__)

EVENEMENTS = {
    "arrivee": "est arrivé(e) à l'école",
    "sortie": "a quitté l'école",
    "absence": "n'avait pas pointé à l'école",
}

# Fraîcheur exigée de la dernière lecture COMPLÈTE des pointeuses avant d'annoncer
# une absence : sinon, un élève présent mais pas encore lu passerait pour absent.
LECTURE_RECENTE = timedelta(minutes=5)


class ErreurTemporaire(Exception):
    """Envoi impossible pour l'instant (pas d'internet, serveur saturé) : on réessaiera."""


class ErreurDefinitive(Exception):
    """Envoi impossible pour de bon (numéro invalide, compte bloqué) : inutile de réessayer."""


# --- Étape 1 : préparer --------------------------------------------------------

def composer_message(eleve: sqlite3.Row, type_: str, horodatage: datetime, en_retard: bool,
                     nom_ecole: str) -> tuple[str, dict]:
    """Renvoie le texte du message et les variables pour un modèle Twilio."""
    heure = horodatage.strftime("%Hh%M")
    evenement = EVENEMENTS[type_] + (" (en retard)" if en_retard else "")
    eleve_txt = f"{eleve['prenom']} {eleve['nom']}"
    message = f"{nom_ecole} : {eleve_txt} ({eleve['classe']}) {evenement} à {heure}."
    variables = {"1": nom_ecole, "2": eleve_txt, "3": evenement, "4": heure}
    return message, variables


def _deja_notifie_ce_jour(conn: sqlite3.Connection, zk_user_id: str, type_: str,
                          horodatage: datetime) -> bool:
    """Au plus une alerte par élève, par type et par jour (économise les messages payants)."""
    return conn.execute(
        "SELECT 1 FROM notifications WHERE zk_user_id = ? AND type = ? AND substr(moment, 1, 10) = ?",
        (zk_user_id, type_, horodatage.date().isoformat()),
    ).fetchone() is not None


def _age_max(config: Config) -> timedelta:
    return timedelta(minutes=config.notifications["age_max_minutes"])


def _trop_ancien(horodatage: datetime, maintenant: datetime, config: Config) -> bool:
    return maintenant - horodatage > _age_max(config)


def _ranger(conn: sqlite3.Connection, eleve: sqlite3.Row, pointage_id: int | None, type_: str,
            moment: datetime, en_retard: bool, config: Config, maintenant: datetime) -> str:
    """Range une alerte dans la file et renvoie son statut. L'appelant fait le commit.

    On garde aussi une trace des alertes NON envoyées : le rapport mensuel
    montre ainsi combien de parents ont raté des alertes faute d'abonnement.
    """
    if not alertes_autorisees(eleve, moment.date(), config.abonnement):
        statut = "ignore_abonnement"
    elif _trop_ancien(moment, maintenant, config):
        statut = "ignore_trop_ancien"
    else:
        statut = "en_attente"
    message, variables = composer_message(eleve, type_, moment, en_retard, config.nom_ecole)
    conn.execute(
        "INSERT INTO notifications (pointage_id, zk_user_id, moment, type, tel, message, variables, "
        "  statut, cree_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pointage_id, eleve["zk_user_id"], dt_texte(moment), type_, eleve["tel_parent"], message,
         json.dumps(variables, ensure_ascii=False), statut, dt_texte(maintenant)),
    )
    return statut


def creer_notification(conn: sqlite3.Connection, pointage_id: int, zk_user_id: str,
                       horodatage: datetime, type_: str, en_retard: bool, config: Config,
                       maintenant: datetime) -> str | None:
    """Range l'alerte d'un pointage dans la file. Renvoie son statut, ou None si
    aucune alerte n'est prévue. L'appelant fait le commit."""
    types = config.notifications["types_notifies"]
    # "retard" : seules les arrivées en retard sont annoncées (formule Essentiel).
    if not (type_ in types or (type_ == "arrivee" and en_retard and "retard" in types)):
        return None
    eleve = conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if eleve is None or not eleve["actif_scolarite"]:
        return None  # empreinte d'un adulte, ou pas encore associée à un élève
    if _deja_notifie_ce_jour(conn, zk_user_id, type_, horodatage):
        return None
    return _ranger(conn, eleve, pointage_id, type_, horodatage, en_retard, config, maintenant)


def creer_alertes_absence(conn: sqlite3.Connection, config: Config,
                          maintenant: datetime | None = None) -> int:
    """Range une alerte pour chaque élève qui n'a pas pointé à `absence_apres`.

    Annoncer à tort une absence à des centaines de parents serait grave. On ne
    le fait donc que si tout indique une vraie journée de classe, bien lue :
    - du lundi au samedi, entre `absence_apres` et `absence_apres` + age_max_minutes ;
    - toutes les pointeuses lues il y a moins de 5 minutes (une pointeuse en panne
      ferait passer pour absents tous les élèves qui y ont pointé) ;
    - au moins `absence_min_presents_pourcent` des élèves présents (sinon : jour
      férié, grève, coupure de courant au portail... on ne dit rien).
    Renvoie le nombre d'alertes rangées. Rappelée toutes les 30 s, elle ne crée
    jamais deux fois la même alerte.
    """
    reglages = config.notifications
    if "absence" not in reglages["types_notifies"]:
        return 0
    maintenant = maintenant or datetime.now()
    jour = maintenant.date()
    constat = datetime.combine(jour, lire_heure(reglages.get("absence_apres", "09:00")))
    if jour.weekday() == 6 or not constat <= maintenant < constat + _age_max(config):
        return 0

    lecture = lire_etat(conn, "derniere_lecture_complete")
    if not lecture or maintenant - texte_dt(lecture) > LECTURE_RECENTE:
        _avertir_une_fois(conn, jour, "pointeuses pas toutes lues récemment")
        return 0

    eleves = conn.execute("SELECT * FROM eleves WHERE actif_scolarite = 1 AND date_inscription <= ?",
                          (jour.isoformat(),)).fetchall()
    ont_pointe = {r[0] for r in conn.execute(
        "SELECT DISTINCT zk_user_id FROM pointages WHERE substr(horodatage, 1, 10) = ?",
        (jour.isoformat(),))}
    absents = [e for e in eleves if e["zk_user_id"] not in ont_pointe]
    seuil = int(reglages.get("absence_min_presents_pourcent", 50))
    if not eleves or (len(eleves) - len(absents)) * 100 < seuil * len(eleves):
        _avertir_une_fois(conn, jour, f"moins de {seuil} % des élèves ont pointé")
        return 0

    ranges = 0
    for e in absents:
        if not _deja_notifie_ce_jour(conn, e["zk_user_id"], "absence", constat):
            _ranger(conn, e, None, "absence", constat, False, config, maintenant)
            ranges += 1
    conn.commit()
    if ranges:
        log.info("%d alerte(s) d'absence préparée(s).", ranges)
    return ranges


def _avertir_une_fois(conn: sqlite3.Connection, jour, raison: str) -> None:
    """Un seul avertissement par jour dans le journal, pas un toutes les 30 s."""
    if lire_etat(conn, "absences_bloquees_le") != jour.isoformat():
        log.warning("Alertes d'absence NON envoyées aujourd'hui pour l'instant : %s.", raison)
        ecrire_etat(conn, "absences_bloquees_le", jour.isoformat())
        conn.commit()


# --- Étape 2 : envoyer ---------------------------------------------------------

class EnvoyeurConsole:
    """Affiche les messages au lieu de les envoyer (démos, tests, mise au point)."""

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        print(f"[WhatsApp -> {tel}] {message}", flush=True)


def _creer_message_twilio(client, **parametres) -> None:
    """Envoie via Twilio et classe l'erreur : à réessayer plus tard, ou définitive."""
    from twilio.base.exceptions import TwilioRestException

    try:
        client.messages.create(**parametres)
    except TwilioRestException as e:
        # 4xx = la requête elle-même est mauvaise (numéro invalide...), sauf
        # 429 (trop de requêtes) qui se règle en attendant.
        if 400 <= e.status < 500 and e.status != 429:
            raise ErreurDefinitive(f"Twilio {e.code} : {e.msg}") from e
        raise ErreurTemporaire(f"Twilio {e.code} : {e.msg}") from e
    except Exception as e:  # coupure réseau, DNS, délai dépassé...
        raise ErreurTemporaire(str(e)) from e


def _client_twilio(account_sid: str, auth_token: str, api_key: str = "", api_secret: str = ""):
    """Client Twilio, de préférence avec une clé d'API propre à l'école.

    Le jeton principal (auth token) donne tout le compte : envois, historique
    des messages de TOUTES les écoles, facturation. Une clé d'API par école se
    révoque seule si le PC de cette école est compromis, sans toucher aux autres.
    """
    from twilio.rest import Client

    if api_key and api_secret:
        return Client(api_key, api_secret, account_sid)
    return Client(account_sid, auth_token)


class EnvoyeurTwilioSMS:
    """Envoi par SMS via Twilio (texte libre, aucun modèle à faire approuver)."""

    def __init__(self, account_sid: str, auth_token: str, expediteur: str,
                 api_key: str = "", api_secret: str = ""):
        self.client = _client_twilio(account_sid, auth_token, api_key, api_secret)
        self.expediteur = expediteur

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        _creer_message_twilio(self.client, from_=self.expediteur, to=tel, body=message)


class EnvoyeurTwilio:
    """Envoi réel par WhatsApp via Twilio."""

    def __init__(self, account_sid: str, auth_token: str, expediteur: str, content_sid: str = "",
                 api_key: str = "", api_secret: str = ""):
        self.client = _client_twilio(account_sid, auth_token, api_key, api_secret)
        self.expediteur = expediteur if expediteur.startswith("whatsapp:") else f"whatsapp:{expediteur}"
        self.content_sid = content_sid

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        parametres = {"from_": self.expediteur, "to": f"whatsapp:{tel}"}
        if self.content_sid:
            # Production : modèle de message approuvé par Meta, avec ses variables.
            parametres["content_sid"] = self.content_sid
            parametres["content_variables"] = json.dumps(variables, ensure_ascii=False)
        else:
            # Bac à sable : texte libre.
            parametres["body"] = message
        _creer_message_twilio(self.client, **parametres)


def creer_envoyeur(config: Config):
    mode = config.notifications["mode"]
    if mode == "console":
        return EnvoyeurConsole()
    if mode in ("twilio", "twilio_sms"):
        s = config.secrets
        expediteur = "TWILIO_WHATSAPP_FROM" if mode == "twilio" else "TWILIO_SMS_FROM"
        manquants = [c for c in ("TWILIO_ACCOUNT_SID", expediteur) if not s.get(c)]
        avec_cle = s.get("TWILIO_API_KEY_SID") and s.get("TWILIO_API_KEY_SECRET")
        if not avec_cle and not s.get("TWILIO_AUTH_TOKEN"):
            manquants.append("TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET (ou TWILIO_AUTH_TOKEN)")
        if manquants:
            raise ValueError(f"Mode {mode} : à remplir dans .env : {', '.join(manquants)}")
        identifiants = {"api_key": s.get("TWILIO_API_KEY_SID", ""),
                        "api_secret": s.get("TWILIO_API_KEY_SECRET", "")}
        if mode == "twilio":
            return EnvoyeurTwilio(s["TWILIO_ACCOUNT_SID"], s.get("TWILIO_AUTH_TOKEN", ""),
                                  s[expediteur], s.get("TWILIO_CONTENT_SID", ""), **identifiants)
        return EnvoyeurTwilioSMS(s["TWILIO_ACCOUNT_SID"], s.get("TWILIO_AUTH_TOKEN", ""),
                                 s[expediteur], **identifiants)
    raise ValueError(f"Mode de notification inconnu : {mode!r} (attendu : 'console', 'twilio' ou 'twilio_sms')")


def envoyer_en_attente(conn: sqlite3.Connection, envoyeur, config: Config,
                       maintenant: datetime | None = None) -> dict[str, int]:
    """Envoie les messages en attente, du plus ancien au plus récent."""
    maintenant = maintenant or datetime.now()
    max_tentatives = config.notifications["max_tentatives"]
    compte = {"envoye": 0, "echec": 0, "ignore_trop_ancien": 0, "reporte": 0}

    en_attente = conn.execute(
        "SELECT id, tel, message, variables, tentatives, moment "
        "FROM notifications WHERE statut = 'en_attente' ORDER BY id"
    ).fetchall()

    for notif in en_attente:
        if _trop_ancien(texte_dt(notif["moment"]), maintenant, config):
            # Coupure trop longue : "arrivé à 7h30" reçu à 15h inquiéterait plus qu'il n'aide.
            conn.execute("UPDATE notifications SET statut = 'ignore_trop_ancien' WHERE id = ?",
                         (notif["id"],))
            conn.commit()
            compte["ignore_trop_ancien"] += 1
            continue

        tentatives = notif["tentatives"] + 1
        try:
            envoyeur.envoyer(notif["tel"], notif["message"], json.loads(notif["variables"]))
        except ErreurDefinitive as e:
            conn.execute("UPDATE notifications SET statut = 'echec', tentatives = ?, erreur = ? WHERE id = ?",
                         (tentatives, str(e), notif["id"]))
            conn.commit()
            compte["echec"] += 1
            log.error("Message %d abandonné : %s", notif["id"], e)
        except ErreurTemporaire as e:
            statut = "echec" if tentatives >= max_tentatives else "en_attente"
            conn.execute("UPDATE notifications SET statut = ?, tentatives = ?, erreur = ? WHERE id = ?",
                         (statut, tentatives, str(e), notif["id"]))
            conn.commit()
            compte["echec" if statut == "echec" else "reporte"] += 1
            log.warning("Envoi impossible pour l'instant (%s). Nouvel essai au prochain cycle.", e)
            break  # probablement pas d'internet : inutile d'essayer les suivants maintenant
        else:
            conn.execute("UPDATE notifications SET statut = 'envoye', tentatives = ?, envoye_le = ? WHERE id = ?",
                         (tentatives, dt_texte(maintenant), notif["id"]))
            conn.commit()  # commit après CHAQUE envoi : un plantage ne fera jamais envoyer 2 fois
            compte["envoye"] += 1
    return compte
