"""Envoi des rapports du personnel au directeur, par email.

Même principe que les messages WhatsApp : un mode "console" pour les démos
(rien ne part, tout s'affiche) et un mode "smtp" pour l'envoi réel.

File d'attente sans table dédiée : chaque PDF envoyé est noté dans la table
`etat` (clé "email:<nom du fichier>"). Le service repasse toutes les heures :
un PDF non envoyé (pas d'internet) part au passage suivant, et un PDF déjà
envoyé ne repart jamais.
"""
from __future__ import annotations

import logging
import smtplib
import sqlite3
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .config import Config
from .db import dt_texte, ecrire_etat, lire_etat

log = logging.getLogger(__name__)


class EnvoyeurConsole:
    """Affiche l'email au lieu de l'envoyer (démos, tests)."""

    def envoyer(self, destinataire: str, sujet: str, corps: str, piece_jointe: Path | None = None) -> None:
        jointe = f" [pièce jointe : {piece_jointe.name}]" if piece_jointe else ""
        print(f"[Email -> {destinataire}] {sujet}{jointe}", flush=True)


class EnvoyeurSMTP:
    """Envoi réel. Avec Gmail : mot de passe d'application, pas le mot de passe du compte."""

    def __init__(self, hote: str, port: int, utilisateur: str, mot_de_passe: str):
        self.hote, self.port = hote, port
        self.utilisateur, self.mot_de_passe = utilisateur, mot_de_passe

    def envoyer(self, destinataire: str, sujet: str, corps: str, piece_jointe: Path | None = None) -> None:
        message = EmailMessage()
        message["From"] = self.utilisateur
        message["To"] = destinataire
        message["Subject"] = sujet
        message.set_content(corps)
        if piece_jointe:
            message.add_attachment(Path(piece_jointe).read_bytes(), maintype="application",
                                   subtype="pdf", filename=Path(piece_jointe).name)
        with smtplib.SMTP_SSL(self.hote, self.port, timeout=30,
                              context=ssl.create_default_context()) as serveur:
            serveur.login(self.utilisateur, self.mot_de_passe)
            serveur.send_message(message)


def creer_envoyeur(config: Config):
    mode = config.directeur.get("mode", "console")
    if mode == "console":
        return EnvoyeurConsole()
    if mode == "smtp":
        s = config.secrets
        manquants = [c for c in ("SMTP_UTILISATEUR", "SMTP_MOT_DE_PASSE") if not s.get(c)]
        if manquants:
            raise ValueError(f"Mode smtp : à remplir dans .env : {', '.join(manquants)}")
        return EnvoyeurSMTP(s.get("SMTP_HOTE") or "smtp.gmail.com", int(s.get("SMTP_PORT") or 465),
                            s["SMTP_UTILISATEUR"], s["SMTP_MOT_DE_PASSE"])
    raise ValueError(f"Mode d'email inconnu : {mode!r} (attendu : 'console' ou 'smtp')")


CORPS = ("Bonjour,\n\nVeuillez trouver ci-joint le rapport du personnel de {ecole} ({periode}).\n"
         "Ce document est confidentiel et vous est destiné.\n\nARIA_point (ARIA_tech)\n")


def envoyer_rapport(conn: sqlite3.Connection, config: Config, envoyeur, chemin: Path, periode: str,
                    maintenant: datetime | None = None) -> None:
    """Envoie un PDF au directeur et note l'envoi. Lève une exception en cas d'échec."""
    destinataire = config.directeur.get("email", "").strip()
    if not destinataire:
        raise ValueError("Adresse du directeur manquante : remplis [directeur] email dans config.toml.")
    envoyeur.envoyer(destinataire, f"{config.nom_ecole} : rapport du personnel, {periode}",
                     CORPS.format(ecole=config.nom_ecole, periode=periode), chemin)
    ecrire_etat(conn, f"email:{chemin.name}", dt_texte(maintenant or datetime.now()))
    conn.commit()


def deja_envoye(conn: sqlite3.Connection, chemin: Path) -> bool:
    return lire_etat(conn, f"email:{chemin.name}") is not None


def envoyer_en_attente(conn: sqlite3.Connection, config: Config, envoyeur, a_envoyer: list[tuple[Path, str]],
                       maintenant: datetime | None = None) -> int:
    """Envoie les PDF (chemin, période) qui existent et ne sont pas encore partis.
    S'arrête au premier échec (probablement pas d'internet) : réessai au passage suivant."""
    if not config.directeur.get("email", "").strip():
        return 0
    envoyes = 0
    for chemin, periode in a_envoyer:
        if not chemin.exists() or deja_envoye(conn, chemin):
            continue
        try:
            envoyer_rapport(conn, config, envoyeur, chemin, periode, maintenant)
            envoyes += 1
        except Exception as e:
            log.warning("Email au directeur impossible pour l'instant (%s). Nouvel essai dans 1 h.", e)
            break
    return envoyes


def tester(config: Config, destinataire: str) -> None:
    creer_envoyeur(config).envoyer(destinataire, f"{config.nom_ecole} : test ARIA_point",
                                   "Si tu lis ce message, l'envoi des rapports par email fonctionne.\n")
