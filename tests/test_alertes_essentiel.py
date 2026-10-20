"""Formule Essentiel : alertes seulement en cas de retard ou d'absence.

Le plus important ici, ce sont les cas où il ne faut RIEN envoyer : annoncer à tort
l'absence de leur enfant à des centaines de parents serait la pire panne possible.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

from pointage import db, eleves
from pointage.collector import synchroniser
from pointage.device import MultiPointeuse, PointeuseSimulee
from pointage.notifier import creer_alertes_absence, envoyer_en_attente

LUNDI = datetime(2026, 10, 5)
DIMANCHE = datetime(2026, 10, 11)


def a(heure, minute=0, jour=LUNDI):
    return jour.replace(hour=heure, minute=minute)


def essentiel(config):
    config.notifications["types_notifies"] = ["retard", "absence"]
    config.notifications["absence_apres"] = "09:00"
    config.notifications["absence_min_presents_pourcent"] = 50
    return config


def lire(conn, config, *pointages, maintenant):
    pointeuse = PointeuseSimulee()
    for zk_id, moment in pointages:
        pointeuse.pointer(zk_id, moment)
    synchroniser(conn, pointeuse, config, maintenant=maintenant)


def alertes(conn):
    return [(r["zk_user_id"], r["type"], r["statut"])
            for r in conn.execute("SELECT * FROM notifications ORDER BY id")]


class EnvoyeurMemoire:
    def __init__(self):
        self.envoyes = []

    def envoyer(self, tel, message, variables):
        self.envoyes.append((tel, message))


# --- Retards -----------------------------------------------------------------------

def test_seule_l_arrivee_en_retard_est_annoncee(conn, config, trois_eleves):
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(8, 15)), maintenant=a(8, 16))
    lire(conn, config, ("1", a(17, 5)), maintenant=a(17, 6))  # sortie : rien non plus

    assert alertes(conn) == [("2", "arrivee", "en_attente")]


# --- Absences : le cas normal ------------------------------------------------------

def test_absence_annoncee_a_9h(conn, config, trois_eleves):
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))

    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 1
    assert alertes(conn) == [("3", "absence", "en_attente")]

    envoyeur = EnvoyeurMemoire()
    envoyer_en_attente(conn, envoyeur, config, maintenant=a(9, 0))
    assert envoyeur.envoyes == [("+221781234567", "Groupe Scolaire Exemple : Fatou Test (6e B) "
                                                  "n'avait pas pointé à l'école à 09h00.")]


def test_une_seule_alerte_par_jour_meme_rappelee_toutes_les_30_s(conn, config, trois_eleves):
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))
    creer_alertes_absence(conn, config, maintenant=a(9, 0))
    lire(conn, config, maintenant=a(9, 1))

    assert creer_alertes_absence(conn, config, maintenant=a(9, 1)) == 0
    assert len(alertes(conn)) == 1


def test_eleve_arrive_apres_l_alerte_d_absence(conn, config, trois_eleves):
    """Fatou arrive à 9h20 : le parent reçoit l'absence de 9h, puis le retard."""
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))
    creer_alertes_absence(conn, config, maintenant=a(9, 0))
    lire(conn, config, ("3", a(9, 20)), maintenant=a(9, 21))

    assert alertes(conn) == [("3", "absence", "en_attente"), ("3", "arrivee", "en_attente")]


def test_absence_d_un_eleve_sans_abonnement_tracee_mais_pas_envoyee(conn, config, trois_eleves):
    essentiel(config)
    # Inscrit le 1er août : essai terminé le 31 août, rien payé depuis.
    eleves.inscrire(conn, "7", "Sow", "Mariama", "5e A", "771112233", date(2026, 8, 1), 30)
    conn.commit()
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), ("3", a(7, 50)), maintenant=a(9, 0))

    creer_alertes_absence(conn, config, maintenant=a(9, 0))
    assert alertes(conn) == [("7", "absence", "ignore_abonnement")]


# --- Absences : les cas où il ne faut RIEN envoyer ---------------------------------

def test_rien_avant_9h_ni_apres_la_fenetre(conn, config, trois_eleves):
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(8, 59))
    assert creer_alertes_absence(conn, config, maintenant=a(8, 59)) == 0

    # PC allumé à 14 h : annoncer une absence de 9 h aussi tard n'aide plus personne.
    lire(conn, config, maintenant=a(14, 0))
    assert creer_alertes_absence(conn, config, maintenant=a(14, 0)) == 0
    assert alertes(conn) == []


def test_rien_le_dimanche(conn, config, trois_eleves):
    essentiel(config)
    lire(conn, config, ("1", a(8, 0, DIMANCHE)), ("2", a(8, 5, DIMANCHE)),
         maintenant=a(9, 0, DIMANCHE))
    assert creer_alertes_absence(conn, config, maintenant=a(9, 0, DIMANCHE)) == 0


def test_rien_un_jour_ferie_ou_presque_personne_n_a_pointe(conn, config, trois_eleves, caplog):
    """1 élève sur 3 (33 %) : jour férié, grève ou panne. Surtout ne rien dire aux parents."""
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), maintenant=a(9, 0))

    with caplog.at_level(logging.WARNING):
        assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0
        assert creer_alertes_absence(conn, config, maintenant=a(9, 1)) == 0
    assert alertes(conn) == []
    assert sum("NON envoyées" in m for m in caplog.messages) == 1  # un seul avertissement par jour


def test_rien_si_la_pointeuse_n_a_pas_ete_lue_recemment(conn, config, trois_eleves):
    """Lecture à 8h50, puis plus rien (câble débranché) : les pointages de 8h50 à 9h
    sont peut-être dans le boîtier sans être arrivés dans la base."""
    essentiel(config)
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(8, 50))
    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0


def test_rien_si_une_des_pointeuses_est_en_panne(conn, config, trois_eleves, monkeypatch):
    """Avec 3 entrées, un boîtier en panne ferait passer pour absents tous les élèves
    entrés par cette porte. La lecture est alors incomplète : pas d'alerte d'absence."""
    from pointage.device import ZKPointeuse

    essentiel(config)
    ok = PointeuseSimulee()
    ok.pointer("1", a(7, 40))
    ok.pointer("2", a(7, 45))
    en_panne = ZKPointeuse("10.0.0.99")
    monkeypatch.setattr(en_panne, "lire_pointages", lambda: (_ for _ in ()).throw(OSError("injoignable")))
    multi = MultiPointeuse([ok, en_panne])

    synchroniser(conn, multi, config, maintenant=a(9, 0))
    assert multi.lecture_complete is False
    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0


def test_rien_si_l_absence_n_est_pas_dans_la_formule(conn, config, trois_eleves):
    config.notifications["types_notifies"] = ["arrivee", "sortie"]
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), maintenant=a(9, 0))
    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0


def test_eleve_inscrit_apres_ce_jour_pas_compte_absent(conn, config, trois_eleves):
    essentiel(config)
    eleves.inscrire(conn, "8", "Fall", "Ibou", "6e A", "770000000", date(2026, 10, 6), 30)
    conn.commit()
    lire(conn, config, ("1", a(7, 40)), ("2", a(7, 45)), ("3", a(7, 50)), maintenant=a(9, 0))
    assert creer_alertes_absence(conn, config, maintenant=a(9, 0)) == 0


# --- Mise à jour d'une base de la version précédente --------------------------------

ANCIEN_SCHEMA = """
CREATE TABLE pointages (id INTEGER PRIMARY KEY AUTOINCREMENT, zk_user_id TEXT NOT NULL,
    horodatage TEXT NOT NULL, type TEXT NOT NULL, en_retard INTEGER NOT NULL DEFAULT 0,
    recu_le TEXT NOT NULL, UNIQUE (zk_user_id, horodatage));
CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT,
    pointage_id INTEGER NOT NULL REFERENCES pointages(id), tel TEXT NOT NULL,
    message TEXT NOT NULL, variables TEXT NOT NULL, statut TEXT NOT NULL,
    tentatives INTEGER NOT NULL DEFAULT 0, erreur TEXT, cree_le TEXT NOT NULL, envoye_le TEXT);
CREATE INDEX idx_notifications_statut ON notifications(statut);
INSERT INTO pointages VALUES (1, '12', '2026-10-05 07:42:00', 'arrivee', 0, '2026-10-05 07:42:30');
INSERT INTO notifications VALUES (1, 1, '+221771234567', 'msg', '{}', 'envoye', 1, NULL,
    '2026-10-05 07:42:30', '2026-10-05 07:42:31');
"""


def test_une_base_existante_est_mise_a_jour_sans_rien_perdre(tmp_path):
    chemin = tmp_path / "ancienne.db"
    ancienne = sqlite3.connect(chemin)
    ancienne.executescript(ANCIEN_SCHEMA)
    ancienne.close()

    conn = db.connecter(chemin)
    db.initialiser(conn)
    db.initialiser(conn)  # deuxième démarrage : rien à refaire

    ligne = conn.execute("SELECT * FROM notifications").fetchone()
    assert (ligne["id"], ligne["pointage_id"], ligne["zk_user_id"], ligne["moment"], ligne["type"],
            ligne["statut"], ligne["envoye_le"]) == (1, 1, "12", "2026-10-05 07:42:00", "arrivee",
                                                    "envoye", "2026-10-05 07:42:31")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "notifications_v1" not in tables
    # Une alerte d'absence (sans pointage) peut maintenant être rangée.
    conn.execute("INSERT INTO notifications (pointage_id, zk_user_id, moment, type, tel, message, "
                 "variables, statut, cree_le) VALUES (NULL, '12', '2026-10-06 09:00:00', 'absence', "
                 "'+221771234567', 'msg', '{}', 'en_attente', '2026-10-06 09:00:00')")
    conn.close()
