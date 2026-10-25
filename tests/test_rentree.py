"""Rentrée, élèves qui partent, et nettoyage des pointeuses.

Le risque à éviter avant tout : un élève parti qui reste « inscrit » et dont le
parent reçoit chaque matin une alerte d'absence. Puis l'inverse : un fichier
incomplet qui ferait passer pour partis des élèves bien présents.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date

import pytest
from zk.finger import Finger
from zk.user import User

import main
from pointage import db, eleves, personnel, reports
from pointage.device import ZKPointeuse
from pointage.notifier import creer_alertes_absence
from test_alertes_essentiel import LUNDI, a, alertes, essentiel, lire
from test_copie_empreintes import BoitierFactice, _patcher_zk

RENTREE = date(2027, 10, 4)
EN_TETE = "zk_user_id;nom;prenom;classe;tel_parent\n"


def liste(tmp_path, *lignes, nom="liste.csv"):
    chemin = tmp_path / nom
    chemin.write_text(EN_TETE + "".join(l + "\n" for l in lignes), encoding="utf-8")
    return chemin


# Rentrée 2027 : Awa passe en 5e A, Moussa reste en 6e A (redouble), Fatou est partie,
# Ibou arrive.
LISTE_2027 = ("1;Test;Awa;5e A;771234567", "2;Test;Moussa;6e A;762345678",
              "4;Fall;Ibou;6e A;770000004")


def eleve(conn, zk_id):
    return conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_id,)).fetchone()


# --- Rentrée ----------------------------------------------------------------------

def test_l_apercu_ne_modifie_rien(conn, config, trois_eleves, tmp_path):
    apercu = eleves.rentree(conn, liste(tmp_path, *LISTE_2027), RENTREE, 30, appliquer=False)

    assert (apercu.maintenus, apercu.changent_de_classe) == (2, 1)
    assert [n["prenom"] for n in apercu.nouveaux] == ["Ibou"]
    assert [p["prenom"] for p in apercu.partants] == ["Fatou"]
    assert eleve(conn, "1")["classe"] == "6e A"
    assert eleve(conn, "3")["actif_scolarite"] == 1
    assert eleve(conn, "4") is None


def test_rentree_appliquee(conn, config, trois_eleves, tmp_path):
    eleves.rentree(conn, liste(tmp_path, *LISTE_2027), RENTREE, 30, appliquer=True)

    assert eleve(conn, "1")["classe"] == "5e A"
    assert eleve(conn, "1")["date_inscription"] == "2026-10-01"  # l'historique reste celui de l'élève
    assert eleve(conn, "4")["classe"] == "6e A"
    fatou = eleve(conn, "3")
    assert (fatou["actif_scolarite"], fatou["date_depart"]) == (0, "2027-10-04")


def test_une_ligne_en_erreur_et_rien_n_est_applique(conn, config, trois_eleves, tmp_path):
    """Sinon Moussa, dont le téléphone est mal écrit, serait marqué parti."""
    fichier = liste(tmp_path, "1;Test;Awa;5e A;771234567", "2;Test;Moussa;6e A;12",
                    "3;Test;Fatou;5e B;781234567")
    apercu = eleves.rentree(conn, fichier, RENTREE, 30, appliquer=True)

    assert len(apercu.erreurs) == 1 and "ligne 3" in apercu.erreurs[0]
    assert eleve(conn, "1")["classe"] == "6e A"
    assert eleve(conn, "2")["actif_scolarite"] == 1


def test_deux_eleves_sous_le_meme_numero(conn, config, trois_eleves, tmp_path):
    fichier = liste(tmp_path, *LISTE_2027, "4;Sarr;Khady;6e B;770000005")
    apercu = eleves.rentree(conn, fichier, RENTREE, 30, appliquer=True)
    assert any("numéro 4 déjà utilisé ligne 4" in e for e in apercu.erreurs)
    assert eleve(conn, "4") is None


def test_fichier_d_une_seule_classe_signale(conn, config, trois_eleves, tmp_path):
    apercu = eleves.rentree(conn, liste(tmp_path, "3;Test;Fatou;5e B;781234567"),
                            RENTREE, 30, appliquer=False)
    assert apercu.trop_de_partants  # 2 sur 3 : plus de la moitié


def test_un_ancien_eleve_qui_revient_est_reactive(conn, config, trois_eleves, tmp_path):
    eleves.rentree(conn, liste(tmp_path, *LISTE_2027), RENTREE, 30, appliquer=True)
    retour = liste(tmp_path, *LISTE_2027, "3;TEST;Fatou;4e B;781234567", nom="2028.csv")
    apercu = eleves.rentree(conn, retour, date(2028, 10, 2), 30, appliquer=True)

    assert [r["prenom"] for r in apercu.revenants] == ["Fatou"]
    fatou = eleve(conn, "3")
    assert (fatou["actif_scolarite"], fatou["date_depart"], fatou["classe"]) == (1, None, "4e B")


def test_le_numero_d_un_ancien_eleve_n_est_pas_redonne(conn, config, trois_eleves, tmp_path):
    eleves.rentree(conn, liste(tmp_path, *LISTE_2027), RENTREE, 30, appliquer=True)
    with pytest.raises(ValueError, match="était celui de Fatou Test"):
        eleves.inscrire(conn, "3", "Ba", "Aminata", "6e B", "770000009", RENTREE, 30)


def test_meme_nom_ecrit_autrement():
    assert eleves.cle_nom("DIOP", "Aïda  Ndèye") == eleves.cle_nom("Diop", "aida ndeye")


def test_prochain_numero_libre(conn, config, trois_eleves):
    eleves.marquer_partis(conn, ["3"], RENTREE)
    assert eleves.prochain_numero_libre(conn) == 4  # le 3 d'un élève parti ne se redonne pas
    personnel.inscrire(conn, "4", "Ndiaye", "Mamadou", "Prof", RENTREE)
    assert eleves.prochain_numero_libre(conn) == 5  # le 4 est pris par un prof


# --- Départ en cours d'année --------------------------------------------------------

def test_plus_d_alerte_d_absence_pour_un_eleve_parti(conn, config, trois_eleves):
    essentiel(config)
    eleves.marquer_partis(conn, ["3"], date(2026, 10, 3))
    conn.commit()
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))

    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0
    assert alertes(conn) == []


def test_les_rapports_comptent_l_eleve_jusqu_a_son_depart(conn, config, trois_eleves):
    eleves.marquer_partis(conn, ["3"], date(2026, 10, 20))
    avant = [e["zk_user_id"] for e in reports._eleves_inscrits(conn, date(2026, 10, 19))]
    apres = [e["zk_user_id"] for e in reports._eleves_inscrits(conn, date(2026, 10, 20))]
    assert "3" in avant and "3" not in apres


def test_numero_inconnu_refuse_sans_rien_changer(conn, config, trois_eleves):
    with pytest.raises(ValueError, match="99"):
        eleves.marquer_partis(conn, ["3", "99"], RENTREE)
    assert eleve(conn, "3")["actif_scolarite"] == 1


def test_commande_eleve_parti(conn, config, trois_eleves, capsys):
    main.cmd_eleve_parti(config, argparse.Namespace(zk_ids=["3"], date="2026-11-02"))
    main.cmd_eleve_parti(config, argparse.Namespace(zk_ids=["3"], date="2026-11-09"))
    sortie = capsys.readouterr().out
    assert "6e B     Test Fatou (n° 3) : parti(e) à partir du 02/11/2026" in sortie
    assert "déjà parti(e) depuis le 02/11/2026" in sortie
    assert eleve(conn, "3")["date_depart"] == "2026-11-02"  # la 2e fois ne change pas la date


def test_commande_rentree_refuse_un_fichier_avec_erreurs(conn, config, trois_eleves, tmp_path):
    fichier = liste(tmp_path, "1;Test;Awa;5e A;pas-un-numero")
    with pytest.raises(SystemExit, match="Rien n'a été modifié"):
        main.cmd_rentree(config, argparse.Namespace(fichier=fichier, date=None, oui=True, forcer=False))


def test_commande_rentree_refuse_trop_de_partants_sans_forcer(conn, config, trois_eleves, tmp_path):
    fichier = liste(tmp_path, "3;Test;Fatou;5e B;781234567")
    with pytest.raises(SystemExit, match="TOUTES les classes"):
        main.cmd_rentree(config, argparse.Namespace(fichier=fichier, date=None, oui=True, forcer=False))
    assert eleve(conn, "1")["actif_scolarite"] == 1
    main.cmd_rentree(config, argparse.Namespace(fichier=fichier, date="2027-10-04", oui=True, forcer=True))
    assert eleve(conn, "1")["actif_scolarite"] == 0


# --- Base : migration vers la version 2 ---------------------------------------------

def test_une_base_en_version_1_recoit_la_date_de_depart(tmp_path):
    ancienne = sqlite3.connect(tmp_path / "v1.db")
    ancienne.executescript(db.SCHEMA.replace(
        "    actif_scolarite  INTEGER NOT NULL DEFAULT 1, -- 0 = a quitté l'école\n"
        "    date_depart      TEXT                        -- premier jour où il n'est plus élève "
        "(NULL = toujours là)\n",
        "    actif_scolarite  INTEGER NOT NULL DEFAULT 1\n"))
    ancienne.execute("INSERT INTO eleves (zk_user_id, nom, prenom, classe, tel_parent, date_inscription, "
                     "essai_fin) VALUES ('1', 'Test', 'Awa', '6e A', '+221771234567', '2026-10-01', "
                     "'2026-10-31')")
    ancienne.execute("PRAGMA user_version = 1")
    ancienne.commit()
    assert "date_depart" not in {r[1] for r in ancienne.execute("PRAGMA table_info(eleves)")}
    ancienne.close()

    conn = db.connecter(tmp_path / "v1.db")
    db.initialiser(conn)
    assert db.version_base(conn) == db.VERSION_BASE
    assert eleve(conn, "1")["date_depart"] is None and eleve(conn, "1")["prenom"] == "Awa"
    conn.close()


# --- Nettoyage des pointeuses --------------------------------------------------------

class BoitierAvecEffacement(BoitierFactice):
    """Boîtier factice qui sait aussi effacer (delete_user, delete_user_template)."""

    def __init__(self, *args, efface_vraiment=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.efface_vraiment = efface_vraiment

    def delete_user_template(self, uid=0, temp_id=0, user_id=""):
        self.appels.append("delete_user_template")
        if self.efface_vraiment:
            self.empreintes.pop((uid, temp_id), None)
        return True

    def delete_user(self, uid=0, user_id=""):
        self.appels.append("delete_user")
        if self.efface_vraiment:
            self.utilisateurs.pop(uid)

    def numeros(self):
        return sorted(u.user_id for u in self.utilisateurs.values())


def _boitier():
    """Élèves 1, 2, 3, le prof 900 et un administrateur du boîtier (999, inconnu de la base)."""
    return BoitierAvecEffacement(
        utilisateurs=[User(1, "Awa", 0, "", "", "1"), User(2, "Moussa", 0, "", "", "2"),
                      User(3, "Fatou", 0, "", "", "3"), User(4, "Prof", 0, "", "", "900"),
                      User(5, "Admin", 14, "", "", "999")],
        empreintes=[Finger(1, 0, 1, b"a"), Finger(2, 0, 1, b"m"), Finger(3, 0, 1, b"f0"),
                    Finger(3, 5, 1, b"f5"), Finger(4, 0, 1, b"p"), Finger(5, 0, 1, b"x")])


def test_retirer_efface_la_personne_et_ses_doigts(monkeypatch):
    boitier = _boitier()
    _patcher_zk(monkeypatch, {"10.0.0.1": boitier})
    assert ZKPointeuse("10.0.0.1").retirer({"3", "42"}) == ["3"]  # 42 : absent, ignoré

    assert boitier.numeros() == ["1", "2", "900", "999"]
    assert not any(uid == 3 for uid, _ in boitier.empreintes)
    assert boitier.appels[0] == "disable_device" and boitier.appels[-1] == "enable_device"


def test_retirer_signale_un_boitier_qui_n_a_pas_efface(monkeypatch):
    boitier = BoitierAvecEffacement(utilisateurs=[User(3, "Fatou", 0, "", "", "3")],
                                    empreintes=[Finger(3, 0, 1, b"f")], efface_vraiment=False)
    _patcher_zk(monkeypatch, {"10.0.0.1": boitier})
    with pytest.raises(RuntimeError, match="toujours présent"):
        ZKPointeuse("10.0.0.1").retirer({"3"})
    assert boitier.appels[-1] == "enable_device"


def test_commande_nettoyer_ne_touche_qu_aux_eleves_partis(conn, config, trois_eleves, monkeypatch, capsys):
    principal, secondaire = _boitier(), _boitier()
    _patcher_zk(monkeypatch, {"10.0.0.1": principal, "10.0.0.2": secondaire})
    config.pointeuse["mode"] = "zk"
    config.pointeuses = [{"ip": "10.0.0.1", "nom": "Entrée"}, {"ip": "10.0.0.2", "nom": "Portail"}]
    eleves.marquer_partis(conn, ["3"], RENTREE)
    conn.commit()

    main.cmd_nettoyer_boitier(config, argparse.Namespace(oui=True))

    assert principal.numeros() == secondaire.numeros() == ["1", "2", "900", "999"]
    sortie = capsys.readouterr().out
    assert sortie.count("1 personne(s) retirée(s), vérifié") == 2
    assert eleve(conn, "3") is not None  # la fiche reste dans la base


def test_commande_nettoyer_sans_eleve_parti(conn, config, trois_eleves, monkeypatch, capsys):
    boitier = _boitier()
    _patcher_zk(monkeypatch, {"10.0.0.1": boitier})
    config.pointeuse["mode"] = "zk"
    config.pointeuses = [{"ip": "10.0.0.1"}]
    main.cmd_nettoyer_boitier(config, argparse.Namespace(oui=True))
    assert "rien à retirer" in capsys.readouterr().out
    assert len(boitier.utilisateurs) == 5
