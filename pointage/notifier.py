"""Alertes WhatsApp aux parents, avec file d'attente.

Deux étapes bien séparées :
1. creer_notification : à chaque pointage, on décide s'il faut prévenir le
   parent et on range le message dans la table `notifications`.
2. envoyer_en_attente : on envoie ce qui attend. Sans internet, les messages
   restent en attente et partent au cycle suivant. Rien n'est perdu.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from .config import Config
from .db import dt_texte, texte_dt
from .subscriptions import abonnement_actif

log = logging.getLogger(__name__)

EVENEMENTS = {
    "arrivee": "est arrivé(e) à l'école",
    "sortie": "a quitté l'école",
}


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
        "SELECT 1 FROM notifications n JOIN pointages p ON p.id = n.pointage_id "
        "WHERE p.zk_user_id = ? AND p.type = ? AND substr(p.horodatage, 1, 10) = ?",
        (zk_user_id, type_, horodatage.date().isoformat()),
    ).fetchone() is not None


def _trop_ancien(horodatage: datetime, maintenant: datetime, config: Config) -> bool:
    return maintenant - horodatage > timedelta(minutes=config.notifications["age_max_minutes"])


def creer_notification(conn: sqlite3.Connection, pointage_id: int, zk_user_id: str,
                       horodatage: datetime, type_: str, en_retard: bool, config: Config,
                       maintenant: datetime) -> str | None:
    """Range l'alerte d'un pointage dans la file. Renvoie son statut, ou None si
    aucune alerte n'est prévue. L'appelant fait le commit."""
    if type_ not in config.notifications["types_notifies"]:
        return None
    eleve = conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if eleve is None or not eleve["actif_scolarite"]:
        return None  # empreinte d'un adulte, ou pas encore associée à un élève
    if _deja_notifie_ce_jour(conn, zk_user_id, type_, horodatage):
        return None

    # On garde aussi une trace des alertes NON envoyées : le rapport mensuel
    # montre ainsi combien de parents ont raté des alertes faute d'abonnement.
    if not abonnement_actif(eleve, horodatage.date()):
        statut = "ignore_abonnement"
    elif _trop_ancien(horodatage, maintenant, config):
        statut = "ignore_trop_ancien"
    else:
        statut = "en_attente"

    message, variables = composer_message(eleve, type_, horodatage, en_retard, config.nom_ecole)
    conn.execute(
        "INSERT INTO notifications (pointage_id, tel, message, variables, statut, cree_le) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pointage_id, eleve["tel_parent"], message, json.dumps(variables, ensure_ascii=False),
         statut, dt_texte(maintenant)),
    )
    return statut


# --- Étape 2 : envoyer ---------------------------------------------------------

class EnvoyeurConsole:
    """Affiche les messages au lieu de les envoyer (démos, tests, mise au point)."""

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        print(f"[WhatsApp -> {tel}] {message}", flush=True)


class EnvoyeurTwilio:
    """Envoi réel par WhatsApp via Twilio."""

    def __init__(self, account_sid: str, auth_token: str, expediteur: str, content_sid: str = ""):
        from twilio.rest import Client

        self.client = Client(account_sid, auth_token)
        self.expediteur = expediteur if expediteur.startswith("whatsapp:") else f"whatsapp:{expediteur}"
        self.content_sid = content_sid

    def envoyer(self, tel: str, message: str, variables: dict) -> None:
        from twilio.base.exceptions import TwilioRestException

        parametres = {"from_": self.expediteur, "to": f"whatsapp:{tel}"}
        if self.content_sid:
            # Production : modèle de message approuvé par Meta, avec ses variables.
            parametres["content_sid"] = self.content_sid
            parametres["content_variables"] = json.dumps(variables, ensure_ascii=False)
        else:
            # Bac à sable : texte libre.
            parametres["body"] = message
        try:
            self.client.messages.create(**parametres)
        except TwilioRestException as e:
            # 4xx = la requête elle-même est mauvaise (numéro invalide...), sauf
            # 429 (trop de requêtes) qui se règle en attendant.
            if 400 <= e.status < 500 and e.status != 429:
                raise ErreurDefinitive(f"Twilio {e.code} : {e.msg}") from e
            raise ErreurTemporaire(f"Twilio {e.code} : {e.msg}") from e
        except Exception as e:  # coupure réseau, DNS, délai dépassé...
            raise ErreurTemporaire(str(e)) from e


def creer_envoyeur(config: Config):
    mode = config.notifications["mode"]
    if mode == "console":
        return EnvoyeurConsole()
    if mode == "twilio":
        s = config.secrets
        manquants = [c for c in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
                     if not s.get(c)]
        if manquants:
            raise ValueError(f"Mode twilio : à remplir dans .env : {', '.join(manquants)}")
        return EnvoyeurTwilio(s["TWILIO_ACCOUNT_SID"], s["TWILIO_AUTH_TOKEN"],
                              s["TWILIO_WHATSAPP_FROM"], s.get("TWILIO_CONTENT_SID", ""))
    raise ValueError(f"Mode de notification inconnu : {mode!r} (attendu : 'console' ou 'twilio')")


def envoyer_en_attente(conn: sqlite3.Connection, envoyeur, config: Config,
                       maintenant: datetime | None = None) -> dict[str, int]:
    """Envoie les messages en attente, du plus ancien au plus récent."""
    maintenant = maintenant or datetime.now()
    max_tentatives = config.notifications["max_tentatives"]
    compte = {"envoye": 0, "echec": 0, "ignore_trop_ancien": 0, "reporte": 0}

    en_attente = conn.execute(
        "SELECT n.id, n.tel, n.message, n.variables, n.tentatives, p.horodatage "
        "FROM notifications n JOIN pointages p ON p.id = n.pointage_id "
        "WHERE n.statut = 'en_attente' ORDER BY n.id"
    ).fetchall()

    for notif in en_attente:
        if _trop_ancien(texte_dt(notif["horodatage"]), maintenant, config):
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
