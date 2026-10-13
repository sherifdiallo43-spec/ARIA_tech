import json
import sqlite3
from datetime import datetime

import pytest

from pointage import backup, purge
from pointage.collector import synchroniser
from pointage.db import lire_etat
from pointage.device import PointeuseSimulee

LUNDI = datetime(2026, 10, 5)


def a(heure, minute=0):
    return LUNDI.replace(hour=heure, minute=minute)


def pointeuse_avec(*pointages):
    pointeuse = PointeuseSimulee()
    for zk_id, moment in pointages:
        pointeuse.pointer(zk_id, moment)
    return pointeuse


def compter(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_vidage_met_tout_en_securite_puis_efface(conn, config, trois_eleves):
    # Le 3e pointage n'a jamais été lu avant le vidage : il doit être récupéré quand même.
    pointeuse = pointeuse_avec(("1", a(7, 40)), ("1", a(7, 41)), ("2", a(7, 45)))

    resume = purge.vider_memoire(conn, pointeuse, config, maintenant=a(19, 0))

    assert pointeuse.lire_pointages() == []
    assert compter(conn, "pointages") == 2           # le doigt reposé à 7h41 est écarté
    assert resume["lus"] == 3 and resume["nouveaux"] == 2
    brute = json.loads(resume["copie_brute"].read_text(encoding="utf-8"))
    assert len(brute) == 3                           # la copie brute garde tout, doublon compris
    copie = sqlite3.connect(resume["sauvegarde"])
    assert copie.execute("SELECT COUNT(*) FROM pointages").fetchone()[0] == 2
    copie.close()
    assert lire_etat(conn, "memoire_pointeuse") == "0"


def test_rien_n_est_efface_si_la_sauvegarde_echoue(conn, config, trois_eleves, monkeypatch):
    pointeuse = pointeuse_avec(("1", a(7, 40)), ("2", a(7, 45)))

    def sauvegarde_en_panne(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(backup, "sauvegarder", sauvegarde_en_panne)
    with pytest.raises(OSError):
        purge.vider_memoire(conn, pointeuse, config, maintenant=a(19, 0))
    assert len(pointeuse.lire_pointages()) == 2      # mémoire intacte


def test_vidage_automatique_seulement_au_dela_du_seuil(conn, config, trois_eleves):
    config.pointeuse["vider_au_dela"] = 3
    pointeuse = pointeuse_avec(("1", a(7, 40)), ("2", a(7, 45)))
    synchroniser(conn, pointeuse, config, maintenant=a(7, 50))
    assert purge.vider_si_necessaire(conn, pointeuse, config, maintenant=a(19, 0)) is None

    pointeuse.pointer("3", a(7, 55))
    synchroniser(conn, pointeuse, config, maintenant=a(8, 0))
    # Seuil atteint, mais 8h00 est en dehors de la plage autorisée (18h00 - 23h59).
    assert purge.vider_si_necessaire(conn, pointeuse, config, maintenant=a(8, 0)) is None
    assert len(pointeuse.lire_pointages()) == 3
    # Le soir : vidage.
    assert purge.vider_si_necessaire(conn, pointeuse, config, maintenant=a(19, 0)) is not None
    assert pointeuse.lire_pointages() == []
    assert compter(conn, "pointages") == 3


def test_apres_vidage_les_lectures_continuent_normalement(conn, config, trois_eleves):
    pointeuse = pointeuse_avec(("1", a(7, 40)))
    purge.vider_memoire(conn, pointeuse, config, maintenant=a(19, 0))
    pointeuse.pointer("1", LUNDI.replace(day=6, hour=7, minute=38))
    resultat = synchroniser(conn, pointeuse, config, maintenant=LUNDI.replace(day=6, hour=7, minute=40))
    assert resultat.lus == 1 and resultat.nouveaux == 1
    assert compter(conn, "pointages") == 2
