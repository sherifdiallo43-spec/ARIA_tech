"""Préparation commune aux tests : une config et une base neuves pour chaque test.

On part du vrai config.toml (ce qui vérifie au passage qu'il est valide),
mais tous les fichiers sont créés dans un dossier temporaire.
"""
from datetime import date

import pytest

from pointage import db, eleves
from pointage.config import RACINE_PROJET, Config, lire_toml

INSCRIPTION = date(2026, 10, 1)  # essai gratuit jusqu'au 31/10/2026


@pytest.fixture
def config(tmp_path):
    donnees = lire_toml(RACINE_PROJET / "config.toml")
    donnees["ecole"]["nom"] = "Groupe Scolaire Exemple"
    donnees["pointeuse"]["mode"] = "simulee"
    donnees["notifications"]["mode"] = "console"
    donnees["directeur"] = donnees.get("directeur", {"email": "", "mode": "console"})
    donnees["directeur"]["mode"] = "console"
    return Config.depuis_dict(tmp_path, donnees)


@pytest.fixture
def conn(config):
    connexion = db.connecter(config.chemin("base"))
    db.initialiser(connexion)
    yield connexion
    connexion.close()


@pytest.fixture
def trois_eleves(conn, config):
    """Élèves 1 à 3 inscrits le 01/10/2026, en essai gratuit tout le mois d'octobre."""
    for zk_id, prenom, classe, tel in [("1", "Awa", "6e A", "771234567"),
                                       ("2", "Moussa", "6e A", "762345678"),
                                       ("3", "Fatou", "6e B", "781234567")]:
        eleves.inscrire(conn, zk_id, "Test", prenom, classe, tel, INSCRIPTION,
                        config.abonnement["essai_jours"])
    conn.commit()
