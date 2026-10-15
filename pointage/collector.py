"""Synchronisation : lire la pointeuse, ranger chaque pointage, préparer les alertes.

Principe de robustesse : à chaque lecture on récupère TOUTE la mémoire de la
pointeuse et on laisse la base trier ce qui est nouveau. Après une coupure de
réseau ou de courant, il n'y a donc rien de spécial à faire : la lecture
suivante rattrape automatiquement tout ce qui manque, sans doublon.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import notifier, personnel
from .config import Config, lire_heure
from .db import dt_texte, ecrire_etat, lire_etat
from .device import Pointeuse, PointageBrut

log = logging.getLogger(__name__)


@dataclass
class ResultatSync:
    lus: int = 0                 # pointages présents dans la mémoire du boîtier
    nouveaux: int = 0            # enregistrés pour la première fois
    deja_connus: int = 0         # déjà en base (lectures précédentes)
    rebonds: int = 0             # doigt reposé peu après (moins de 30 min) : ignoré
    anterieurs_archive: int = 0  # d'une année déjà archivée : ignorés


def classer(moment: datetime, deja_pointe_ce_jour: bool, horaires: dict) -> tuple[str, bool]:
    """Deux pointages par jour : le premier est l'arrivée, les suivants la sortie.

    Exception : un premier pointage tardif (après `sortie_seule_apres`) est une
    sortie. L'élève a oublié de pointer le matin, il ne vient pas d'arriver.
    Renvoie (type, en_retard). Seule une arrivée peut être en retard.
    """
    heure = moment.time()
    if deja_pointe_ce_jour or heure >= lire_heure(horaires["sortie_seule_apres"]):
        return "sortie", False
    return "arrivee", heure > lire_heure(horaires["retard_apres"])


def _deja_pointe_ce_jour(conn: sqlite3.Connection, zk_user_id: str, moment: datetime) -> bool:
    debut_journee = moment.replace(hour=0, minute=0, second=0)
    return conn.execute(
        "SELECT 1 FROM pointages WHERE zk_user_id = ? AND horodatage >= ? AND horodatage < ?",
        (zk_user_id, dt_texte(debut_journee), dt_texte(moment)),
    ).fetchone() is not None


def deja_connu(conn: sqlite3.Connection, zk_user_id: str, horodatage: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pointages WHERE zk_user_id = ? AND horodatage = ?",
        (zk_user_id, horodatage),
    ).fetchone() is not None


def _est_rebond(conn: sqlite3.Connection, zk_user_id: str, moment: datetime, delai: timedelta) -> bool:
    """Vrai si le même élève a déjà un pointage enregistré juste avant (moins de `delai`)."""
    return conn.execute(
        "SELECT 1 FROM pointages WHERE zk_user_id = ? AND horodatage >= ? AND horodatage < ?",
        (zk_user_id, dt_texte(moment - delai), dt_texte(moment)),
    ).fetchone() is not None


def synchroniser(conn: sqlite3.Connection, pointeuse: Pointeuse, config: Config,
                 maintenant: datetime | None = None) -> ResultatSync:
    """Une lecture complète de la pointeuse. Lève une exception si elle est injoignable."""
    bruts = pointeuse.lire_pointages()
    # Taille de la mémoire du boîtier : sert à décider quand la vider (voir purge.py).
    ecrire_etat(conn, "memoire_pointeuse", str(len(bruts)))
    return enregistrer(conn, bruts, config, maintenant)


def enregistrer(conn: sqlite3.Connection, bruts: list[PointageBrut], config: Config,
                maintenant: datetime | None = None) -> ResultatSync:
    """Range une liste de pointages bruts dans la base et prépare les alertes."""
    maintenant = maintenant or datetime.now()
    resultat = ResultatSync(lus=len(bruts))
    limite_archive = lire_etat(conn, "ignorer_avant")
    delai_rebond = timedelta(minutes=config.pointeuse.get("anti_doublon_minutes", 30))
    tolerance = int(config.personnel.get("tolerance_retard_minutes", 5))

    # Ordre chronologique obligatoire : la détection des rebonds compare
    # chaque pointage à ceux qui le précèdent.
    for brut in sorted(bruts, key=lambda p: (p.horodatage, p.zk_user_id)):
        horodatage = dt_texte(brut.horodatage)
        if limite_archive and horodatage < limite_archive:
            resultat.anterieurs_archive += 1
            continue
        if deja_connu(conn, brut.zk_user_id, horodatage):
            resultat.deja_connus += 1
            continue
        if _est_rebond(conn, brut.zk_user_id, brut.horodatage, delai_rebond):
            resultat.rebonds += 1
            continue

        membre = personnel.trouver(conn, brut.zk_user_id)
        if membre:
            type_, en_retard = personnel.classer(conn, membre, brut.horodatage, tolerance)
        else:
            type_, en_retard = classer(brut.horodatage,
                                       _deja_pointe_ce_jour(conn, brut.zk_user_id, brut.horodatage),
                                       config.horaires)
        curseur = conn.execute(
            "INSERT INTO pointages (zk_user_id, horodatage, type, en_retard, recu_le) "
            "VALUES (?, ?, ?, ?, ?)",
            (brut.zk_user_id, horodatage, type_, int(en_retard), dt_texte(maintenant)),
        )
        resultat.nouveaux += 1
        if not membre:  # aucun message pour le personnel
            notifier.creer_notification(conn, curseur.lastrowid, brut.zk_user_id, brut.horodatage,
                                        type_, en_retard, config, maintenant)

    ecrire_etat(conn, "derniere_sync", dt_texte(maintenant))
    conn.commit()
    if resultat.nouveaux:
        log.info("%d nouveau(x) pointage(s) enregistré(s).", resultat.nouveaux)
    return resultat
