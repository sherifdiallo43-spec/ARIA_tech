"""École au forfait : l'école paie pour tous ses élèves, aucun paiement par parent.

Le danger évité ici : avec la règle « un parent doit avoir payé », tous les
messages s'arrêteraient en silence à la fin de l'essai gratuit.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import pytest

import main
from pointage import eleves, reports
from pointage.collector import synchroniser
from pointage.device import PointeuseSimulee
from pointage.notifier import creer_alertes_absence
from pointage.subscriptions import alertes_autorisees

DECEMBRE = datetime(2026, 12, 7)  # lundi, bien après la fin de l'essai gratuit (31/10)


def a(heure, minute=0):
    return DECEMBRE.replace(hour=heure, minute=minute)


def forfait(config):
    config.abonnement["paiement"] = "forfait"
    return config


def lire(conn, config, *pointages, maintenant):
    pointeuse = PointeuseSimulee()
    for zk_id, moment in pointages:
        pointeuse.pointer(zk_id, moment)
    synchroniser(conn, pointeuse, config, maintenant=maintenant)


def statuts(conn):
    return [r[0] for r in conn.execute("SELECT statut FROM notifications ORDER BY id")]


def test_sans_forfait_les_messages_s_arretent_apres_l_essai(conn, config, trois_eleves):
    """Le comportement « les parents paient » : rien payé, plus de message en décembre."""
    lire(conn, config, ("1", a(7, 40)), maintenant=a(7, 41))
    assert statuts(conn) == ["ignore_abonnement"]


def test_au_forfait_les_messages_continuent_sans_paiement_des_parents(conn, config, trois_eleves):
    forfait(config)
    lire(conn, config, ("1", a(7, 40)), maintenant=a(7, 41))
    assert statuts(conn) == ["en_attente"]


def test_au_forfait_les_alertes_d_absence_partent_aussi(conn, config, trois_eleves):
    forfait(config)
    config.notifications["types_notifies"] = ["retard", "absence"]
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))
    creer_alertes_absence(conn, config, maintenant=a(9, 0))
    assert statuts(conn) == ["en_attente"]


def test_au_forfait_aucune_limite_de_date(conn, trois_eleves):
    eleve = conn.execute("SELECT * FROM eleves WHERE zk_user_id = '1'").fetchone()
    assert alertes_autorisees(eleve, date(2027, 6, 30), {"paiement": "forfait"})


def test_mode_de_paiement_mal_ecrit_refuse(conn, trois_eleves):
    eleve = conn.execute("SELECT * FROM eleves WHERE zk_user_id = '1'").fetchone()
    with pytest.raises(ValueError, match="forfait"):
        alertes_autorisees(eleve, date(2026, 12, 7), {"paiement": "forfai"})


def test_au_forfait_pas_de_releve_des_parents_a_relancer(conn, config, trois_eleves):
    forfait(config)
    lire(conn, config, ("1", datetime(2026, 11, 9, 7, 40)), maintenant=datetime(2026, 11, 9, 7, 41))
    crees = reports.rapports_automatiques(conn, config, maintenant=datetime(2026, 12, 1, 8, 0))
    assert not any("releve" in p.name for p in crees)
    assert any(p.name.startswith("rapport") for p in crees)  # le rapport de l'école, lui, est fait


def test_au_forfait_la_commande_paiement_est_refusee(conn, config, trois_eleves):
    forfait(config)
    with pytest.raises(SystemExit, match="forfait"):
        main.cmd_paiement(config, argparse.Namespace(zk_id="1", mois=1, montant=None, note=None))


def test_statut_affiche_le_forfait(conn, config, trois_eleves, capsys, monkeypatch):
    forfait(config)
    monkeypatch.setattr(main, "ouvrir_base", lambda c: conn)
    main.cmd_statut(config, argparse.Namespace())
    assert "forfait payé par l'école" in capsys.readouterr().out
