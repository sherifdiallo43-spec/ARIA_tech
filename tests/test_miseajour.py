"""Versions de la base, verrous, paquets et mises à jour à l'école."""
import json
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from pointage import db, miseajour, verrou
from pointage.config import CONFIG_REFERENCE, RACINE_PROJET, Config, charger, fusionner, lire_toml
from pointage.miseajour import ErreurMiseAJour

MOMENT = datetime(2026, 10, 25, 20, 0)

CONFIG_ECOLE = """
[ecole]
nom = "École Saint Exemple"
[notifications]
mode = "meta"
"""


def ok(racine):
    return True, "Logiciel vérifié."


# --- Version de la base -----------------------------------------------------------

def test_une_base_neuve_recoit_le_dernier_numero(conn):
    assert db.version_base(conn) == db.VERSION_BASE


def test_une_base_d_avant_la_numerotation_est_numerotee(tmp_path):
    ancienne = sqlite3.connect(tmp_path / "ancienne.db")
    ancienne.executescript(db.SCHEMA)  # structure actuelle, mais user_version = 0
    ancienne.close()
    conn = db.connecter(tmp_path / "ancienne.db")
    db.initialiser(conn)
    assert db.version_base(conn) == db.VERSION_BASE
    conn.close()


def test_un_logiciel_plus_ancien_refuse_une_base_plus_recente(conn):
    conn.execute(f"PRAGMA user_version = {db.VERSION_BASE + 1}")
    with pytest.raises(db.BaseTropRecente):
        db.initialiser(conn)


# --- Verrous ----------------------------------------------------------------------

def test_un_verrou_ne_peut_etre_pris_qu_une_fois(config):
    premier = verrou.prendre(config, verrou.SERVICE)
    with pytest.raises(verrou.DejaEnCours):
        verrou.prendre(config, verrou.SERVICE)
    verrou.liberer(premier)
    verrou.liberer(verrou.prendre(config, verrou.SERVICE))  # libéré : de nouveau disponible


def test_le_service_refuse_de_tourner_deux_fois(config):
    from pointage import service
    tenu = verrou.prendre(config, verrou.SERVICE)
    try:
        with pytest.raises(SystemExit, match="tourne déjà"):
            service.lancer(config)
    finally:
        verrou.liberer(tenu)


# --- Réglages : config de l'école complétée par la référence ------------------------

def test_fusion_garde_les_reglages_de_l_ecole_et_complete_les_manquants():
    reference = {"notifications": {"mode": "console", "nouveau": 5}, "neuve": {"a": 1},
                 "pointeuses": [{"ip": "1"}]}
    ecole = {"notifications": {"mode": "meta"}, "pointeuses": [{"ip": "2"}, {"ip": "3"}]}
    assert fusionner(reference, ecole) == {
        "notifications": {"mode": "meta", "nouveau": 5}, "neuve": {"a": 1},
        "pointeuses": [{"ip": "2"}, {"ip": "3"}]}


def test_charger_complete_avec_config_reference(tmp_path):
    shutil.copy(RACINE_PROJET / "config.toml", tmp_path / CONFIG_REFERENCE)
    reference = lire_toml(tmp_path / CONFIG_REFERENCE)
    ecole = dict(reference)
    ecole["ecole"] = {"nom": "École Saint Exemple"}
    del ecole["sauvegarde"]  # réglages d'une ancienne version, sans la section [sauvegarde]
    (tmp_path / "config.toml").write_text(_toml(ecole), encoding="utf-8")
    config = charger(tmp_path / "config.toml")
    assert config.nom_ecole == "École Saint Exemple"
    assert config.sauvegarde == reference["sauvegarde"]


def _toml(donnees: dict) -> str:
    """TOML minimal (assez pour les tests) : sections de valeurs simples."""
    def valeur(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            return "[" + ", ".join(valeur(x) for x in v) + "]"
        return str(v)

    lignes = []

    def section(nom, contenu):
        lignes.append(f"[{nom}]")
        for cle, v in contenu.items():
            if not isinstance(v, dict):
                lignes.append(f"{cle} = {valeur(v)}")
        for cle, v in contenu.items():
            if isinstance(v, dict):
                section(f"{nom}.{cle}", v)

    for nom, contenu in donnees.items():
        section(nom, contenu)
    return "\n".join(lignes) + "\n"


def test_config_enregistree_avec_bom_acceptee(tmp_path):
    (tmp_path / "config.toml").write_bytes("﻿".encode() + CONFIG_ECOLE.encode())
    assert lire_toml(tmp_path / "config.toml")["ecole"]["nom"] == "École Saint Exemple"


def test_nouveaux_reglages_listes():
    reference = {"ecole": {"nom": "x"}, "notifications": {"mode": "c", "nouveau": 1}, "neuve": {"a": 1}}
    ecole = {"ecole": {"nom": "y"}, "notifications": {"mode": "m"}}
    assert miseajour.nouveaux_reglages(reference, ecole) == ["notifications.nouveau", "[neuve]"]


# --- Paquets ----------------------------------------------------------------------

def test_versions_comparees_comme_des_nombres():
    assert miseajour.cle_version("1.10.0") > miseajour.cle_version("1.9.0")
    with pytest.raises(ErreurMiseAJour):
        miseajour.cle_version("1.2")


def fabriquer(tmp_path: Path, version: str, fichiers: dict[str, str], commit: str = "abc1234",
              notes: bool = True) -> Path:
    """Paquet `version` contenant `fichiers` (+ pointage/__init__.py et les notes)."""
    source = tmp_path / f"source_{version}_{commit}"
    tout = {"pointage/__init__.py": f'__version__ = "{version}"\n', **fichiers}
    if notes:
        tout[miseajour.NOTES] = f"# Notes\n\n## {version}\n\n- Nouveautés {version}.\n\n## 1.0.0\n\n- Début.\n"
    for nom, contenu in tout.items():
        (source / nom).parent.mkdir(parents=True, exist_ok=True)
        (source / nom).write_bytes(contenu.encode("utf-8"))
    return miseajour.construire_paquet(source, sorted(tout), version, commit, tmp_path / "dist", MOMENT)


def test_paquet_fabrique_puis_relu(tmp_path):
    chemin = fabriquer(tmp_path, "1.1.0", {"main.py": "print('v1.1')\n"})
    assert chemin.name == "ARIA_point_1.1.0.zip"
    paquet = miseajour.ouvrir_paquet(chemin)
    assert (paquet.version, paquet.commit, paquet.version_base) == ("1.1.0", "abc1234", db.VERSION_BASE)
    assert paquet.contenus["main.py"] == b"print('v1.1')\n"


def test_paquet_refuse_sans_notes_de_version(tmp_path):
    source = tmp_path / "source"
    (source / "pointage").mkdir(parents=True)
    (source / "pointage" / "__init__.py").write_text('__version__ = "1.1.0"\n')
    (source / miseajour.NOTES).write_text("## 1.0.0\n")
    with pytest.raises(ErreurMiseAJour, match="## 1.1.0"):
        miseajour.construire_paquet(source, ["pointage/__init__.py", miseajour.NOTES], "1.1.0",
                                    "abc", tmp_path / "dist")


def test_deux_paquets_differents_sous_le_meme_numero_refuses(tmp_path):
    fabriquer(tmp_path, "1.1.0", {"main.py": "a"}, commit="aaa")
    fabriquer(tmp_path, "1.1.0", {"main.py": "a"}, commit="aaa")  # même commit : refait sans souci
    with pytest.raises(ErreurMiseAJour, match="existe déjà"):
        fabriquer(tmp_path, "1.1.0", {"main.py": "b"}, commit="bbb")


def test_jamais_de_donnees_ou_de_secrets_dans_un_paquet(tmp_path):
    with pytest.raises(ErreurMiseAJour, match="jamais"):
        fabriquer(tmp_path, "1.1.0", {".env": "META_JETON=secret"})


def _reecrire_zip(chemin: Path, modifier) -> None:
    with zipfile.ZipFile(chemin) as z:
        contenus = {n: z.read(n) for n in z.namelist()}
    modifier(contenus)
    with zipfile.ZipFile(chemin, "w") as z:
        for nom, contenu in contenus.items():
            z.writestr(nom, contenu)


def test_paquet_modifie_refuse(tmp_path):
    chemin = fabriquer(tmp_path, "1.1.0", {"main.py": "print('ok')\n"})
    _reecrire_zip(chemin, lambda c: c.update({"main.py": b"print('pirate')\n"}))
    with pytest.raises(ErreurMiseAJour, match="modifié"):
        miseajour.ouvrir_paquet(chemin)


def test_paquet_qui_sort_du_dossier_refuse(tmp_path):
    chemin = fabriquer(tmp_path, "1.1.0", {"main.py": "x"})

    def evasion(contenus):
        manifeste = json.loads(contenus["paquet.json"])
        manifeste["fichiers"]["../evasion.py"] = manifeste["fichiers"]["main.py"]
        contenus["paquet.json"] = json.dumps(manifeste).encode()
        contenus["../evasion.py"] = b"x"

    _reecrire_zip(chemin, evasion)
    with pytest.raises(ErreurMiseAJour, match="interdits"):
        miseajour.ouvrir_paquet(chemin)


def test_fichier_qui_n_est_pas_un_zip(tmp_path):
    faux = tmp_path / "ARIA_point_1.1.0.zip"
    faux.write_text("pas un zip")
    with pytest.raises(ErreurMiseAJour, match="abîmé"):
        miseajour.ouvrir_paquet(faux)


def test_notes_de_toutes_les_versions_sautees():
    texte = "# Notes\n\n## 1.3.0\n- c\n\n## 1.2.0\n- b\n\n## 1.1.0\n- a\n"
    assert miseajour.notes_de_version(texte, "1.1.0", "1.3.0") == "## 1.3.0\n- c\n\n## 1.2.0\n- b"


# --- Installation à l'école -------------------------------------------------------

@pytest.fixture
def ecole(tmp_path, config):
    """Une école en version 1.0.0 (dossier = tmp_path, comme la config des tests), avec des données."""
    fichiers = {"pointage/__init__.py": '__version__ = "1.0.0"\n', "main.py": "print('v1.0')\n",
                "requirements.txt": "pyzk\n", "ancien.py": "# disparaît en 1.1.0\n"}
    for nom, contenu in fichiers.items():
        (tmp_path / nom).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / nom).write_text(contenu, encoding="utf-8")  # fins de ligne Windows, exprès
    (tmp_path / "config.toml").write_text(CONFIG_ECOLE, encoding="utf-8")
    # Manifeste de la 1.0.0, comme après une installation depuis un paquet.
    (tmp_path / "paquet.json").write_text(json.dumps({"version": "1.0.0", "fichiers": {
        n: "x" for n in fichiers}}), encoding="utf-8")
    conn = db.connecter(config.chemin("base"))
    db.initialiser(conn)
    conn.execute("INSERT INTO pointages (zk_user_id, horodatage, type, recu_le) "
                  "VALUES ('1', '2026-10-24 07:40:00', 'arrivee', '2026-10-24 07:40:10')")
    conn.commit()
    conn.close()
    return tmp_path


def paquet_1_1_0(tmp_path, **fichiers):
    contenu = {"main.py": "print('v1.1')\n", "pointage/nouveau.py": "X = 1\n",
               "requirements.txt": "pyzk\n",
               "config.toml": '[ecole]\nnom = "Exemple"\n[notifications]\nmode = "console"\nnouveau = 3\n'}
    contenu.update(fichiers)
    return fabriquer(tmp_path, "1.1.0", contenu)


def lire(chemin: Path) -> str:
    return chemin.read_text(encoding="utf-8")


def nb_pointages(config) -> int:
    conn = sqlite3.connect(config.chemin("base"))
    try:
        return conn.execute("SELECT COUNT(*) FROM pointages").fetchone()[0]
    finally:
        conn.close()


def test_mise_a_jour_reussie(ecole, config):
    installe = []
    resultat = miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok,
                                   installer=installe.append, maintenant=MOMENT)
    assert (resultat.ancienne, resultat.nouvelle) == ("1.0.0", "1.1.0")
    assert miseajour.version_installee(ecole) == "1.1.0"
    assert lire(ecole / "main.py") == "print('v1.1')\n"
    assert lire(ecole / "pointage" / "nouveau.py") == "X = 1\n"
    assert not (ecole / "ancien.py").exists()           # n'existe plus dans la 1.1.0
    assert lire(ecole / "config.toml") == CONFIG_ECOLE   # les réglages de l'école restent
    assert "nouveau = 3" in lire(ecole / CONFIG_REFERENCE)
    assert resultat.nouveaux_reglages == ["notifications.nouveau"]
    assert json.loads(lire(ecole / "paquet.json"))["version"] == "1.1.0"
    assert installe == []                                # requirements.txt inchangé : pas de pip
    assert "Nouveautés 1.1.0" in resultat.notes and "Début" not in resultat.notes
    assert nb_pointages(config) == 1
    # Mis de côté : la base et l'ancienne version des fichiers.
    assert (resultat.secours / "pointage.db").exists()
    assert lire(resultat.secours / "code" / "main.py") == "print('v1.0')\n"
    assert json.loads(lire(resultat.secours / "secours.json"))["statut"] == "installee"


def test_verification_ratee_tout_est_remis_comme_avant(ecole, config):
    def verification_qui_echoue(racine):
        # Le nouveau code a déjà touché à la base (migration) avant d'échouer.
        conn = sqlite3.connect(config.chemin("base"))
        conn.execute("DELETE FROM pointages")
        conn.commit()
        conn.close()
        return False, "ModuleNotFoundError: No module named 'truc'"

    avant = {n: lire(ecole / n) for n in ("main.py", "ancien.py", "config.toml", "paquet.json",
                                          "pointage/__init__.py")}
    with pytest.raises(ErreurMiseAJour, match="truc") as erreur:
        miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=verification_qui_echoue,
                            maintenant=MOMENT)
    assert "1.0.0 est remise en place" in str(erreur.value)
    assert {n: lire(ecole / n) for n in avant} == avant
    assert not (ecole / "pointage" / "nouveau.py").exists()
    assert not (ecole / CONFIG_REFERENCE).exists()
    assert nb_pointages(config) == 1  # base restaurée
    secours = next((ecole / "backups").glob("mise_a_jour_*"))
    assert json.loads(lire(secours / "secours.json"))["statut"] == "echec"


def test_nouvelles_bibliotheques_installees_et_echec_annule(ecole, config):
    def pip_sans_internet(racine):
        raise RuntimeError("pip : connexion impossible")

    with pytest.raises(ErreurMiseAJour, match="connexion impossible"):
        miseajour.appliquer(ecole, paquet_1_1_0(ecole, **{"requirements.txt": "pyzk\nnouvelle\n"}),
                            config, verifier=ok, installer=pip_sans_internet, maintenant=MOMENT)
    assert lire(ecole / "requirements.txt") == "pyzk\n"

    installe = []
    miseajour.appliquer(ecole, paquet_1_1_0(ecole, **{"requirements.txt": "pyzk\nnouvelle\n"}),
                        config, verifier=ok, installer=installe.append, maintenant=MOMENT)
    assert installe == [ecole]


def test_refusee_pendant_que_le_service_tourne(ecole, config):
    tenu = verrou.prendre(config, verrou.SERVICE)
    try:
        with pytest.raises(ErreurMiseAJour, match="service"):
            miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok)
    finally:
        verrou.liberer(tenu)
    assert miseajour.version_installee(ecole) == "1.0.0"


def test_le_service_ne_peut_pas_redemarrer_pendant_l_installation(ecole, config):
    def verification(racine):
        with pytest.raises(verrou.DejaEnCours):
            verrou.prendre(config, verrou.SERVICE)
        return True, ""

    miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=verification)
    verrou.liberer(verrou.prendre(config, verrou.SERVICE))  # rendu une fois fini


def test_meme_version_ou_plus_ancienne_refusee(ecole, config):
    miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok, maintenant=MOMENT)
    with pytest.raises(ErreurMiseAJour, match="déjà installée"):
        miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok)
    with pytest.raises(ErreurMiseAJour, match="plus ancien"):
        miseajour.appliquer(ecole, fabriquer(ecole, "1.0.5", {}), config, verifier=ok)


def test_refusee_dans_le_dossier_de_developpement(ecole, config):
    (ecole / ".git").mkdir()
    with pytest.raises(ErreurMiseAJour, match="développement"):
        miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok)


# --- Annulation plus tard ---------------------------------------------------------

def test_annuler_sans_changement_de_base_garde_les_nouveaux_pointages(ecole, config):
    miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=ok, maintenant=MOMENT)
    conn = sqlite3.connect(config.chemin("base"))
    conn.execute("INSERT INTO pointages (zk_user_id, horodatage, type, recu_le) "
                 "VALUES ('1', '2026-10-26 07:41:00', 'arrivee', '2026-10-26 07:41:05')")
    conn.commit()
    conn.close()

    annulation = miseajour.preparer_annulation(ecole, config)
    assert not annulation.base_a_restaurer
    miseajour.annuler(ecole, config, annulation)

    assert miseajour.version_installee(ecole) == "1.0.0"
    assert lire(ecole / "main.py") == "print('v1.0')\n"
    assert (ecole / "ancien.py").exists()
    assert not (ecole / "pointage" / "nouveau.py").exists()
    assert json.loads(lire(ecole / "paquet.json"))["version"] == "1.0.0"
    assert nb_pointages(config) == 2  # le pointage du lendemain est gardé
    with pytest.raises(ErreurMiseAJour, match="aucune"):
        miseajour.preparer_annulation(ecole, config)


def test_annuler_apres_changement_de_base_reprend_l_ancienne_base(ecole, config):
    conn = sqlite3.connect(config.chemin("base"))
    conn.execute("PRAGMA user_version = 0")  # l'école avait une base d'ancienne structure
    conn.close()

    def migration(racine):
        conn = sqlite3.connect(config.chemin("base"))
        conn.execute(f"PRAGMA user_version = {db.VERSION_BASE}")
        conn.execute("INSERT INTO pointages (zk_user_id, horodatage, type, recu_le) "
                     "VALUES ('2', '2026-10-26 07:45:00', 'arrivee', '2026-10-26 07:45:05')")
        conn.commit()
        conn.close()
        return True, ""

    miseajour.appliquer(ecole, paquet_1_1_0(ecole), config, verifier=migration, maintenant=MOMENT)
    annulation = miseajour.preparer_annulation(ecole, config)
    assert annulation.base_a_restaurer
    miseajour.annuler(ecole, config, annulation)
    assert nb_pointages(config) == 1
    gardee = sqlite3.connect(annulation.secours / "pointage_avant_annulation.db")
    assert gardee.execute("SELECT COUNT(*) FROM pointages").fetchone()[0] == 2
    gardee.close()


# --- De bout en bout, avec le vrai programme de vérification ------------------------

FICHIERS_REELS = ["main.py", "requirements.txt", "config.toml", miseajour.NOTES,
                  *[f"pointage/{p.name}" for p in (RACINE_PROJET / "pointage").glob("*.py")],
                  *[f"pointage/templates/{p.name}" for p in (RACINE_PROJET / "pointage/templates").iterdir()],
                  "pointage/static/style.css"]


def _copie_du_logiciel(dossier: Path, version: str) -> None:
    for nom in FICHIERS_REELS:
        (dossier / nom).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(RACINE_PROJET / nom, dossier / nom)
    (dossier / "pointage" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    notes = dossier / miseajour.NOTES
    notes.write_text(f"## {version}\n\n- Test.\n\n" + lire(notes), encoding="utf-8")


@pytest.fixture
def vraie_ecole(tmp_path):
    ecole = tmp_path / "ecole"
    _copie_du_logiciel(ecole, "1.0.0")
    return ecole


def _paquet_reel(tmp_path, version, modifier=None) -> Path:
    source = tmp_path / f"source_{version}"
    _copie_du_logiciel(source, version)
    if modifier:
        modifier(source)
    return miseajour.construire_paquet(source, FICHIERS_REELS, version, "e2e", tmp_path / "dist")


def test_bout_en_bout_avec_le_vrai_logiciel(tmp_path, vraie_ecole):
    config = charger(vraie_ecole / "config.toml")
    resultat = miseajour.appliquer(vraie_ecole, _paquet_reel(tmp_path, "1.0.1"), config)
    assert "Logiciel 1.0.1, base en version" in resultat.verification
    assert config.chemin("base").exists()  # la vérification a ouvert et migré la base


def test_bout_en_bout_un_paquet_casse_est_annule(tmp_path, vraie_ecole):
    def casser(source):
        with open(source / "pointage" / "reports.py", "a", encoding="utf-8") as f:
            f.write("\ndef erreur_de_syntaxe(:\n")

    config = charger(vraie_ecole / "config.toml")
    avant = lire(vraie_ecole / "pointage" / "reports.py")
    with pytest.raises(ErreurMiseAJour, match="SyntaxError"):
        miseajour.appliquer(vraie_ecole, _paquet_reel(tmp_path, "1.0.1", casser), config)
    assert lire(vraie_ecole / "pointage" / "reports.py") == avant
    assert miseajour.version_installee(vraie_ecole) == "1.0.0"
