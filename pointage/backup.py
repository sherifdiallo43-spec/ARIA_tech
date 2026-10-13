"""Sauvegarde automatique de la base SQLite.

On utilise l'API officielle `Connection.backup()` plutôt qu'une simple copie
de fichier : elle produit une copie cohérente même si le service est en train
d'écrire un pointage au même moment. Une copie de fichier brute, elle,
pourrait capturer une base à moitié écrite.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .db import dt_texte, ecrire_etat, lire_etat, texte_dt

log = logging.getLogger(__name__)

PREFIXE = "pointage_"
FORMAT_NOM = "%Y-%m-%d_%H%M%S"


def sauvegarder(conn: sqlite3.Connection, config: Config, maintenant: datetime | None = None) -> Path:
    """Copie complète de la base dans le dossier des sauvegardes. Renvoie le fichier créé."""
    maintenant = maintenant or datetime.now()
    dossier = config.chemin("sauvegardes")
    dossier.mkdir(parents=True, exist_ok=True)
    cible_chemin = dossier / f"{PREFIXE}{maintenant.strftime(FORMAT_NOM)}.db"

    conn.commit()  # on ne sauvegarde pas une transaction à moitié faite
    cible = sqlite3.connect(cible_chemin)
    try:
        conn.backup(cible)
    finally:
        cible.close()

    ecrire_etat(conn, "derniere_sauvegarde", dt_texte(maintenant))
    conn.commit()
    supprimees = nettoyer_anciennes(dossier, config.sauvegarde["retention_jours"], maintenant)
    log.info("Sauvegarde créée : %s (%d ancienne(s) supprimée(s)).", cible_chemin.name, supprimees)
    return cible_chemin


def nettoyer_anciennes(dossier: Path, retention_jours: int, maintenant: datetime) -> int:
    """Supprime les sauvegardes plus vieilles que `retention_jours` (date lue dans le nom)."""
    supprimees = 0
    for fichier in dossier.glob(f"{PREFIXE}*.db"):
        try:
            date_fichier = datetime.strptime(fichier.stem[len(PREFIXE):], FORMAT_NOM)
        except ValueError:
            continue  # fichier qui ne vient pas de nous : on n'y touche pas
        if maintenant - date_fichier > timedelta(days=retention_jours):
            fichier.unlink()
            supprimees += 1
    return supprimees


def sauvegarder_si_besoin(conn: sqlite3.Connection, config: Config,
                          maintenant: datetime | None = None) -> Path | None:
    """Sauvegarde si la dernière date de plus de 24 h.

    Le PC de l'école est souvent éteint la nuit : plutôt qu'une heure fixe
    (2 h du matin), on vérifie régulièrement si une sauvegarde est due.
    """
    maintenant = maintenant or datetime.now()
    derniere = lire_etat(conn, "derniere_sauvegarde")
    if derniere and maintenant - texte_dt(derniere) < timedelta(hours=24):
        return None
    return sauvegarder(conn, config, maintenant)
