from datetime import date

import pytest

from pointage.subscriptions import ajouter_mois, enregistrer_paiement, statut_abonnement


def eleve(conn, zk_id="1"):
    return conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_id,)).fetchone()


def test_ajouter_mois_reste_sur_un_jour_valide():
    assert ajouter_mois(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert ajouter_mois(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_essai_puis_expiration(conn, trois_eleves):
    assert statut_abonnement(eleve(conn), date(2026, 10, 31)) == "essai"
    assert statut_abonnement(eleve(conn), date(2026, 11, 1)) == "expire"


def test_payer_pendant_l_essai_ne_fait_pas_perdre_de_jours(conn, trois_eleves):
    fin = enregistrer_paiement(conn, "1", 1, 1000, aujourd_hui=date(2026, 10, 15))
    assert fin == date(2026, 11, 30)  # l'essai finit le 31/10, puis un mois payé
    assert statut_abonnement(eleve(conn), date(2026, 11, 20)) == "actif"


def test_payer_apres_expiration_repart_d_aujourd_hui(conn, trois_eleves):
    fin = enregistrer_paiement(conn, "1", 2, 1000, aujourd_hui=date(2027, 1, 10))
    assert fin == date(2027, 3, 9)
    montant = conn.execute("SELECT montant FROM paiements").fetchone()[0]
    assert montant == 2000


def test_paiements_successifs_s_additionnent(conn, trois_eleves):
    enregistrer_paiement(conn, "1", 1, 1000, aujourd_hui=date(2026, 10, 15))
    fin = enregistrer_paiement(conn, "1", 1, 1000, aujourd_hui=date(2026, 10, 20))
    assert fin == date(2026, 12, 31)


def test_eleve_inconnu_refuse(conn, trois_eleves):
    with pytest.raises(ValueError):
        enregistrer_paiement(conn, "999", 1, 1000, aujourd_hui=date(2026, 10, 15))
