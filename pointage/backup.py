"""Sauvegarde automatique de la base SQLite.

On utilise l'API officielle `Connection.backup()` plutôt qu'une simple copie
de fichier : elle produit une copie cohérente même si le service est en train
d'écrire un pointage au même moment. Une copie de fichier brute, elle,
pourrait capturer une base à moitié écrite.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import string
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


# --- Copie sur un support externe (clé USB, disque externe) ------------------------
#
# Les sauvegardes ci-dessus sont sur le même disque que la base : si ce disque
# meurt ou si le PC est volé, tout est perdu d'un coup. On copie donc chaque jour
# la dernière sauvegarde sur un support qui peut être rangé ailleurs.
#
# Sous Windows, la lettre d'une clé USB change (E:, F:...) selon l'ordre de
# branchement. On ne la fixe donc pas : on cherche, à la racine de chaque lecteur
# de D: à Z:, un dossier au nom convenu (créé une fois pour toutes sur la clé).

ALERTE_JOURS = 3  # au-delà, le tableau de bord et `statut` signalent l'absence de copie


def _lecteurs() -> list[Path]:
    """Racines des lecteurs D: à Z: présents (C: est le disque du PC lui-même)."""
    if os.name != "nt":
        return []
    return [Path(f"{lettre}:\\") for lettre in string.ascii_uppercase[3:]
            if os.path.exists(f"{lettre}:\\")]


def trouver_dossier_externe(config: Config, racines: list[Path] | None = None) -> Path | None:
    """Dossier de copie externe s'il est branché, sinon None.

    Réglage `copie_externe` de [sauvegarde] : un chemin complet ("E:/sauvegardes"),
    ou seulement un nom de dossier ("ARIA_point_sauvegardes") à chercher sur les lecteurs.
    """
    reglage = config.sauvegarde.get("copie_externe", "")
    if not reglage:
        return None
    chemin = Path(reglage)
    if chemin.is_absolute():
        return chemin if chemin.is_dir() else None
    for racine in (racines if racines is not None else _lecteurs()):
        if (racine / reglage).is_dir():
            return racine / reglage
    return None


def _derniere_sauvegarde_locale(config: Config) -> Path | None:
    fichiers = sorted(config.chemin("sauvegardes").glob(f"{PREFIXE}*.db"))
    return fichiers[-1] if fichiers else None


def copier_vers_externe(conn: sqlite3.Connection, config: Config, maintenant: datetime | None = None,
                        racines: list[Path] | None = None) -> Path | None:
    """Copie la dernière sauvegarde sur le support externe. None si le support est absent.

    La copie est d'abord écrite sous un nom temporaire puis vérifiée (SQLite relit
    tout le fichier) : une clé arrachée en pleine copie ne laisse jamais un fichier
    abîmé qui passerait pour une bonne sauvegarde.
    """
    maintenant = maintenant or datetime.now()
    dossier = trouver_dossier_externe(config, racines)
    if dossier is None:
        return None
    source = _derniere_sauvegarde_locale(config) or sauvegarder(conn, config, maintenant)
    cible = dossier / source.name
    temporaire = dossier / (source.name + ".tmp")
    try:
        shutil.copyfile(source, temporaire)
        verif = sqlite3.connect(temporaire)
        try:
            resultat = verif.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            verif.close()
        if resultat != "ok":
            raise RuntimeError(f"Copie externe abîmée ({resultat}).")
    except Exception:
        # Copie interrompue ou illisible : on ne laisse rien qui passerait pour une sauvegarde.
        temporaire.unlink(missing_ok=True)
        raise
    temporaire.replace(cible)

    ecrire_etat(conn, "derniere_copie_externe", dt_texte(maintenant))
    conn.commit()
    nettoyer_anciennes(dossier, config.sauvegarde["retention_jours"], maintenant)
    log.info("Copie externe créée : %s", cible)
    return cible


def copier_externe_si_besoin(conn: sqlite3.Connection, config: Config, maintenant: datetime | None = None,
                             racines: list[Path] | None = None) -> Path | None:
    """Copie externe si la dernière a plus de 24 h et que le support est branché."""
    maintenant = maintenant or datetime.now()
    if not config.sauvegarde.get("copie_externe"):
        return None
    derniere = lire_etat(conn, "derniere_copie_externe")
    if derniere and maintenant - texte_dt(derniere) < timedelta(hours=24):
        return None
    copie = copier_vers_externe(conn, config, maintenant, racines)
    if copie is None:
        log.warning("Copie externe impossible : support « %s » introuvable. Branche la clé USB.",
                    config.sauvegarde["copie_externe"])
    return copie


def alerte_copie_externe(conn: sqlite3.Connection, config: Config,
                         maintenant: datetime | None = None) -> str | None:
    """Message d'alerte si la copie externe est en retard, sinon None (ou si elle n'est pas réglée)."""
    maintenant = maintenant or datetime.now()
    if not config.sauvegarde.get("copie_externe"):
        return None
    derniere = lire_etat(conn, "derniere_copie_externe")
    if not derniere:
        return "Aucune sauvegarde n'a encore été copiée sur la clé USB. Branche-la sur ce PC."
    jours = (maintenant - texte_dt(derniere)).days
    if jours >= ALERTE_JOURS:
        return f"Aucune sauvegarde copiée sur la clé USB depuis {jours} jours. Branche-la sur ce PC."
    return None
