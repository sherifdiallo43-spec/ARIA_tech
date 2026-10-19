"""Tests de la copie des empreintes d'un boîtier vers un autre, avec un faux boîtier
qui imite pyzk (fiches utilisateur, modèles d'empreintes, capacités).

On ne peut pas vérifier ici qu'un vrai F18 reconnaît un doigt copié : ça, il faudra
le tester sur le vrai matériel. On vérifie la logique : numéros internes, boîtier
neuf, place disponible, déblocage en cas de panne.
"""
from __future__ import annotations

import pytest
from zk.finger import Finger
from zk.user import User

from pointage.device import CarnetEmpreintes, FicheEmpreintes, ZKPointeuse


class BoitierFactice:
    """Imite la connexion pyzk d'un boîtier : des fiches, des empreintes, une capacité."""

    def __init__(self, utilisateurs=(), empreintes=(), taille_fiche=72,
                 users_cap=3000, fingers_cap=3000, echoue_apres=None):
        self.utilisateurs = {u.uid: u for u in utilisateurs}
        self.empreintes = {(f.uid, f.fid): f for f in empreintes}
        # pyzk ne connaît le format des fiches qu'en lisant des fiches existantes ;
        # sur un boîtier vide, il garde son défaut (28) même si le boîtier attend 72.
        self.taille_reelle = taille_fiche
        self.user_packet_size = taille_fiche if utilisateurs else 28
        self.users_cap, self.fingers_cap = users_cap, fingers_cap
        self.echoue_apres = echoue_apres  # nombre d'écritures avant une panne simulée
        self.appels: list[str] = []

    def get_users(self):
        return list(self.utilisateurs.values())

    def get_templates(self):
        return list(self.empreintes.values())

    def read_sizes(self):
        self.users, self.fingers = len(self.utilisateurs), len(self.empreintes)

    def save_user_template(self, user, fingers):
        if self.echoue_apres is not None and len(self.utilisateurs) >= self.echoue_apres:
            raise RuntimeError("panne simulée")
        assert self.user_packet_size == self.taille_reelle, "fiche écrite dans le mauvais format"
        self.appels.append("save_user_template")
        self.utilisateurs[user.uid] = user
        for f in fingers:
            self.empreintes[(user.uid, f.fid)] = Finger(user.uid, f.fid, f.valid, f.template)

    def disable_device(self):
        self.appels.append("disable_device")

    def enable_device(self):
        self.appels.append("enable_device")

    def disconnect(self):
        pass

    def par_numero(self, zk_user_id):
        return next(u for u in self.utilisateurs.values() if u.user_id == zk_user_id)

    def doigts_de(self, zk_user_id):
        uid = self.par_numero(zk_user_id).uid
        return sorted((f.fid, f.template) for f in self.empreintes.values() if f.uid == uid)


def _patcher_zk(monkeypatch, boitiers_par_ip):
    import zk

    class ZKFactice:
        def __init__(self, ip, **_):
            self._boitier = boitiers_par_ip[ip]

        def connect(self):
            return self._boitier

    monkeypatch.setattr(zk, "ZK", ZKFactice)


def _source():
    """Boîtier de l'entrée principale : 2 élèves (2 doigts chacun) et 1 prof."""
    return BoitierFactice(
        utilisateurs=[User(1, "Awa Diop", 0, "", "", "1"),
                      User(2, "NN-2", 0, "", "", "2"),
                      User(3, "Mamadou Ndiaye", 0, "", "", "900")],
        empreintes=[Finger(1, 0, 1, b"awa-pouce"), Finger(1, 1, 1, b"awa-index"),
                    Finger(2, 0, 1, b"moussa-pouce"), Finger(2, 6, 1, b"moussa-index-g"),
                    Finger(3, 0, 1, b"prof-pouce")])


def _copier(monkeypatch, source, cible):
    _patcher_zk(monkeypatch, {"10.0.0.1": source, "10.0.0.2": cible})
    carnet = ZKPointeuse("10.0.0.1").lire_empreintes()
    ZKPointeuse("10.0.0.2").ecrire_empreintes(carnet)
    return carnet


def test_lecture_regroupe_les_empreintes_par_personne(monkeypatch):
    _patcher_zk(monkeypatch, {"10.0.0.1": _source()})
    carnet = ZKPointeuse("10.0.0.1").lire_empreintes()

    assert [f.zk_user_id for f in carnet.fiches] == ["1", "2", "900"]
    assert carnet.nb_doigts == 5
    assert carnet.fiches[0].doigts == ((0, 1, b"awa-pouce"), (1, 1, b"awa-index"))
    assert carnet.fiches[1].nom == ""  # le "NN-2" inventé par pyzk n'est pas recopié comme un vrai nom


def test_copie_vers_un_boitier_neuf(monkeypatch):
    """Cas réel de l'installation : on achète un 2e boîtier, vide, du même modèle."""
    source, neuf = _source(), BoitierFactice(taille_fiche=72)
    _copier(monkeypatch, source, neuf)  # le faux boîtier refuse une fiche au mauvais format

    for numero in ("1", "2", "900"):
        assert neuf.doigts_de(numero) == source.doigts_de(numero)
    assert neuf.par_numero("1").name == "Awa Diop"
    assert neuf.appels[0] == "disable_device" and neuf.appels[-1] == "enable_device"


def test_les_numeros_internes_du_boitier_cible_sont_respectes(monkeypatch):
    """Sur la cible, uid 1 est déjà pris par le prof 900. Recopier l'élève 1 avec l'uid 1
    de la source écraserait le prof : chacun doit garder sa propre fiche."""
    cible = BoitierFactice(utilisateurs=[User(1, "Mamadou Ndiaye", 0, "", "", "900")],
                           empreintes=[Finger(1, 0, 1, b"ancien-pouce-prof")])
    _copier(monkeypatch, _source(), cible)

    assert cible.par_numero("900").uid == 1        # le prof garde sa fiche...
    assert cible.doigts_de("900") == [(0, b"prof-pouce")]  # ...mise à jour avec l'empreinte de la source
    assert {cible.par_numero("1").uid, cible.par_numero("2").uid} == {2, 3}
    assert len(cible.utilisateurs) == 3


def test_les_personnes_absentes_de_la_source_ne_sont_pas_touchees(monkeypatch):
    cible = BoitierFactice(utilisateurs=[User(7, "Gardien", 0, "", "", "950")],
                           empreintes=[Finger(7, 0, 1, b"gardien-pouce")])
    _copier(monkeypatch, _source(), cible)

    assert cible.doigts_de("950") == [(0, b"gardien-pouce")]


def test_recopier_une_deuxieme_fois_ne_cree_pas_de_doublon(monkeypatch):
    source, cible = _source(), BoitierFactice(taille_fiche=72)
    _copier(monkeypatch, source, cible)
    _copier(monkeypatch, source, cible)

    assert len(cible.utilisateurs) == 3
    assert len(cible.empreintes) == 5


def test_boitier_trop_petit_refuse_avant_decrire(monkeypatch):
    cible = BoitierFactice(taille_fiche=72, fingers_cap=4)
    with pytest.raises(ValueError, match="pleine"):
        _copier(monkeypatch, _source(), cible)
    assert cible.utilisateurs == {}
    assert "disable_device" not in cible.appels  # refusé avant même de bloquer le boîtier


def test_recopie_sur_boitier_presque_plein_acceptee(monkeypatch):
    """Les personnes déjà présentes ne prennent pas de place en plus."""
    source, cible = _source(), BoitierFactice(taille_fiche=72, users_cap=3, fingers_cap=5)
    _copier(monkeypatch, source, cible)
    _copier(monkeypatch, source, cible)  # ne doit pas lever « pleine »


def test_boitier_debloque_meme_si_la_copie_plante(monkeypatch):
    """Un boîtier qui resterait bloqué empêcherait tous les élèves de pointer."""
    cible = BoitierFactice(taille_fiche=72, echoue_apres=1)
    with pytest.raises(RuntimeError):
        _copier(monkeypatch, _source(), cible)
    assert cible.appels[-1] == "enable_device"


def test_incompletes_repere_les_personnes_et_doigts_manquants():
    def fiche(numero, nb_doigts):
        return FicheEmpreintes(numero, "", 0, "", "", 0,
                               tuple((i, 1, b"x") for i in range(nb_doigts)))

    source = CarnetEmpreintes([fiche("1", 2), fiche("2", 2), fiche("3", 1)], 72)
    copie = CarnetEmpreintes([fiche("1", 2), fiche("2", 1)], 72)

    assert source.incompletes_dans(copie) == ["2", "3"]
    assert source.incompletes_dans(source) == []


# --- La commande `copier-empreintes` -------------------------------------------

def _config_trois_boitiers(config):
    config.pointeuse["mode"] = "zk"
    config.pointeuses = [{"ip": "10.0.0.1", "nom": "Entrée principale"},
                         {"ip": "10.0.0.2", "nom": "Entrée secondaire"},
                         {"ip": "10.0.0.3", "nom": "Portail sud"}]
    return config


def test_commande_copie_vers_tous_les_autres_et_verifie(config, monkeypatch, capsys):
    import argparse

    import main

    source, b2, b3 = _source(), BoitierFactice(taille_fiche=72), BoitierFactice(taille_fiche=72)
    _patcher_zk(monkeypatch, {"10.0.0.1": source, "10.0.0.2": b2, "10.0.0.3": b3})
    main.cmd_copier_empreintes(_config_trois_boitiers(config),
                               argparse.Namespace(source=None, oui=True))

    sortie = capsys.readouterr().out
    assert sortie.count("Vérifié") == 2
    assert "Copie terminée" in sortie
    assert b2.doigts_de("1") == b3.doigts_de("1") == source.doigts_de("1")


def test_commande_source_choisie_par_son_nom(config, monkeypatch, capsys):
    """Les doigts ont été enregistrés sur le portail sud : c'est lui qu'il faut lire."""
    import argparse

    import main

    principale, secondaire = BoitierFactice(taille_fiche=72), BoitierFactice(taille_fiche=72)
    _patcher_zk(monkeypatch, {"10.0.0.1": principale, "10.0.0.2": secondaire, "10.0.0.3": _source()})
    main.cmd_copier_empreintes(_config_trois_boitiers(config),
                               argparse.Namespace(source="Portail sud", oui=True))

    assert "« Portail sud » : 3 personne(s), 5 empreinte(s)" in capsys.readouterr().out
    assert len(principale.utilisateurs) == len(secondaire.utilisateurs) == 3


def test_commande_source_inconnue(config, monkeypatch):
    import argparse

    import main

    with pytest.raises(SystemExit) as fin:
        main.cmd_copier_empreintes(_config_trois_boitiers(config),
                                   argparse.Namespace(source="Cantine", oui=True))
    assert "Entrée principale, Entrée secondaire, Portail sud" in str(fin.value)


def test_commande_signale_le_boitier_en_echec_sans_bloquer_les_autres(config, monkeypatch, capsys):
    import argparse

    import main

    b2, b3 = BoitierFactice(taille_fiche=72, echoue_apres=0), BoitierFactice(taille_fiche=72)
    _patcher_zk(monkeypatch, {"10.0.0.1": _source(), "10.0.0.2": b2, "10.0.0.3": b3})
    with pytest.raises(SystemExit) as fin:
        main.cmd_copier_empreintes(_config_trois_boitiers(config),
                                   argparse.Namespace(source=None, oui=True))

    assert "Entrée secondaire" in str(fin.value)
    assert "Portail sud" not in str(fin.value)
    assert len(b3.utilisateurs) == 3  # le 3e boîtier a quand même été rempli
