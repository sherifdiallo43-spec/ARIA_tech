import argparse

import pytest

import main
from pointage import notifier


class Recorder:
    def __init__(self, erreur=None):
        self.envois, self.erreur = [], erreur

    def envoyer(self, tel, message, variables):
        if self.erreur:
            raise self.erreur
        self.envois.append((tel, message))


def test_message_de_test_envoye_au_numero_normalise(config, monkeypatch, capsys):
    envoyeur = Recorder()
    monkeypatch.setattr(notifier, "creer_envoyeur", lambda c: envoyeur)
    main.cmd_test_whatsapp(config, argparse.Namespace(numero="76 500 08 43"))
    assert envoyeur.envois[0][0] == "+221765000843"
    assert "Message envoyé" in capsys.readouterr().out


def test_echec_explique_le_bac_a_sable(config, monkeypatch):
    monkeypatch.setattr(notifier, "creer_envoyeur",
                        lambda c: Recorder(notifier.ErreurDefinitive("Twilio 63015")))
    with pytest.raises(SystemExit) as erreur:
        main.cmd_test_whatsapp(config, argparse.Namespace(numero="765000843"))
    assert "join" in str(erreur.value)
