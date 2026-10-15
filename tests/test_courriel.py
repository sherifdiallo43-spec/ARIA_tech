from datetime import date

import pytest

from pointage import courriel, reports


class EnvoyeurFactice:
    def __init__(self, en_panne=False):
        self.envois, self.en_panne = [], en_panne

    def envoyer(self, destinataire, sujet, corps, piece_jointe=None):
        if self.en_panne:
            raise OSError("pas d'internet")
        self.envois.append((destinataire, sujet, piece_jointe.name))


@pytest.fixture
def pdf(config):
    chemin = reports.chemin_personnel_jour(config, date(2026, 10, 5))
    chemin.parent.mkdir(parents=True)
    chemin.write_bytes(b"%PDF-factice")
    return [(chemin, "lundi 5 octobre 2026")]


def test_envoi_une_seule_fois(conn, config, pdf):
    config.directeur["email"] = "directeur@ecole.sn"
    envoyeur = EnvoyeurFactice()
    assert courriel.envoyer_en_attente(conn, config, envoyeur, pdf) == 1
    assert courriel.envoyer_en_attente(conn, config, envoyeur, pdf) == 0
    assert envoyeur.envois == [("directeur@ecole.sn", "Groupe Scolaire Exemple : rapport du personnel, "
                                "lundi 5 octobre 2026", "personnel_2026-10-05.pdf")]


def test_reessai_apres_coupure_internet(conn, config, pdf):
    config.directeur["email"] = "directeur@ecole.sn"
    assert courriel.envoyer_en_attente(conn, config, EnvoyeurFactice(en_panne=True), pdf) == 0
    assert courriel.envoyer_en_attente(conn, config, EnvoyeurFactice(), pdf) == 1


def test_sans_adresse_rien_ne_part(conn, config, pdf):
    config.directeur["email"] = ""
    envoyeur = EnvoyeurFactice()
    assert courriel.envoyer_en_attente(conn, config, envoyeur, pdf) == 0 and envoyeur.envois == []


def test_mode_smtp_exige_les_identifiants(config):
    config.directeur["mode"] = "smtp"
    with pytest.raises(ValueError, match="SMTP_UTILISATEUR"):
        courriel.creer_envoyeur(config)
