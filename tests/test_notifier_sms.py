from unittest.mock import patch

import pytest

from pointage import notifier


def test_sms_envoye_en_texte_libre_sans_prefixe_whatsapp():
    with patch("twilio.rest.Client") as Client:
        envoyeur = notifier.EnvoyeurTwilioSMS("AC" + "0" * 32, "t" * 32, "+17372508034")
        envoyeur.envoyer("+221765000843", "Awa est arrivée.", {})
    Client.return_value.messages.create.assert_called_once_with(
        from_="+17372508034", to="+221765000843", body="Awa est arrivée.")


def test_creer_envoyeur_sms_exige_le_numero_expediteur(config):
    config.notifications["mode"] = "twilio_sms"
    config.secrets.update(TWILIO_ACCOUNT_SID="AC" + "0" * 32, TWILIO_AUTH_TOKEN="t" * 32)
    with pytest.raises(ValueError, match="TWILIO_SMS_FROM"):
        notifier.creer_envoyeur(config)


def test_cle_api_de_l_ecole_preferee_au_jeton_principal(config):
    """Avec une clé d'API, le jeton principal du compte n'est ni requis ni utilisé."""
    config.notifications["mode"] = "twilio_sms"
    config.secrets.update(TWILIO_ACCOUNT_SID="AC" + "0" * 32, TWILIO_SMS_FROM="+17372508034",
                          TWILIO_API_KEY_SID="SK" + "1" * 32, TWILIO_API_KEY_SECRET="s" * 32)
    with patch("twilio.rest.Client") as Client:
        notifier.creer_envoyeur(config)
    Client.assert_called_once_with("SK" + "1" * 32, "s" * 32, "AC" + "0" * 32)


def test_sans_cle_ni_jeton_l_envoi_est_refuse(config):
    config.notifications["mode"] = "twilio"
    config.secrets.update(TWILIO_ACCOUNT_SID="AC" + "0" * 32, TWILIO_WHATSAPP_FROM="+14155238886")
    with pytest.raises(ValueError, match="TWILIO_API_KEY_SID"):
        notifier.creer_envoyeur(config)
