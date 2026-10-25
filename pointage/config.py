"""Chargement de la configuration.

Deux sources séparées volontairement :
- config.toml : les réglages de l'école (horaires, prix, chemins). Pas secret.
- .env        : les secrets (jeton WhatsApp de Meta, mot de passe de la pointeuse).
                Jamais versionné, jamais partagé.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

RACINE_PROJET = Path(__file__).resolve().parent.parent
CONFIG_REFERENCE = "config.reference.toml"  # déposé par les mises à jour (voir charger)

CLES_SECRETES = (
    "META_JETON",
    "META_ID_NUMERO",
    "META_API_VERSION",
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
    # utf-8-sig : accepte aussi un fichier enregistré avec BOM (ancien Bloc-notes de Windows).
    return tomllib.loads(Path(chemin).read_text(encoding="utf-8-sig"))


def fusionner(reference: dict, ecole: dict) -> dict:
    """Réglages de l'école, complétés par ceux de la référence quand ils manquent."""
    resultat = dict(reference)
    for cle, valeur in ecole.items():
        if isinstance(valeur, dict) and isinstance(reference.get(cle), dict):
            resultat[cle] = fusionner(reference[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def charger(chemin_config: Path | None = None) -> Config:
    """Lit config.toml et .env (situés dans le même dossier).

    Après une mise à jour, config.reference.toml (le config.toml livré avec la nouvelle
    version) est à côté : un réglage apparu dans la nouvelle version et absent du
    config.toml de l'école y prend sa valeur par défaut, au lieu de faire planter le service.
    """
    from dotenv import load_dotenv

    chemin_config = Path(chemin_config or RACINE_PROJET / "config.toml")
    racine = chemin_config.parent
    load_dotenv(racine / ".env")
    secrets = {cle: os.getenv(cle, "") for cle in CLES_SECRETES}
    donnees = lire_toml(chemin_config)
    reference = racine / CONFIG_REFERENCE
    if reference.exists():
        donnees = fusionner(lire_toml(reference), donnees)
    return Config.depuis_dict(racine, donnees, secrets)


def lire_heure(texte: str) -> time:
    """Convertit "08:00" en objet time."""
    heures, minutes = texte.split(":")
    return time(int(heures), int(minutes))
