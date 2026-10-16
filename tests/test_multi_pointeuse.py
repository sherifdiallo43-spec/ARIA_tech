"""Tests de MultiPointeuse, avec de fausses connexions qui imitent pyzk.

On ne peut pas tester contre un vrai boîtier ZKTeco ici, mais on peut vérifier
la logique de blocage/déblocage et de fusion, qui est la même quel que soit
le vrai matériel derrière `zk.ZK`.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from pointage.device import MultiPointeuse, PointageBrut, ZKPointeuse


class _Enregistrement:
    def __init__(self, user_id, timestamp):
        self.user_id = user_id
        self.timestamp = timestamp


class ConnexionFactice:
    """Imite l'objet renvoyé par `ZK().connect()`. Journalise l'ordre des appels."""

    def __init__(self, enregistrements=(), echoue_a=None):
        self.enregistrements = list(enregistrements)
        self.echoue_a = echoue_a  # nom de méthode à faire échouer, pour simuler une panne
        self.appels: list[str] = []

    def _tenter(self, nom):
        self.appels.append(nom)
        if nom == self.echoue_a:
            raise RuntimeError(f"panne simulée sur {nom}")

    def disable_device(self):
        self._tenter("disable_device")

    def get_attendance(self):
        self._tenter("get_attendance")
        return self.enregistrements

    def clear_attendance(self):
        self._tenter("clear_attendance")

    def enable_device(self):
        self._tenter("enable_device")

    def disconnect(self):
        self.appels.append("disconnect")


def _patcher_zk(monkeypatch, connexions_par_ip):
    """Remplace `zk.ZK` : connect() renvoie la connexion factice de l'IP demandée."""
    import zk

    class ZKFactice:
        def __init__(self, ip, port=4370, timeout=5, password=0, force_udp=False, ommit_ping=True):
            self._connexion = connexions_par_ip[ip]

        def connect(self):
            return self._connexion

    monkeypatch.setattr(zk, "ZK", ZKFactice)


def test_lire_puis_effacer_bloque_le_boitier_pendant_toute_loperation(monkeypatch):
    """Le boîtier doit rester bloqué (disable_device) de la lecture jusqu'à l'effacement :
    sinon un pointage peut arriver entre les deux et être effacé sans avoir été enregistré."""
    conn = ConnexionFactice([_Enregistrement("1", datetime(2026, 10, 17, 7, 30))])
    _patcher_zk(monkeypatch, {"192.168.1.201": conn})
    multi = MultiPointeuse([ZKPointeuse(ip="192.168.1.201")])

    recus = []
    nb = multi.lire_puis_effacer(lambda bruts: recus.append(bruts))

    assert nb == 1
    assert recus == [[PointageBrut("1", datetime(2026, 10, 17, 7, 30))]]
    # Ordre attendu : bloqué -> lu -> (traiter) -> effacé -> débloqué -> déconnecté.
    assert conn.appels == ["disable_device", "get_attendance", "clear_attendance",
                           "enable_device", "disconnect"]


def test_lire_puis_effacer_ne_vide_pas_si_traiter_echoue(monkeypatch):
    """Si `traiter` échoue (ex. vérification en base ratée), rien n'est effacé,
    mais le boîtier est quand même débloqué."""
    conn = ConnexionFactice([_Enregistrement("1", datetime(2026, 10, 17, 7, 30))])
    _patcher_zk(monkeypatch, {"192.168.1.201": conn})
    multi = MultiPointeuse([ZKPointeuse(ip="192.168.1.201")])

    def traiter_qui_echoue(bruts):
        raise ValueError("vérification échouée")

    with pytest.raises(ValueError):
        multi.lire_puis_effacer(traiter_qui_echoue)

    assert "clear_attendance" not in conn.appels
    assert conn.appels == ["disable_device", "get_attendance", "enable_device", "disconnect"]


def test_lire_puis_effacer_une_pointeuse_en_panne_les_autres_continuent(monkeypatch):
    """Une pointeuse injoignable ne doit ni bloquer les autres, ni empêcher leur vidage."""
    en_panne = ConnexionFactice(echoue_a="get_attendance")
    ok = ConnexionFactice([_Enregistrement("2", datetime(2026, 10, 17, 7, 35))])
    _patcher_zk(monkeypatch, {"192.168.1.201": en_panne, "192.168.1.202": ok})
    multi = MultiPointeuse([ZKPointeuse(ip="192.168.1.201"), ZKPointeuse(ip="192.168.1.202")])

    recus = []
    nb = multi.lire_puis_effacer(lambda bruts: recus.append(bruts))

    assert nb == 1
    assert recus == [[PointageBrut("2", datetime(2026, 10, 17, 7, 35))]]
    # La pointeuse en panne a quand même été débloquée (elle avait été bloquée avant l'échec).
    assert "disable_device" in en_panne.appels
    assert "enable_device" in en_panne.appels
    assert "clear_attendance" not in en_panne.appels
    # La pointeuse qui fonctionne a fait tout le cycle normalement.
    assert ok.appels == ["disable_device", "get_attendance", "clear_attendance",
                         "enable_device", "disconnect"]


def test_lire_pointages_fusionne_plusieurs_boitiers(monkeypatch):
    c1 = ConnexionFactice([_Enregistrement("1", datetime(2026, 10, 17, 7, 30))])
    c2 = ConnexionFactice([_Enregistrement("2", datetime(2026, 10, 17, 7, 32))])
    _patcher_zk(monkeypatch, {"192.168.1.201": c1, "192.168.1.202": c2})
    multi = MultiPointeuse([ZKPointeuse(ip="192.168.1.201"), ZKPointeuse(ip="192.168.1.202")])

    pointages = multi.lire_pointages()

    assert sorted(p.zk_user_id for p in pointages) == ["1", "2"]


def test_lire_pointages_tolere_une_pointeuse_en_panne(monkeypatch):
    en_panne = ConnexionFactice(echoue_a="get_attendance")
    ok = ConnexionFactice([_Enregistrement("2", datetime(2026, 10, 17, 7, 32))])
    _patcher_zk(monkeypatch, {"192.168.1.201": en_panne, "192.168.1.202": ok})
    multi = MultiPointeuse([ZKPointeuse(ip="192.168.1.201"), ZKPointeuse(ip="192.168.1.202")])

    pointages = multi.lire_pointages()

    assert [p.zk_user_id for p in pointages] == ["2"]
