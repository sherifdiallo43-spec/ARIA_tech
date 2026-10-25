"""Personnel qui quitte l'école : il sort du rapport du directeur, sans fausser le passé.

Sans cela, les cours d'un prof parti apparaîtraient « manqués » chaque jour dans le
rapport confidentiel. Et pour le mois de son départ, ses heures doivent rester
visibles (elles servent à la paie).
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date

import pytest
from zk.finger import Finger
from zk.user import User

import main
from pointage import db, personnel
from test_personnel import INSCRIPTION, LUNDI, a, profs, synchro  # noqa: F401  (profs : fixture)
from test_rentree import BoitierAvecEffacement, _patcher_zk

LUNDI_SUIVANT = date(2026, 10, 12)
DEPART_901 = date(2026, 10, 10)  # Aïssatou Sy (901) part le samedi 10 octobre


def noms(lignes):
    return [m["nom"] for m in lignes]


def membre(conn, zk_id):
    return conn.execute("SELECT * FROM personnel WHERE zk_user_id = ?", (zk_id,)).fetchone()


def test_le_rapport_du_jour_ne_compte_plus_le_prof_parti(conn, config, profs):
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    conn.commit()
    assert "Aïssatou Sy" in noms(personnel.calculer_jour(conn, LUNDI, 5))  # avant son départ
    apres = personnel.calculer_jour(conn, LUNDI_SUIVANT, 5)
    assert "Aïssatou Sy" not in noms(apres)
    assert "Mamadou Ndiaye" in noms(apres)


def test_le_mois_du_depart_garde_ses_heures_sans_absences_apres(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 55)), ("900", a(LUNDI, 17, 5)),
            ("901", a(LUNDI, 9, 55)), ("901", a(LUNDI, 12, 5)),
            ("1", a(LUNDI_SUIVANT, 7, 40)))  # le 12, l'école est ouverte (un élève a pointé)
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    conn.commit()

    lignes = {m["nom"]: m for m in personnel.calculer_mois(conn, 2026, 10, 5, aujourd_hui=date(2026, 10, 31))}
    sy = lignes["Aïssatou Sy"]
    assert (sy["prevus"], sy["absences"], sy["minutes"]) == (1, 0, 130)  # le 12 ne compte pas
    assert sy["fonction"] == "Prof de français (parti(e) depuis le 10/10)"
    assert lignes["Mamadou Ndiaye"]["absences"] == 1  # lui est toujours là : absent le 12


def test_le_mois_suivant_il_n_apparait_plus(conn, config, profs):
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    conn.commit()
    assert "Aïssatou Sy" not in noms(personnel.calculer_mois(conn, 2026, 11, 5, aujourd_hui=date(2026, 11, 30)))


def test_le_numero_d_un_prof_parti_n_est_pas_redonne(conn, config, profs):
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    with pytest.raises(ValueError, match="était celui de Aïssatou Sy"):
        personnel.inscrire(conn, "901", "Diouf", "Omar", "Prof d'anglais", LUNDI_SUIVANT)


def test_un_prof_qui_revient_est_reactive(conn, config, profs):
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    personnel.inscrire(conn, "901", "SY", "Aissatou", "Prof de français", LUNDI_SUIVANT)
    assert (membre(conn, "901")["actif"], membre(conn, "901")["date_depart"]) == (1, None)


def test_commande_personnel_parti(conn, config, profs, capsys):
    main.cmd_personnel_parti(config, argparse.Namespace(zk_ids=["901"], date="2026-10-10"))
    main.cmd_personnel_parti(config, argparse.Namespace(zk_ids=["901"], date="2026-10-20"))
    sortie = capsys.readouterr().out
    assert "Prof de français Sy Aïssatou (n° 901) : parti(e) à partir du 10/10/2026" in sortie
    assert "déjà parti(e) le 10/10/2026" in sortie
    assert membre(conn, "901")["date_depart"] == "2026-10-10"
    with pytest.raises(ValueError, match="555"):
        main.cmd_personnel_parti(config, argparse.Namespace(zk_ids=["555"], date=None))


def test_nettoyer_retire_aussi_le_personnel_parti(conn, config, profs, monkeypatch):
    boitier = BoitierAvecEffacement(
        utilisateurs=[User(1, "Awa", 0, "", "", "1"), User(2, "Mamadou", 0, "", "", "900"),
                      User(3, "Aissatou", 0, "", "", "901")],
        empreintes=[Finger(1, 0, 1, b"a"), Finger(2, 0, 1, b"m"), Finger(3, 0, 1, b"s")])
    _patcher_zk(monkeypatch, {"10.0.0.1": boitier})
    config.pointeuse["mode"] = "zk"
    config.pointeuses = [{"ip": "10.0.0.1"}]
    personnel.marquer_partis(conn, ["901"], DEPART_901)
    conn.commit()

    main.cmd_nettoyer_boitier(config, argparse.Namespace(oui=True))
    assert boitier.numeros() == ["1", "900"]


def test_une_base_en_version_2_recoit_la_date_de_depart_du_personnel(tmp_path):
    ancienne = sqlite3.connect(tmp_path / "v2.db")
    ancienne.executescript(db.SCHEMA.replace(
        "    actif            INTEGER NOT NULL DEFAULT 1,\n"
        "    date_depart      TEXT     -- premier jour où il ne fait plus partie du personnel "
        "(NULL = toujours là)\n",
        "    actif            INTEGER NOT NULL DEFAULT 1\n"))
    ancienne.execute("INSERT INTO personnel (zk_user_id, nom, prenom, fonction, date_inscription) "
                     "VALUES ('900', 'Ndiaye', 'Mamadou', 'Prof', '2026-10-01')")
    ancienne.execute("PRAGMA user_version = 2")
    ancienne.commit()
    assert "date_depart" not in {r[1] for r in ancienne.execute("PRAGMA table_info(personnel)")}
    ancienne.close()

    conn = db.connecter(tmp_path / "v2.db")
    db.initialiser(conn)
    assert db.version_base(conn) == 3
    assert membre(conn, "900")["date_depart"] is None
    conn.close()
