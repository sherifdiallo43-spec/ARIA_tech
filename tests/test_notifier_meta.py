"""Envoi WhatsApp par l'API Cloud de Meta. Aucun vrai message : urlopen est remplacé."""
import io
import json
import urllib.error
from datetime import datetime
from unittest.mock import patch

import pytest

from pointage import notifier
from pointage.notifier import EnvoyeurMeta, ErreurDefinitive, ErreurTemporaire

ELEVE = {"prenom": "Awa", "nom": "Diop", "classe": "6e A"}


class FauxMeta:
    """Remplace urllib.request.urlopen : garde les requêtes, répond OK ou par une erreur."""

    def __init__(self, erreur=None):
        self.requetes, self.erreur = [], erreur

    def __call__(self, requete, timeout):
        self.requetes.append(requete)
        if self.erreur:
            raise self.erreur
        return io.BytesIO(b'{"messages": [{"id": "wamid.X"}]}')

    def corps(self, i=0):
        return json.loads(self.requetes[i].data.decode("utf-8"))


def erreur_http(statut, code, details="détail"):
    corps = json.dumps({"error": {"code": code, "message": "msg", "error_data": {"details": details}}})
    return urllib.error.HTTPError("url", statut, "erreur", {}, io.BytesIO(corps.encode()))


def envoyer(envoyeur, type_="arrivee", en_retard=False):
    message, variables = notifier.composer_message(ELEVE, type_, datetime(2026, 10, 5, 7, 42),
                                                   en_retard, "GS Exemple")
    envoyeur.envoyer("+221765000843", message, variables)


def test_alerte_envoyee_avec_son_modele_approuve():
    faux = FauxMeta()
    with patch("urllib.request.urlopen", faux):
        envoyer(EnvoyeurMeta("jeton-secret", "1234567890"))
    requete = faux.requetes[0]
    assert requete.full_url == "https://graph.facebook.com/v25.0/1234567890/messages"
    assert requete.get_header("Authorization") == "Bearer jeton-secret"
    assert faux.corps() == {
        "messaging_product": "whatsapp", "to": "221765000843", "type": "template",
        "template": {"name": "aria_arrivee", "language": {"code": "fr"}, "components": [
            {"type": "body", "parameters": [{"type": "text", "text": "GS Exemple"},
                                            {"type": "text", "text": "Awa Diop (6e A)"},
                                            {"type": "text", "text": "07h42"}]}]}}


@pytest.mark.parametrize("type_, en_retard, modele", [
    ("arrivee", True, "aria_retard"), ("sortie", False, "aria_sortie"), ("absence", False, "aria_absence")])
def test_chaque_type_d_alerte_a_son_modele(type_, en_retard, modele):
    faux = FauxMeta()
    with patch("urllib.request.urlopen", faux):
        envoyer(EnvoyeurMeta("j", "1"), type_, en_retard)
    assert faux.corps()["template"]["name"] == modele


def test_texte_libre_pour_les_demos():
    faux = FauxMeta()
    with patch("urllib.request.urlopen", faux):
        envoyer(EnvoyeurMeta("j", "1", texte_libre=True))
    assert faux.corps()["type"] == "text"
    assert faux.corps()["text"]["body"] == "GS Exemple : Awa Diop (6e A) est arrivé(e) à l'école à 07h42."


def test_ancien_message_sans_modele_abandonne_proprement():
    """Message rangé avant le passage à Meta (variables au format Twilio)."""
    with patch("urllib.request.urlopen", FauxMeta()), pytest.raises(ErreurDefinitive, match="modeles_meta"):
        EnvoyeurMeta("j", "1").envoyer("+221765000843", "texte", {"1": "a", "2": "b", "3": "c", "4": "d"})


@pytest.mark.parametrize("erreur, attendue", [
    (erreur_http(400, 132001, "template name does not exist"), ErreurDefinitive),  # modèle inconnu
    (erreur_http(400, 131030, "not in allowed list"), ErreurDefinitive),           # numéro de test
    (erreur_http(400, 130429), ErreurTemporaire),                                  # débit dépassé
    (erreur_http(401, 190), ErreurTemporaire),                                     # jeton expiré
    (erreur_http(503, 1), ErreurTemporaire),                                       # panne Meta
    (urllib.error.HTTPError("url", 502, "x", {}, io.BytesIO(b"<html>")), ErreurTemporaire),
    (urllib.error.URLError("getaddrinfo failed"), ErreurTemporaire),               # pas d'internet
    (TimeoutError("timed out"), ErreurTemporaire),
])
def test_erreurs_classees(erreur, attendue):
    with patch("urllib.request.urlopen", FauxMeta(erreur)), pytest.raises(attendue):
        envoyer(EnvoyeurMeta("j", "1"))


def test_message_d_erreur_lisible_sans_le_jeton():
    with patch("urllib.request.urlopen", FauxMeta(erreur_http(400, 132001, "modèle inconnu"))):
        with pytest.raises(ErreurDefinitive) as e:
            envoyer(EnvoyeurMeta("jeton-secret", "1"))
    assert str(e.value) == "Meta 132001 : modèle inconnu" and "jeton-secret" not in str(e.value)


def test_creer_envoyeur_exige_jeton_et_numero(config):
    config.notifications["mode"] = "meta"
    config.secrets.update(META_JETON="j")
    with pytest.raises(ValueError, match="META_ID_NUMERO"):
        notifier.creer_envoyeur(config)


def test_creer_envoyeur_lit_config_toml_et_env(config):
    config.notifications.update(mode="meta", texte_libre=True, modeles_meta={"arrivee": "mon_modele"})
    config.secrets.update(META_JETON="j", META_ID_NUMERO="42", META_API_VERSION="v26.0")
    envoyeur = notifier.creer_envoyeur(config)
    assert envoyeur.url == "https://graph.facebook.com/v26.0/42/messages"
    assert envoyeur.texte_libre and envoyeur.modeles["arrivee"] == "mon_modele"
    assert envoyeur.modeles["absence"] == "aria_absence"  # les autres gardent leur nom par défaut


def test_ancien_mode_twilio_explique(config):
    config.notifications["mode"] = "twilio"
    with pytest.raises(ValueError, match="meta"):
        notifier.creer_envoyeur(config)
