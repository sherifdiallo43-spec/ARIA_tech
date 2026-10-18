from datetime import date, datetime

import pytest

from pointage import personnel, reports
from pointage.collector import synchroniser
from pointage.config import RACINE_PROJET
from pointage.device import PointeuseSimulee
from pointage.eleves import inscrire as inscrire_eleve
from pointage.web import create_app

INSCRIPTION = date(2026, 10, 1)
LUNDI = date(2026, 10, 5)
MARDI = date(2026, 10, 6)
SAMEDI = date(2026, 10, 10)
EXEMPLES = RACINE_PROJET / "exemples"


def a(jour, heure, minute=0):
    return datetime(jour.year, jour.month, jour.day, heure, minute)


def ecrire_csv(tmp_path, nom, contenu):
    chemin = tmp_path / nom
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


@pytest.fixture
def profs(conn, trois_eleves, tmp_path):
    """900 : lundi 08-10 et 15-17, samedi 08-10. 901 : lundi 10-12 seulement."""
    personnel.inscrire(conn, "900", "Ndiaye", "Mamadou", "Prof de maths", INSCRIPTION)
    personnel.inscrire(conn, "901", "Sy", "Aïssatou", "Prof de français", INSCRIPTION)
    conn.commit()
    edt = ecrire_csv(tmp_path, "edt.csv", "zk_user_id;jour;debut;fin;classe;matiere\n"
                     "900;lundi;8h;10h;3e A;Maths\n900;Lundi;15:00;17:00;4e A;Maths\n"
                     "900;samedi;08:00;10:00;3e A;Maths\n901;lundi;10:00;12:00;3e A;Français\n")
    assert personnel.importer_emploi_du_temps(conn, edt) == (4, 2, [])


def synchro(conn, config, *pointages):
    pointeuse = PointeuseSimulee()
    for zk_id, moment in pointages:
        pointeuse.pointer(zk_id, moment)
    return synchroniser(conn, pointeuse, config, maintenant=max(m for _, m in pointages))


def ligne(conn, jour, nom):
    return next(m for m in personnel.calculer_jour(conn, jour, 5) if m["nom"] == nom)


# --- Inscription et emploi du temps ----------------------------------------------

def test_import_des_exemples(conn):
    assert personnel.importer_csv(conn, EXEMPLES / "personnel_exemple.csv", INSCRIPTION) == (4, 0, [])
    cours, membres, erreurs = personnel.importer_emploi_du_temps(conn, EXEMPLES / "emploi_du_temps_exemple.csv")
    assert (membres, erreurs) == (4, []) and cours == 18
    assert [c["debut"] for c in personnel.creneaux_du_jour(conn, "900", LUNDI)] == ["08:00", "10:00"]


def test_un_numero_ne_peut_pas_etre_eleve_et_prof(conn, config, profs):
    with pytest.raises(ValueError, match="déjà celui d'un élève"):
        personnel.inscrire(conn, "1", "X", "Y", "Prof", INSCRIPTION)
    with pytest.raises(ValueError, match="déjà celui d'un membre du personnel"):
        inscrire_eleve(conn, "900", "X", "Y", "6e A", "771234567", INSCRIPTION, 30)


def test_emploi_du_temps_en_erreur_ne_touche_pas_l_ancien(conn, profs, tmp_path):
    edt = ecrire_csv(tmp_path, "faux.csv", "zk_user_id;jour;debut;fin\n"
                     "900;mardi;08:00;10:00\n900;mardi;12:00;11:00\n"   # fin avant début
                     "999;lundi;08:00;10:00\n901;dimenche;08:00;10:00\n901;mardi;14:00;16:00\n")
    cours, membres, erreurs = personnel.importer_emploi_du_temps(conn, edt)
    assert (cours, membres) == (0, 0)
    texte = " ".join(erreurs)
    assert "finit" in texte and "inconnu" in texte and "jour invalide" in texte
    assert len(personnel.creneaux_du_jour(conn, "900", LUNDI)) == 2   # ancien emploi du temps gardé


def test_reimport_remplace_l_emploi_du_temps(conn, profs, tmp_path):
    edt = ecrire_csv(tmp_path, "nouveau.csv", "zk_user_id;jour;debut;fin\n900;mardi;08:00;09:00\n")
    assert personnel.importer_emploi_du_temps(conn, edt) == (1, 1, [])
    assert personnel.creneaux_du_jour(conn, "900", LUNDI) == []
    assert len(personnel.creneaux_du_jour(conn, "901", LUNDI)) == 1   # 901 absent du fichier : inchangé


# --- Pointages -------------------------------------------------------------------

def test_aucun_message_pour_le_personnel(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 55)), ("1", a(LUNDI, 7, 56)))
    assert [m["tel"] for m in conn.execute("SELECT tel FROM notifications")] == ["+221771234567"]


def test_alternance_arrivee_sortie(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 55)), ("900", a(LUNDI, 10, 5)),
            ("900", a(LUNDI, 14, 50)), ("900", a(LUNDI, 17, 5)))
    types = [r["type"] for r in conn.execute(
        "SELECT type FROM pointages WHERE zk_user_id = '900' ORDER BY horodatage")]
    assert types == ["arrivee", "sortie", "arrivee", "sortie"]


# --- Analyse des cours -----------------------------------------------------------

def test_journee_complete_a_l_heure(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 55)), ("900", a(LUNDI, 10, 5)),
            ("900", a(LUNDI, 14, 50)), ("900", a(LUNDI, 17, 5)))
    maths = ligne(conn, LUNDI, "Mamadou Ndiaye")
    assert maths["statut"] == "present" and maths["retards"] == maths["manques"] == 0
    assert maths["minutes"] == 130 + 135 and maths["prevu"] == "08h-10h, 15h-17h"


def test_retard_sur_le_cours_de_l_apres_midi(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 58)), ("900", a(LUNDI, 10, 0)),
            ("900", a(LUNDI, 15, 20)), ("900", a(LUNDI, 17, 0)))
    maths = ligne(conn, LUNDI, "Mamadou Ndiaye")
    assert [(c["etat"], c["retard"]) for c in maths["cours"]] == [("ok", 0), ("retard", 20)]
    assert maths["statut"] == "retard"


def test_cours_manque_mais_present_le_matin(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 58)), ("900", a(LUNDI, 10, 0)))
    maths = ligne(conn, LUNDI, "Mamadou Ndiaye")
    assert [c["etat"] for c in maths["cours"]] == ["ok", "manque"]


def test_sortie_oubliee_n_invente_pas_de_cours_manque(conn, config, profs):
    synchro(conn, config, ("900", a(LUNDI, 7, 58)))
    maths = ligne(conn, LUNDI, "Mamadou Ndiaye")
    assert [c["etat"] for c in maths["cours"]] == ["ok", "ok"]
    assert maths["incomplet"] and maths["minutes"] == 0


def test_absent_et_pas_de_cours(conn, config, profs):
    synchro(conn, config, ("1", a(MARDI, 7, 40)))
    assert ligne(conn, LUNDI, "Aïssatou Sy")["statut"] == "absent"
    assert ligne(conn, MARDI, "Aïssatou Sy")["statut"] == "repos"


def test_samedi_est_un_jour_de_classe(conn, config, profs):
    synchro(conn, config, ("1", a(SAMEDI, 7, 40)), ("900", a(SAMEDI, 8, 15)), ("900", a(SAMEDI, 10, 0)))
    maths = ligne(conn, SAMEDI, "Mamadou Ndiaye")
    assert maths["statut"] == "retard" and maths["cours"][0]["retard"] == 15
    stats = reports.calculer_stats(conn, 2026, 10, aujourd_hui=date(2026, 10, 31))
    assert stats.jours_ecole == 1 and stats.total_absences == 2   # élèves 2 et 3 absents samedi


def test_bilan_du_mois(conn, config, profs):
    synchro(conn, config,
            ("1", a(LUNDI, 7, 40)), ("1", a(SAMEDI, 7, 40)),             # école ouverte lundi et samedi
            ("900", a(LUNDI, 8, 30)), ("900", a(LUNDI, 12, 30)))        # lundi : retard + cours de 15h manqué
    mois = {m["nom"]: m for m in personnel.calculer_mois(conn, 2026, 10, 5, aujourd_hui=date(2026, 10, 31))}
    maths = mois["Mamadou Ndiaye"]
    assert (maths["prevus"], maths["absences"], maths["retards"], maths["manques"], maths["minutes"]) \
        == (2, 1, 1, 2, 240)   # samedi absent : son cours compte comme manqué
    assert (mois["Aïssatou Sy"]["prevus"], mois["Aïssatou Sy"]["absences"]) == (1, 1)


def test_un_prof_seul_le_dimanche_ne_cree_pas_de_jour_d_ecole(conn, config, profs):
    synchro(conn, config, ("1", a(LUNDI, 7, 40)), ("900", a(date(2026, 10, 11), 9, 0)))
    stats = reports.calculer_stats(conn, 2026, 10, aujourd_hui=date(2026, 10, 31))
    assert stats.jours_ecole == 1 and stats.total_pointages == 1


# --- Rapports et confidentialité -------------------------------------------------

def test_rapports_personnel_separes(conn, config, profs):
    synchro(conn, config, ("1", a(LUNDI, 7, 40)), ("900", a(LUNDI, 8, 20)), ("900", a(LUNDI, 10, 0)))
    jour = reports.generer_rapport_personnel_jour(conn, config, LUNDI)
    mois = reports.generer_rapport_personnel_mois(conn, config, 2026, 10)
    assert jour.parent.parent.name == "personnel" and jour.stat().st_size > 0
    assert mois.name == "personnel_2026-10.pdf" and mois.stat().st_size > 0
    assert not reports.chemin_personnel_jour(config, MARDI).exists()


def test_rapports_automatiques_avec_et_sans_personnel(conn, config, trois_eleves):
    synchro(conn, config, ("1", a(LUNDI, 7, 40)))
    crees = reports.rapports_automatiques(conn, config, maintenant=a(LUNDI, 19, 0))
    assert not any("personnel" in c.parts for c in crees)
    personnel.inscrire(conn, "900", "Ndiaye", "Mamadou", "Prof", INSCRIPTION)
    conn.commit()
    crees = reports.rapports_automatiques(conn, config, maintenant=a(LUNDI, 19, 0))
    assert [c.name for c in crees] == ["personnel_2026-10-05.pdf"]


def test_tableau_de_bord_ne_montre_pas_le_rapport_du_personnel(conn, config, profs):
    maintenant = datetime.now().replace(hour=7, minute=55, second=0, microsecond=0)
    synchro(conn, config, ("900", maintenant))
    reports.generer_rapport_personnel_jour(conn, config, maintenant.date())
    client = create_app(config, "mot-de-passe-test").test_client()
    import re
    html_connexion = client.get("/connexion").get_data(as_text=True)
    jeton = re.search(r'name="csrf" value="([^"]+)"', html_connexion).group(1)
    page = client.post("/connexion", data={"mot_de_passe": "mot-de-passe-test", "csrf": jeton},
                       follow_redirects=True).get_data(as_text=True)
    assert "Mamadou Ndiaye" in page                 # nommé dans les derniers pointages, pas « Inconnu »
    assert "<h2>Personnel</h2>" not in page
    fichier = reports.chemin_personnel_jour(config, maintenant.date()).name
    assert client.get(f"/rapports/personnel/{fichier}").status_code == 404
    assert client.get(f"/rapports/personnel/journaliers/{fichier}").status_code == 404
    assert "personnel_" not in client.get("/rapports").get_data(as_text=True)
