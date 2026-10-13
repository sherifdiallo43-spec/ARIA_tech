"""Vidage de la mémoire de la pointeuse.

Pourquoi : le boîtier garde chaque pointage dans sa mémoire flash, de taille
limitée (souvent 50 000 à 200 000). Comme on relit TOUTE la mémoire à chaque
cycle, une mémoire pleine rend aussi chaque lecture plus lente. Il faut donc
la vider de temps en temps, mais seulement quand on est certain que tout est
en sécurité dans la base.

Ordre des opérations (dans device.lire_puis_effacer + _securiser ci-dessous) :
1. boîtier bloqué, lecture de toute la mémoire
2. enregistrement dans la base + vérification que chaque pointage y est
3. copie brute de la mémoire en JSON + sauvegarde complète de la base
4. seulement alors : effacement, puis déblocage du boîtier
Une erreur aux étapes 2 ou 3 annule l'effacement.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from . import backup, collector
from .config import Config, lire_heure
from .db import dt_texte, ecrire_etat, lire_etat
from .device import Pointeuse, PointageBrut

log = logging.getLogger(__name__)


class VerificationEchouee(Exception):
    """Des pointages lus dans le boîtier sont absents de la base : on n'efface rien."""


def vider_memoire(conn: sqlite3.Connection, pointeuse: Pointeuse, config: Config,
                  maintenant: datetime | None = None) -> dict:
    """Vide la mémoire de la pointeuse après avoir tout mis en sécurité. Renvoie un résumé."""
    maintenant = maintenant or datetime.now()
    resume: dict = {}

    def _securiser(bruts: list[PointageBrut]) -> None:
        sync = collector.enregistrer(conn, bruts, config, maintenant)

        # Vérification : chaque pointage lu est soit en base, soit volontairement
        # écarté (doigt reposé, année déjà archivée). Aucun autre cas n'est admis.
        en_base = sum(collector.deja_connu(conn, b.zk_user_id, dt_texte(b.horodatage)) for b in bruts)
        if en_base + sync.rebonds + sync.anterieurs_archive != len(bruts):
            raise VerificationEchouee(
                f"{len(bruts)} pointages lus mais seulement {en_base} retrouvés en base "
                f"(+ {sync.rebonds} doublons, {sync.anterieurs_archive} archivés).")

        # Copie brute de la mémoire du boîtier, tels quels (doublons compris).
        dossier = config.chemin("sauvegardes")
        dossier.mkdir(parents=True, exist_ok=True)
        copie = dossier / f"memoire_pointeuse_{maintenant:%Y-%m-%d_%H%M%S}.json"
        copie.write_text(json.dumps(
            [{"id": b.zk_user_id, "t": dt_texte(b.horodatage)} for b in bruts]), encoding="utf-8")

        resume.update(lus=len(bruts), nouveaux=sync.nouveaux, copie_brute=copie,
                      sauvegarde=backup.sauvegarder(conn, config, maintenant))

    pointeuse.lire_puis_effacer(_securiser)
    ecrire_etat(conn, "memoire_pointeuse", "0")
    ecrire_etat(conn, "dernier_vidage", dt_texte(maintenant))
    conn.commit()
    log.info("Pointeuse vidée : %d pointages mis en sécurité, copie brute : %s",
             resume["lus"], resume["copie_brute"].name)
    return resume


def vider_si_necessaire(conn: sqlite3.Connection, pointeuse: Pointeuse, config: Config,
                        maintenant: datetime | None = None) -> dict | None:
    """Vidage automatique, appelé par le service toutes les heures.

    Deux conditions : la mémoire dépasse le seuil, ET on est dans la plage
    horaire autorisée (le boîtier est bloqué quelques secondes : on évite les
    heures d'arrivée des élèves).
    """
    maintenant = maintenant or datetime.now()
    seuil = int(config.pointeuse.get("vider_au_dela", 0))
    taille = int(lire_etat(conn, "memoire_pointeuse", "0"))
    if seuil <= 0 or taille < seuil:
        return None
    debut, fin = (lire_heure(h) for h in config.pointeuse.get("vidage_auto_entre", ["18:00", "23:59"]))
    if not debut <= maintenant.time() <= fin:
        log.info("Mémoire de la pointeuse : %d pointages (seuil %d). Vidage prévu entre %s et %s.",
                 taille, seuil, debut.strftime("%Hh%M"), fin.strftime("%Hh%M"))
        return None
    return vider_memoire(conn, pointeuse, config, maintenant)
