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
import urllib.error
import urllib.request
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
    """Renvoie le texte du message et les variables du modèle WhatsApp.

    Meta n'accepte qu'un modèle au texte fixe, approuvé à l'avance : un modèle par
    type d'alerte ("modele"), avec 3 variables : école, élève et classe, heure.
    """
    heure = horodatage.strftime("%Hh%M")
    evenement = EVENEMENTS[type_] + (" (en retard)" if en_retard else "")
    eleve_txt = f"{eleve['prenom']} {eleve['nom']} ({eleve['classe']})"
    message = f"{nom_ecole} : {eleve_txt} {evenement} à {heure}."
    modele = "retard" if type_ == "arrivee" and en_retard else type_
    variables = {"modele": modele, "1": nom_ecole, "2": eleve_txt, "3": heure}
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


# Version de l'API Graph de Meta. Chaque version reste utilisable environ 2 ans ;
# META_API_VERSION dans .env permet d'en changer sans toucher au code.
VERSION_API_META = "v25.0"

# Noms des modèles approuvés dans WhatsApp Manager, si config.toml n'en donne pas.
MODELES_META = {"arrivee": "aria_arrivee", "retard": "aria_retard",
                "sortie": "aria_sortie", "absence": "aria_absence"}

# Erreurs Meta qui se règlent en attendant : panne passagère, limites de débit, et
# jeton expiré (les messages restent en file le temps de le renouveler dans .env).
CODES_META_TEMPORAIRES = {1, 2, 4, 17, 190, 80007, 130429, 131000, 131016, 131048, 131056, 133016}


def _erreur_meta(statut_http: int, corps: bytes) -> Exception:
    """Classe une réponse d'erreur de Meta : à réessayer plus tard, ou définitive."""
    try:
        erreur = json.loads(corps.decode("utf-8")).get("error") or {}
        code = erreur.get("code")
        detail = (erreur.get("error_data") or {}).get("details") or erreur.get("message")
    except (ValueError, UnicodeDecodeError, AttributeError):
        code, detail = None, None
    texte = f"Meta {code or statut_http} : {detail or f'HTTP {statut_http}'}"
    if statut_http >= 500 or statut_http == 429 or code in CODES_META_TEMPORAIRES:
        return ErreurTemporaire(texte)
    # 4xx : la requête elle-même est mauvaise (numéro invalide, modèle inconnu...).
    return ErreurDefinitive(texte)


class EnvoyeurMeta:
    """Envoi réel par WhatsApp, directement par l'API Cloud de Meta (sans intermédiaire).

    WhatsApp interdit le texte libre à quelqu'un qui n'a pas écrit au numéro dans les
    dernières 24 h : en production, chaque alerte part avec son modèle approuvé.
    `texte_libre` sert aux démos (le téléphone de démo écrit d'abord au numéro).
    "Envoyé" veut dire "accepté par Meta", pas "lu" : la lecture n'est pas suivie.
    """

    def __init__(self, jeton: str, id_numero: str, modeles: dict | None = None, langue: str = "fr",
                 texte_libre: bool = False, version: str = VERSION_API_META, delai_s: int = 15):
        self.jeton = jeton
        self.url = f"https://graph.facebook.com/{version}/{id_numero}/messages"
        self.modeles = modeles or MODELES_META
        self.langue = langue
        self.texte_libre = texte_libre
        self.delai_s = delai_s

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        if self.texte_libre:
            self._poster(tel, {"type": "text", "text": {"body": message}})
            return
        nom = self.modeles.get(variables.get("modele", ""))
        if not nom:
            raise ErreurDefinitive(f"aucun modèle Meta pour l'alerte {variables.get('modele')!r} "
                                   "([notifications.modeles_meta] dans config.toml)")
        self.envoyer_modele(tel, nom, self.langue, [variables["1"], variables["2"], variables["3"]])

    def envoyer_modele(self, tel: str, nom: str, langue: str, parametres: list[str] = ()) -> None:
        modele = {"name": nom, "language": {"code": langue}}
        if parametres:
            modele["components"] = [{"type": "body", "parameters": [
                {"type": "text", "text": p} for p in parametres]}]
        self._poster(tel, {"type": "template", "template": modele})

    def _poster(self, tel: str, contenu: dict) -> None:
        corps = {"messaging_product": "whatsapp", "to": tel.lstrip("+"), **contenu}
        requete = urllib.request.Request(
            self.url, data=json.dumps(corps, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.jeton}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(requete, timeout=self.delai_s) as reponse:
                reponse.read()
        except urllib.error.HTTPError as e:  # avant URLError, dont elle hérite
            raise _erreur_meta(e.code, e.read()) from e
        except OSError as e:  # pas d'internet, DNS, délai dépassé (URLError en fait partie)
            raise ErreurTemporaire(f"réseau : {e}") from e


def creer_envoyeur(config: Config):
    reglages = config.notifications
    mode = reglages["mode"]
    if mode == "console":
        return EnvoyeurConsole()
    if mode == "meta":
        s = config.secrets
        manquants = [c for c in ("META_JETON", "META_ID_NUMERO") if not s.get(c)]
        if manquants:
            raise ValueError(f"Mode meta : à remplir dans .env : {', '.join(manquants)}")
        return EnvoyeurMeta(s["META_JETON"], s["META_ID_NUMERO"],
                            {**MODELES_META, **reglages.get("modeles_meta", {})},
                            langue=reglages.get("langue_meta", "fr"),
                            texte_libre=bool(reglages.get("texte_libre", False)),
                            version=s.get("META_API_VERSION") or VERSION_API_META)
    conseil = " Twilio n'est plus pris en charge : mets mode = \"meta\"." if mode.startswith("twilio") else ""
    raise ValueError(f"Mode de notification inconnu : {mode!r} (attendu : 'console' ou 'meta').{conseil}")


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
