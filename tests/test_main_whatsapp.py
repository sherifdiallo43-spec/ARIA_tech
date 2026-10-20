import argparse

import pytest

import main
from pointage import notifier


class Recorder:
    def __init__(self, erreur=None):
        self.envois, self.modeles, self.erreur = [], [], erreur

    def envoyer(self, tel, message, variables):
        if self.erreur:
            raise self.erreur
        self.envois.append((tel, message, variables))

    def envoyer_modele(self, tel, nom, langue, parametres=()):
        self.modeles.append((tel, nom, langue))


def lancer(config, numero="76 500 08 43", hello=False):
    main.cmd_test_whatsapp(config, argparse.Namespace(numero=numero, hello=hello))


def test_alerte_de_test_envoyee_au_numero_normalise(config, monkeypatch, capsys):
    envoyeur = Recorder()
    monkeypatch.setattr(notifier, "creer_envoyeur", lambda c: envoyeur)
    lancer(config)
    tel, message, variables = envoyeur.envois[0]
    assert tel == "+221765000843" and variables["modele"] == "arrivee"
    assert "Message envoyé" in capsys.readouterr().out


def test_hello_world_verifie_le_compte(config, monkeypatch):
    envoyeur = Recorder()
    monkeypatch.setattr(notifier, "creer_envoyeur", lambda c: envoyeur)
    lancer(config, hello=True)
    assert envoyeur.modeles == [("+221765000843", "hello_world", "en_US")]


def test_echec_explique_le_numero_de_test(config, monkeypatch):
    monkeypatch.setattr(notifier, "creer_envoyeur",
                        lambda c: Recorder(notifier.ErreurDefinitive("Meta 131030 : not in allowed list")))
    with pytest.raises(SystemExit) as erreur:
        lancer(config)
    assert "API Setup" in str(erreur.value)


def test_pas_de_conseil_pour_une_autre_erreur(config, monkeypatch):
    monkeypatch.setattr(notifier, "creer_envoyeur",
                        lambda c: Recorder(notifier.ErreurDefinitive("Meta 100 : Invalid parameter")))
    with pytest.raises(SystemExit) as erreur:
        lancer(config)
    assert str(erreur.value) == "Échec de l'envoi à +221765000843 : Meta 100 : Invalid parameter"


def test_jeton_manquant_message_clair(config):
    with pytest.raises(SystemExit) as erreur:
        lancer(config)
    assert "META_JETON" in str(erreur.value)
