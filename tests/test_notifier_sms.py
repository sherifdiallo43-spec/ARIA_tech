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
