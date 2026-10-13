import random
import sqlite3
from datetime import date, datetime

import pytest

from pointage import archive, backup, eleves, reports
from pointage.collector import synchroniser
from pointage.device import PointeuseSimulee, generer_journee


def compter(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_archivage_garde_les_eleves_et_vide_les_pointages(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", datetime(2027, 6, 10, 7, 40))   # année 2026-2027
    pointeuse.pointer("2", datetime(2027, 6, 10, 7, 45))
    pointeuse.pointer("1", datetime(2027, 10, 4, 7, 40))   # rentrée suivante
    synchroniser(conn, pointeuse, config, maintenant=datetime(2027, 10, 4, 7, 41))

    resultat = archive.archiver_annee(conn, config, "2026-2027", date(2027, 7, 31))

    assert compter(conn, "eleves") == 3
    assert compter(conn, "pointages") == 1
    assert resultat["comptes"]["pointages"] == 2
    copie = sqlite3.connect(resultat["archive"])
    assert copie.execute("SELECT COUNT(*) FROM pointages").fetchone()[0] == 2
    copie.close()
    assert all(f.exists() for f in resultat["csv"])
    assert resultat["sauvegarde"].exists()

    # La pointeuse a encore les vieux pointages en mémoire : ils ne reviennent pas.
    relecture = synchroniser(conn, pointeuse, config, maintenant=datetime(2027, 10, 4, 8, 0))
    assert relecture.anterieurs_archive == 2 and relecture.nouveaux == 0


def test_archivage_refuse_d_ecraser_une_archive(conn, config, trois_eleves):
    archive.archiver_annee(conn, config, "2026-2027", date(2027, 7, 31))
    with pytest.raises(FileExistsError):
        archive.archiver_annee(conn, config, "2026-2027", date(2027, 7, 31))


def test_rapport_mensuel_pdf(conn, config, trois_eleves):
    rng = random.Random(42)
    pointeuse = PointeuseSimulee()
    for jour in range(5, 10):  # du lundi 5 au vendredi 9 octobre
        pointeuse.ajouter(generer_journee(["1", "2", "3"], date(2026, 10, jour), rng))
    synchroniser(conn, pointeuse, config, maintenant=datetime(2026, 10, 9, 18, 0))

    stats = reports.calculer_stats(conn, 2026, 10, aujourd_hui=date(2026, 10, 12))
    assert stats.nb_eleves == 3
    assert stats.jours_ecole == 5
    assert 0 < stats.taux_assiduite <= 1
    assert stats.abonnements["essai"] == 3

    chemin = reports.generer_pdf(stats, config.nom_ecole, config.chemin("rapports") / "test.pdf")
    assert chemin.read_bytes().startswith(b"%PDF")


def test_rapport_du_jour_signale_qui_n_a_pas_pointe(conn, config, trois_eleves):
    lundi = date(2026, 10, 5)
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", datetime(2026, 10, 5, 7, 40))    # Awa : arrivée + sortie, à l'heure
    pointeuse.pointer("1", datetime(2026, 10, 5, 17, 5))
    pointeuse.pointer("2", datetime(2026, 10, 5, 8, 20))    # Moussa : en retard, sortie oubliée
    # Fatou (3) : aucun pointage -> absente
    synchroniser(conn, pointeuse, config, maintenant=datetime(2026, 10, 5, 18, 0))

    stats = reports.calculer_jour(conn, lundi)
    assert stats.nb_eleves == 3 and stats.presents == 2
    assert [a["nom"] for a in stats.absents] == ["Fatou Test"]
    assert stats.absents[0]["tel"] == "+221781234567"
    assert [(r["nom"], r["heure"]) for r in stats.retards] == [("Moussa Test", "08:20")]
    assert [s["nom"] for s in stats.sans_sortie] == ["Moussa Test"]
    assert stats.sans_arrivee == []

    chemin = reports.generer_rapport_jour(conn, config, lundi)
    assert chemin.name == "rapport_2026-10-05.pdf"
    assert chemin.read_bytes().startswith(b"%PDF")


def test_releve_du_prestataire(conn, config, trois_eleves):
    from pointage.subscriptions import enregistrer_paiement

    enregistrer_paiement(conn, "1", 2, 1000, aujourd_hui=date(2026, 10, 15))
    enregistrer_paiement(conn, "2", 1, 1000, aujourd_hui=date(2026, 10, 16))
    # Au 15 novembre : l'essai de Fatou (3) a pris fin le 31/10 et elle n'a rien payé.
    releve = reports.calculer_releve(conn, 2026, 11, part_pourcent=100, aujourd_hui=date(2026, 11, 15))
    assert releve.paiements == [] and releve.montant_du == 0
    assert [r["nom"] for r in releve.a_relancer] == ["Fatou Test"]

    releve = reports.calculer_releve(conn, 2026, 10, part_pourcent=100, aujourd_hui=date(2026, 10, 31))
    assert releve.revenus == 3000 and releve.montant_du == 3000
    assert [p["montant"] for p in releve.paiements] == [2000, 1000]
    chemin = reports.generer_pdf_releve(releve, config.nom_ecole, config.chemin("rapports") / "r.pdf")
    assert chemin.read_bytes().startswith(b"%PDF")


def test_rapports_automatiques(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", datetime(2026, 10, 5, 7, 40))
    synchroniser(conn, pointeuse, config, maintenant=datetime(2026, 10, 5, 7, 41))

    # 17h : trop tôt pour le rapport du jour.
    assert reports.rapports_automatiques(conn, config, maintenant=datetime(2026, 10, 5, 17, 0)) == []
    # 18h30 : rapport du jour créé, une seule fois.
    crees = reports.rapports_automatiques(conn, config, maintenant=datetime(2026, 10, 5, 18, 30))
    assert [c.name for c in crees] == ["rapport_2026-10-05.pdf"]
    assert reports.rapports_automatiques(conn, config, maintenant=datetime(2026, 10, 5, 19, 30)) == []
    # Le 2 novembre : rapport mensuel d'octobre.
    crees = reports.rapports_automatiques(conn, config, maintenant=datetime(2026, 11, 2, 9, 0))
    assert [c.name for c in crees] == ["rapport_2026-10.pdf", "releve_2026-10.pdf"]


def test_sauvegarde_et_rotation(conn, config, trois_eleves):
    ancienne = backup.sauvegarder(conn, config, maintenant=datetime(2026, 8, 1, 10, 0))
    recente = backup.sauvegarder(conn, config, maintenant=datetime(2026, 10, 12, 10, 0))
    assert not ancienne.exists()  # plus de 30 jours : supprimée
    copie = sqlite3.connect(recente)
    assert copie.execute("SELECT COUNT(*) FROM eleves").fetchone()[0] == 3
    copie.close()
    # Moins de 24 h depuis la dernière : pas de nouvelle sauvegarde.
    assert backup.sauvegarder_si_besoin(conn, config, maintenant=datetime(2026, 10, 12, 18, 0)) is None


def test_import_csv_exemple(conn, config):
    from pointage.config import RACINE_PROJET

    crees, maj, erreurs = eleves.importer_csv(conn, RACINE_PROJET / "exemples" / "eleves_exemple.csv",
                                              date(2026, 10, 1), 30)
    assert crees == 9 and maj == 0
    assert len(erreurs) == 1 and "ligne 11" in erreurs[0]  # le numéro "123" est refusé
