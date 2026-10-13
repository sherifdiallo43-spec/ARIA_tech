from datetime import date, datetime

import pytest

from pointage import eleves
from pointage.collector import synchroniser
from pointage.device import PointeuseSimulee
from pointage.notifier import ErreurDefinitive, ErreurTemporaire, envoyer_en_attente
from pointage.subscriptions import enregistrer_paiement

LUNDI = datetime(2026, 10, 5)


def a(heure, minute=0):
    return LUNDI.replace(hour=heure, minute=minute)


class EnvoyeurMemoire:
    def __init__(self):
        self.envoyes = []

    def envoyer(self, tel, message, variables):
        self.envoyes.append((tel, message))


class EnvoyeurEnPanne:
    def __init__(self, erreur):
        self.erreur = erreur

    def envoyer(self, tel, message, variables):
        raise self.erreur


def statuts(conn):
    return [r[0] for r in conn.execute("SELECT statut FROM notifications ORDER BY id")]


def pointer_et_lire(conn, config, *pointages, maintenant):
    pointeuse = PointeuseSimulee()
    for zk_id, moment in pointages:
        pointeuse.pointer(zk_id, moment)
    synchroniser(conn, pointeuse, config, maintenant=maintenant)


def test_message_envoye_au_parent(conn, config, trois_eleves):
    pointer_et_lire(conn, config, ("1", a(8, 12)), maintenant=a(8, 13))
    envoyeur = EnvoyeurMemoire()
    envoyer_en_attente(conn, envoyeur, config, maintenant=a(8, 13))
    assert envoyeur.envoyes == [
        ("+221771234567", "Groupe Scolaire Exemple : Awa Test (6e A) "
                          "est arrivé(e) à l'école (en retard) à 08h12.")
    ]
    assert statuts(conn) == ["envoye"]


def test_pas_d_envoi_si_abonnement_expire(conn, config):
    # Inscrit le 1er août : essai terminé le 31 août, rien payé depuis.
    eleves.inscrire(conn, "7", "Sow", "Mariama", "5e A", "771112233", date(2026, 8, 1), 30)
    pointer_et_lire(conn, config, ("7", a(7, 40)), maintenant=a(7, 41))
    envoyeur = EnvoyeurMemoire()
    envoyer_en_attente(conn, envoyeur, config, maintenant=a(7, 41))
    assert envoyeur.envoyes == []
    assert statuts(conn) == ["ignore_abonnement"]


def test_envoi_reprend_si_abonnement_paye(conn, config):
    eleves.inscrire(conn, "7", "Sow", "Mariama", "5e A", "771112233", date(2026, 8, 1), 30)
    enregistrer_paiement(conn, "7", 1, 1000, aujourd_hui=date(2026, 10, 2))
    pointer_et_lire(conn, config, ("7", a(7, 40)), maintenant=a(7, 41))
    assert statuts(conn) == ["en_attente"]


def test_file_d_attente_survit_a_une_coupure_internet(conn, config, trois_eleves):
    pointer_et_lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(7, 46))

    compte = envoyer_en_attente(conn, EnvoyeurEnPanne(ErreurTemporaire("pas d'internet")),
                                config, maintenant=a(7, 46))
    assert compte["reporte"] == 1                      # on s'arrête au premier échec
    assert statuts(conn) == ["en_attente", "en_attente"]

    envoyeur = EnvoyeurMemoire()                       # internet revient
    envoyer_en_attente(conn, envoyeur, config, maintenant=a(8, 0))
    assert len(envoyeur.envoyes) == 2
    assert statuts(conn) == ["envoye", "envoye"]


def test_message_perime_apres_longue_coupure(conn, config, trois_eleves):
    pointer_et_lire(conn, config, ("1", a(7, 40)), maintenant=a(7, 41))
    envoyeur = EnvoyeurMemoire()
    envoyer_en_attente(conn, envoyeur, config, maintenant=a(11, 0))  # 3 h 20 plus tard
    assert envoyeur.envoyes == []
    assert statuts(conn) == ["ignore_trop_ancien"]


def test_pointage_recupere_trop_tard_non_notifie(conn, config, trois_eleves):
    pointer_et_lire(conn, config, ("1", a(7, 40)), maintenant=a(15, 0))
    assert statuts(conn) == ["ignore_trop_ancien"]


def test_numero_invalide_abandonne_sans_bloquer_la_file(conn, config, trois_eleves):
    pointer_et_lire(conn, config, ("1", a(7, 40)), maintenant=a(7, 41))
    envoyer_en_attente(conn, EnvoyeurEnPanne(ErreurDefinitive("numéro invalide")),
                       config, maintenant=a(7, 41))
    assert statuts(conn) == ["echec"]


@pytest.mark.parametrize("brut", ["77 123 45 67", "771234567", "00221771234567",
                                  "221 77 123 45 67", "+221 77-123-45-67"])
def test_normalisation_telephone(brut):
    assert eleves.normaliser_telephone(brut) == "+221771234567"


def test_telephone_invalide_refuse():
    with pytest.raises(ValueError):
        eleves.normaliser_telephone("123")
