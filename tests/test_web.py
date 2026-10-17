from datetime import date, datetime

import pytest

from pointage import reports
from pointage.collector import synchroniser
from pointage.device import PointeuseSimulee
from pointage.web import create_app

MOT_DE_PASSE = "mot-de-passe-test"


@pytest.fixture
def client(config, conn, trois_eleves):
    aujourd_hui = datetime.now().replace(hour=7, minute=40, second=0, microsecond=0)
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", aujourd_hui)
    synchroniser(conn, pointeuse, config, maintenant=aujourd_hui)
    return create_app(config, MOT_DE_PASSE).test_client()


def connecter(client):
    return client.post("/connexion", data={"mot_de_passe": MOT_DE_PASSE}, follow_redirects=True)


def test_acces_refuse_sans_connexion(client):
    for chemin in ["/", "/rapports", "/rapports/mensuels/rapport_2026-10.pdf"]:
        reponse = client.get(chemin)
        assert reponse.status_code == 302 and "/connexion" in reponse.headers["Location"]


def test_mauvais_mot_de_passe(client, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # on n'attend pas la seconde de pénalité
    reponse = client.post("/connexion", data={"mot_de_passe": "faux"}, follow_redirects=True)
    assert "Mot de passe incorrect" in reponse.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_page_aujourdhui(client):
    page = connecter(client).get_data(as_text=True)
    assert "Awa Test" in page                 # a pointé
    assert "Absents" in page and "Moussa Test" in page and "+221762345678" in page


def test_jour_invalide_retombe_sur_aujourdhui(client):
    connecter(client)
    assert client.get("/?jour=n-importe-quoi").status_code == 200


def test_generer_et_telecharger_un_rapport(client, config):
    connecter(client)
    jour = date.today().isoformat()
    client.post("/rapports/generer", data={"type": "jour", "jour": jour})
    nom = f"rapport_{jour}.pdf"
    assert nom in client.get("/rapports").get_data(as_text=True)
    pdf = client.get(f"/rapports/journaliers/{nom}")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_releve_du_prestataire_inaccessible(client, conn, config):
    connecter(client)
    reports.generer_releve(conn, config, 2026, 10)
    assert client.get("/rapports/prestataire/releve_2026-10.pdf").status_code == 404
    assert "releve_2026-10" not in client.get("/rapports").get_data(as_text=True)


def test_pas_de_sortie_du_dossier_des_rapports(client):
    connecter(client)
    assert client.get("/rapports/journaliers/..%2F..%2Fconfig.toml").status_code == 404
    assert client.get("/rapports/journaliers/config.toml").status_code == 404


def test_entetes_de_securite(client):
    reponse = client.get("/connexion")
    assert reponse.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in reponse.headers["Content-Security-Policy"]
    assert reponse.headers["Cache-Control"] == "no-store"


def test_blocage_apres_trop_d_essais(client):
    for _ in range(5):
        client.post("/connexion", data={"mot_de_passe": "faux"})
    # 6e essai, même avec le BON mot de passe : refusé pendant le blocage
    reponse = client.post("/connexion", data={"mot_de_passe": MOT_DE_PASSE})
    assert reponse.status_code == 429
    assert client.get("/").status_code == 302


def test_limiteur_se_libere_apres_15_minutes():
    from pointage.web import LimiteurConnexion

    maintenant = [1000.0]
    limiteur = LimiteurConnexion(horloge=lambda: maintenant[0])
    for _ in range(5):
        limiteur.echec("10.0.0.5")
    assert limiteur.bloque("10.0.0.5") and not limiteur.bloque("10.0.0.6")
    maintenant[0] += 15 * 60 + 1
    assert not limiteur.bloque("10.0.0.5")


def test_deconnexion_apres_30_minutes_sans_activite(config, conn, trois_eleves):
    from pointage import web

    maintenant = [1_000_000.0]
    client = create_app(config, MOT_DE_PASSE, horloge=lambda: maintenant[0]).test_client()
    connecter(client)
    maintenant[0] += 29 * 60
    assert client.get("/").status_code == 200          # activité : le compteur repart
    maintenant[0] += 29 * 60
    assert client.get("/").status_code == 200
    maintenant[0] += (web.INACTIVITE_MINUTES + 1) * 60
    reponse = client.get("/")
    assert reponse.status_code == 302 and "/connexion" in reponse.headers["Location"]
    assert client.get("/rapports").status_code == 302  # la session a bien été effacée
