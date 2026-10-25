"""Base de données SQLite : connexion, schéma et petits utilitaires.

Toutes les dates sont stockées en texte au format ISO ("2026-10-12 07:42:05").
Ce format a une propriété précieuse : l'ordre alphabétique est aussi l'ordre
chronologique. On peut donc comparer des dates directement en SQL avec < et >.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

FORMAT_DATETIME = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
-- Fiche de chaque élève. zk_user_id = numéro sous lequel l'empreinte est
-- enregistrée dans la pointeuse. C'est le lien entre le boîtier et la base.
CREATE TABLE IF NOT EXISTS eleves (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    zk_user_id       TEXT    NOT NULL UNIQUE,
    nom              TEXT    NOT NULL,
    prenom           TEXT    NOT NULL,
    classe           TEXT    NOT NULL,
    tel_parent       TEXT    NOT NULL,          -- format international : +221...
    date_inscription TEXT    NOT NULL,
    essai_fin        TEXT    NOT NULL,          -- dernier jour de l'essai gratuit
    abonnement_fin   TEXT,                      -- dernier jour payé (NULL = jamais payé)
    actif_scolarite  INTEGER NOT NULL DEFAULT 1, -- 0 = a quitté l'école
    date_depart      TEXT                        -- premier jour où il n'est plus élève (NULL = toujours là)
);

-- Personnel (professeurs, surveillants...) qui pointe sur le même boîtier.
-- Pas de parents à prévenir ni d'abonnement : seulement présence, retards, heures.
CREATE TABLE IF NOT EXISTS personnel (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    zk_user_id       TEXT    NOT NULL UNIQUE,
    nom              TEXT    NOT NULL,
    prenom           TEXT    NOT NULL,
    fonction         TEXT    NOT NULL,
    date_inscription TEXT    NOT NULL,
    actif            INTEGER NOT NULL DEFAULT 1,
    date_depart      TEXT     -- premier jour où il ne fait plus partie du personnel (NULL = toujours là)
);

-- Emploi du temps du personnel : une ligne par cours de la semaine.
-- Un jour sans créneau = la personne n'est pas attendue ce jour-là.
CREATE TABLE IF NOT EXISTS creneaux (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    zk_user_id TEXT    NOT NULL REFERENCES personnel(zk_user_id),
    jour       TEXT    NOT NULL,   -- lundi ... samedi
    debut      TEXT    NOT NULL,   -- "08:00"
    fin        TEXT    NOT NULL,
    classe     TEXT    NOT NULL DEFAULT '',
    matiere    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paiements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    eleve_id      INTEGER NOT NULL REFERENCES eleves(id),
    montant       INTEGER NOT NULL,             -- en FCFA
    mois_couverts INTEGER NOT NULL,
    date_paiement TEXT    NOT NULL,
    note          TEXT
);

-- Un pointage = une empreinte posée sur le boîtier.
-- UNIQUE(zk_user_id, horodatage) : la garantie anti-doublon. On peut relire
-- toute la mémoire de la pointeuse autant de fois qu'on veut, un même
-- pointage ne sera jamais enregistré deux fois.
CREATE TABLE IF NOT EXISTS pointages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    zk_user_id TEXT    NOT NULL,
    horodatage TEXT    NOT NULL,
    type       TEXT    NOT NULL,   -- arrivee ou sortie
    en_retard  INTEGER NOT NULL DEFAULT 0,
    recu_le    TEXT    NOT NULL,   -- quand le logiciel l'a récupéré
    UNIQUE (zk_user_id, horodatage)
);
CREATE INDEX IF NOT EXISTS idx_pointages_horodatage ON pointages(horodatage);

-- File d'attente des messages WhatsApp. Un message reste "en_attente" tant
-- qu'il n'a pas pu partir (coupure internet), puis passe à "envoye" ou "echec".
-- Une alerte d'absence n'a pas de pointage (pointage_id vide) : c'est pourquoi
-- l'élève, le moment et le type sont gardés dans la notification elle-même.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pointage_id INTEGER REFERENCES pointages(id),  -- NULL pour une alerte d'absence
    zk_user_id  TEXT    NOT NULL,
    moment      TEXT    NOT NULL,  -- heure annoncée : celle du pointage, ou du constat d'absence
    type        TEXT    NOT NULL,  -- arrivee, sortie ou absence
    tel         TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    variables   TEXT    NOT NULL,  -- JSON, pour les modèles de message WhatsApp
    statut      TEXT    NOT NULL,  -- en_attente, envoye, echec, ignore_abonnement, ignore_trop_ancien
    tentatives  INTEGER NOT NULL DEFAULT 0,
    erreur      TEXT,
    cree_le     TEXT    NOT NULL,
    envoye_le   TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_statut ON notifications(statut);
CREATE INDEX IF NOT EXISTS idx_notifications_eleve ON notifications(zk_user_id, moment);

-- Petites valeurs de fonctionnement (dernière synchro, dernière sauvegarde...).
CREATE TABLE IF NOT EXISTS etat (
    cle    TEXT PRIMARY KEY,
    valeur TEXT
);
"""


def connecter(chemin: Path | str) -> sqlite3.Connection:
    """Ouvre (et crée si besoin) la base SQLite."""
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin)
    conn.row_factory = sqlite3.Row             # accès aux colonnes par nom : ligne["nom"]
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL : les lectures (rapports) ne bloquent pas les écritures (pointages),
    # et la base résiste mieux à une coupure de courant.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class BaseTropRecente(ValueError):
    """La base a été mise à jour par une version plus récente du logiciel."""


def version_base(conn: sqlite3.Connection) -> int:
    """Numéro de version de la structure de la base (0 = base d'avant la numérotation)."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def initialiser(conn: sqlite3.Connection) -> None:
    """Crée les tables manquantes et met à jour une base d'une version précédente.

    Le numéro de version est rangé dans l'en-tête du fichier SQLite (PRAGMA user_version).
    Une base neuve reçoit directement le schéma complet (SCHEMA) et le dernier numéro.
    Une base existante passe par chaque migration manquante, dans l'ordre : une école
    restée en version 1 pendant que le logiciel passait en 3 reçoit la 2 puis la 3.
    """
    version = version_base(conn)
    if version > VERSION_BASE:
        raise BaseTropRecente(
            f"la base est en version {version}, ce logiciel ne connaît que la version "
            f"{VERSION_BASE}. Elle a été mise à jour par un logiciel plus récent : "
            "réinstalle la dernière version d'ARIA_point.")
    existante = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    if existante:  # une base neuve n'a encore aucune table : rien à migrer
        for numero in range(version + 1, VERSION_BASE + 1):
            MIGRATIONS[numero](conn)
    conn.executescript(SCHEMA)  # tables nouvelles (une nouvelle table n'a pas besoin de migration)
    conn.execute(f"PRAGMA user_version = {VERSION_BASE}")
    conn.commit()


# --- Migrations ------------------------------------------------------------------
#
# Pour changer la structure d'une table qui existe déjà (ajouter une colonne...) :
# 1. modifier SCHEMA (pour les bases neuves) ;
# 2. écrire une fonction _migration_N (pour les bases existantes) et l'ajouter à MIGRATIONS ;
# 3. passer VERSION_BASE à N.
# Une migration peut être relancée après une coupure de courant : elle doit vérifier
# ce qui est déjà fait (comme _migration_1) plutôt que tout refaire à l'aveugle.

def _migration_1(conn: sqlite3.Connection) -> None:
    """Notifications sans pointage (alertes d'absence) : la colonne `moment` apparaît."""
    colonnes = _colonnes(conn, "notifications")
    if not colonnes or "moment" in colonnes:
        return
    # SQLite ne sait pas rendre une colonne facultative : on met l'ancienne table
    # de côté, on crée la nouvelle, puis on recopie.
    conn.execute("ALTER TABLE notifications RENAME TO notifications_v1")
    conn.execute("DROP INDEX IF EXISTS idx_notifications_statut")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO notifications (id, pointage_id, zk_user_id, moment, type, tel, message, "
        "  variables, statut, tentatives, erreur, cree_le, envoye_le) "
        "SELECT n.id, n.pointage_id, p.zk_user_id, p.horodatage, p.type, n.tel, n.message, "
        "  n.variables, n.statut, n.tentatives, n.erreur, n.cree_le, n.envoye_le "
        "FROM notifications_v1 n JOIN pointages p ON p.id = n.pointage_id")
    conn.execute("DROP TABLE notifications_v1")


def _migration_2(conn: sqlite3.Connection) -> None:
    """Élèves partis : la date de départ apparaît (rapports justes pour les jours d'avant)."""
    if "date_depart" not in _colonnes(conn, "eleves"):
        conn.execute("ALTER TABLE eleves ADD COLUMN date_depart TEXT")


def _migration_3(conn: sqlite3.Connection) -> None:
    """Personnel parti : la date de départ apparaît, comme pour les élèves."""
    if "date_depart" not in _colonnes(conn, "personnel"):
        conn.execute("ALTER TABLE personnel ADD COLUMN date_depart TEXT")


MIGRATIONS = {1: _migration_1, 2: _migration_2, 3: _migration_3}
VERSION_BASE = max(MIGRATIONS)


def _colonnes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def dt_texte(moment: datetime) -> str:
    return moment.strftime(FORMAT_DATETIME)


def texte_dt(texte: str) -> datetime:
    return datetime.strptime(texte, FORMAT_DATETIME)


def texte_d(texte: str) -> date:
    return date.fromisoformat(texte)


def lire_etat(conn: sqlite3.Connection, cle: str, defaut: str | None = None) -> str | None:
    ligne = conn.execute("SELECT valeur FROM etat WHERE cle = ?", (cle,)).fetchone()
    return ligne["valeur"] if ligne else defaut


def ecrire_etat(conn: sqlite3.Connection, cle: str, valeur: str) -> None:
    """Enregistre une valeur d'état. L'appelant fait le commit."""
    conn.execute(
        "INSERT INTO etat (cle, valeur) VALUES (?, ?) "
        "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
        (cle, valeur),
    )
