"""Chargement de la configuration.

Deux sources séparées volontairement :
- config.toml : les réglages de l'école (horaires, prix, chemins). Pas secret.
- .env        : les secrets (identifiants Twilio, mot de passe de la pointeuse).
                Jamais versionné, jamais partagé.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

RACINE_PROJET = Path(__file__).resolve().parent.parent

CLES_SECRETES = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_WHATSAPP_FROM",
    "TWILIO_CONTENT_SID",
    "TWILIO_SMS_FROM",
    "ZK_MOT_DE_PASSE",
    "DASHBOARD_PASSWORD",
    "SMTP_HOTE",
    "SMTP_PORT",
    "SMTP_UTILISATEUR",
    "SMTP_MOT_DE_PASSE",
)


@dataclass
class Config:
    racine: Path          # dossier de référence pour tous les chemins relatifs
    ecole: dict
    pointeuse: dict
    horaires: dict
    abonnement: dict
    notifications: dict
    sauvegarde: dict
    chemins: dict
    rapports: dict = field(default_factory=dict)
    personnel: dict = field(default_factory=dict)
    directeur: dict = field(default_factory=dict)
    pointeuses: list[dict] = field(default_factory=list)
    secrets: dict = field(default_factory=dict)

    @classmethod
    def depuis_dict(cls, racine: Path, donnees: dict, secrets: dict | None = None) -> Config:
        return cls(
            racine=Path(racine),
            ecole=donnees["ecole"],
            pointeuse=donnees["pointeuse"],
            horaires=donnees["horaires"],
            abonnement=donnees["abonnement"],
            notifications=donnees["notifications"],
            sauvegarde=donnees["sauvegarde"],
            chemins=donnees["chemins"],
            rapports=donnees.get("rapports", {}),
            personnel=donnees.get("personnel", {}),
            directeur=donnees.get("directeur", {}),
            pointeuses=donnees.get("pointeuses", []),
            secrets=secrets or {},
        )

    @property
    def nom_ecole(self) -> str:
        return self.ecole["nom"]

    def chemin(self, cle: str) -> Path:
        """Chemin absolu d'un fichier ou dossier déclaré dans la section [chemins]."""
        return self.racine / self.chemins[cle]


def lire_toml(chemin: Path) -> dict:
    with open(chemin, "rb") as f:
        return tomllib.load(f)


def charger(chemin_config: Path | None = None) -> Config:
    """Lit config.toml et .env (situés dans le même dossier)."""
    from dotenv import load_dotenv

    chemin_config = Path(chemin_config or RACINE_PROJET / "config.toml")
    racine = chemin_config.parent
    load_dotenv(racine / ".env")
    secrets = {cle: os.getenv(cle, "") for cle in CLES_SECRETES}
    return Config.depuis_dict(racine, lire_toml(chemin_config), secrets)


def lire_heure(texte: str) -> time:
    """Convertit "08:00" en objet time."""
    heures, minutes = texte.split(":")
    return time(int(heures), int(minutes))
