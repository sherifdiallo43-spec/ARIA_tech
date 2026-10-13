"""Fin d'année scolaire : archiver les pointages et repartir sur une base légère.

Déroulé, du plus prudent au plus risqué :
1. Sauvegarde complète de la base (filet de sécurité).
2. Copie de l'année dans un fichier séparé archives/annee_2026-2027.db
   (même structure que la base principale) + des CSV lisibles dans Excel.
3. Vérification : autant de lignes dans l'archive que dans l'original.
4. Seulement alors : suppression dans la base principale, puis VACUUM
   (qui rend au disque la place libérée).
Les fiches élèves et leurs abonnements restent dans la base principale.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import backup
from .config import Config
from .db import SCHEMA, dt_texte, ecrire_etat

log = logging.getLogger(__name__)


def archiver_annee(conn: sqlite3.Connection, config: Config, libelle: str, date_fin: date,
                   maintenant: datetime | None = None) -> dict:
    """Archive tout ce qui précède le lendemain de `date_fin`. Renvoie un résumé."""
    maintenant = maintenant or datetime.now()
    lendemain = date_fin + timedelta(days=1)
    limite = dt_texte(datetime.combine(lendemain, time()))  # "2027-08-01 00:00:00"
    limite_jour = lendemain.isoformat()                      # "2027-08-01" (pour les paiements)

    dossier = config.chemin("archives")
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / f"annee_{libelle}.db"
    if cible.exists():
        raise FileExistsError(f"L'archive {cible.name} existe déjà : rien n'a été modifié.")

    sauvegarde = backup.sauvegarder(conn, config, maintenant)

    # Base d'archive vide, avec exactement les mêmes tables.
    archive = sqlite3.connect(cible)
    archive.executescript(SCHEMA)
    archive.close()

    selections = {
        "eleves": ("SELECT * FROM main.eleves", ()),
        "pointages": ("SELECT * FROM main.pointages WHERE horodatage < ?", (limite,)),
        "notifications": ("SELECT * FROM main.notifications WHERE pointage_id IN "
                          "(SELECT id FROM main.pointages WHERE horodatage < ?)", (limite,)),
        "paiements": ("SELECT * FROM main.paiements WHERE date_paiement < ?", (limite_jour,)),
    }

    conn.commit()  # ATTACH est interdit au milieu d'une transaction
    conn.execute("ATTACH DATABASE ? AS archive", (str(cible),))
    try:
        comptes = {}
        for table, (requete, params) in selections.items():
            conn.execute(f"INSERT INTO archive.{table} {requete}", params)
            attendu = conn.execute(f"SELECT COUNT(*) FROM ({requete})", params).fetchone()[0]
            copie = conn.execute(f"SELECT COUNT(*) FROM archive.{table}").fetchone()[0]
            if copie != attendu:
                raise RuntimeError(f"Copie incomplète de {table} : {copie}/{attendu} lignes.")
            comptes[table] = copie
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("DETACH DATABASE archive")
        cible.unlink(missing_ok=True)
        raise

    fichiers_csv = [_exporter_csv(conn, table, dossier / f"annee_{libelle}_{table}.csv")
                    for table in selections]

    # Les notifications d'abord : elles font référence aux pointages.
    conn.execute("DELETE FROM main.notifications WHERE pointage_id IN "
                 "(SELECT id FROM main.pointages WHERE horodatage < ?)", (limite,))
    conn.execute("DELETE FROM main.pointages WHERE horodatage < ?", (limite,))
    conn.execute("DELETE FROM main.paiements WHERE date_paiement < ?", (limite_jour,))
    # La pointeuse garde peut-être encore ces vieux pointages en mémoire :
    # sans ce repère, la prochaine lecture les réimporterait.
    ecrire_etat(conn, "ignorer_avant", limite)
    conn.commit()
    conn.execute("DETACH DATABASE archive")
    conn.execute("VACUUM")

    log.info("Année %s archivée dans %s : %s", libelle, cible.name, comptes)
    return {"archive": cible, "sauvegarde": sauvegarde, "csv": fichiers_csv, "comptes": comptes}


def _exporter_csv(conn: sqlite3.Connection, table: str, chemin: Path) -> Path:
    """CSV au format Excel français : séparateur ";" et encodage UTF-8 avec BOM."""
    curseur = conn.execute(f"SELECT * FROM archive.{table}")
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        ecrivain = csv.writer(f, delimiter=";")
        ecrivain.writerow([colonne[0] for colonne in curseur.description])
        ecrivain.writerows(curseur)
    return chemin
