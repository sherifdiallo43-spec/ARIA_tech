"""Mises à jour du logiciel dans les écoles.

Deux moments, deux PC :

1. Chez ARIA_tech, sur le PC de développement : `pt creer-paquet` vérifie que tout
   est commité et que les tests passent, puis fabrique `dist/ARIA_point_1.1.0.zip`.
   Le paquet contient les fichiers suivis par git et un manifeste (paquet.json) qui
   donne la version et l'empreinte SHA-256 de chaque fichier.

2. À l'école : `pt mettre-a-jour ARIA_point_1.1.0.zip`
   - vérifie le paquet (fichier complet, non modifié, version plus récente) ;
   - vérifie que le service et le tableau de bord sont arrêtés, et les empêche de
     redémarrer pendant l'installation ;
   - met de côté la base et les fichiers qui vont changer (backups/mise_a_jour_...) ;
   - installe les nouveaux fichiers, et les bibliothèques si requirements.txt a changé ;
   - lance le NOUVEAU logiciel dans un programme séparé (`pt verifier-installation`) :
     il lit la configuration de l'école, met la base à jour et la contrôle ;
   - si une seule étape échoue : tout est remis comme avant, automatiquement.

`pt annuler-mise-a-jour` revient à la version précédente plus tard, si un problème
apparaît après coup (en cours de journée par exemple).

Une mise à jour ne touche JAMAIS à ce qui appartient à l'école : config.toml (ses
réglages), .env (ses secrets), data/ (la base), backups/, archives/, rapports/, logs/.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db, verrou
from .config import CONFIG_REFERENCE, Config, lire_toml

NOM_LOGICIEL = "ARIA_point"
MANIFESTE = "paquet.json"
# Manifeste de la version installée, à la racine du logiciel. Même nom que dans le zip :
# une école installée en dézippant un paquet connaît donc tout de suite sa version.
INSTALLE = MANIFESTE
NOTES = "NOTES_DE_VERSION.md"
CONFIG = "config.toml"
PREFIXE_SECOURS = "mise_a_jour_"
SECOURS_GARDES = 5                  # dossiers de secours conservés (les plus récents)

# Propres à chaque école ou au PC de développement : un paquet n'y écrit jamais.
PROTEGES = {".env", ".git", ".venv", "data", "backups", "archives", "rapports", "logs",
            "dist", INSTALLE, CONFIG_REFERENCE}

FORMAT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ErreurMiseAJour(ValueError):
    """Mise à jour refusée ou ratée. Le message dit quoi faire."""


# --- Versions ---------------------------------------------------------------------

def cle_version(version: str) -> tuple[int, ...]:
    """ "1.10.0" -> (1, 10, 0) : compare correctement 1.10.0 et 1.9.0 (pas en texte)."""
    if not FORMAT_VERSION.match(version):
        raise ErreurMiseAJour(f"numéro de version invalide « {version} » (attendu : 1.2.3).")
    return tuple(int(x) for x in version.split("."))


def version_installee(racine: Path) -> str:
    """Version écrite dans pointage/__init__.py du dossier `racine` ("0.0.0" si absente)."""
    texte = (racine / "pointage" / "__init__.py").read_text(encoding="utf-8")
    trouve = re.search(r'^__version__\s*=\s*"([^"]+)"', texte, re.MULTILINE)
    return trouve.group(1) if trouve else "0.0.0"


def _sha256(donnees: bytes) -> str:
    return hashlib.sha256(donnees).hexdigest()


# --- Fabrication du paquet (PC de développement) ----------------------------------

def _git(racine: Path, *args: str) -> str:
    resultat = subprocess.run(["git", *args], cwd=racine, capture_output=True, text=True,
                              encoding="utf-8")
    if resultat.returncode != 0:
        raise ErreurMiseAJour(f"git {' '.join(args)} a échoué : {resultat.stderr.strip()}")
    return resultat.stdout


def verifier_depot_propre(racine: Path) -> None:
    """Un paquet doit correspondre exactement à un commit : rien de modifié en dehors."""
    modifies = _git(racine, "status", "--porcelain").rstrip()
    if modifies:
        raise ErreurMiseAJour("des fichiers ne sont pas commités. Commite (ou annule) d'abord :\n"
                              + modifies)


def fichiers_suivis(racine: Path) -> list[str]:
    return [f for f in _git(racine, "ls-files").splitlines() if f]


def commit_actuel(racine: Path) -> str:
    return _git(racine, "rev-parse", "--short", "HEAD").strip()


def notes_de_version(texte: str, apres: str, jusqu_a: str) -> str:
    """Sections « ## X.Y.Z » de NOTES_DE_VERSION.md pour apres < X.Y.Z <= jusqu_a."""
    garde, gardees = False, []
    for ligne in texte.splitlines():
        titre = re.match(r"^##\s+(\d+\.\d+\.\d+)", ligne)
        if titre:
            v = cle_version(titre.group(1))
            garde = cle_version(apres) < v <= cle_version(jusqu_a)
        if garde:
            gardees.append(ligne)
    return "\n".join(gardees).strip()


def construire_paquet(racine: Path, fichiers: list[str], version: str, commit: str,
                      dossier_sortie: Path, maintenant: datetime | None = None) -> Path:
    """Fabrique NOM_LOGICIEL_version.zip avec son manifeste. Renvoie le chemin du zip."""
    cle_version(version)
    maintenant = maintenant or datetime.now()
    interdits = [f for f in fichiers if _premier_dossier(f) in PROTEGES]
    if interdits:
        raise ErreurMiseAJour("ces fichiers ne doivent jamais partir dans un paquet : "
                              + ", ".join(interdits))
    if NOTES in fichiers:
        notes = (racine / NOTES).read_text(encoding="utf-8")
        if not re.search(rf"^##\s+{re.escape(version)}\b", notes, re.MULTILINE):
            raise ErreurMiseAJour(f"ajoute une section « ## {version} » dans {NOTES} : "
                                  "c'est ce que l'école lira à l'installation.")

    contenus = {f: (racine / f).read_bytes() for f in fichiers}
    manifeste = {
        "logiciel": NOM_LOGICIEL,
        "version": version,
        "version_base": db.VERSION_BASE,
        "commit": commit,
        "cree_le": maintenant.isoformat(timespec="seconds"),
        "fichiers": {f: _sha256(c) for f, c in sorted(contenus.items())},
    }

    dossier_sortie.mkdir(parents=True, exist_ok=True)
    chemin = dossier_sortie / f"{NOM_LOGICIEL}_{version}.zip"
    if chemin.exists():
        # Deux paquets différents sous le même numéro : impossible de savoir lequel
        # une école a reçu. On oblige à changer de numéro.
        with zipfile.ZipFile(chemin) as existant:
            deja = json.loads(existant.read(MANIFESTE))
        if deja.get("commit") != commit:
            raise ErreurMiseAJour(
                f"un paquet {version} existe déjà (commit {deja.get('commit')}). Augmente "
                "__version__ dans pointage/__init__.py pour cette nouvelle version.")
    temporaire = chemin.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporaire, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFESTE, json.dumps(manifeste, indent=2, ensure_ascii=False))
        for nom, contenu in contenus.items():
            z.writestr(nom, contenu)
    temporaire.replace(chemin)
    return chemin


# --- Lecture et contrôle d'un paquet ----------------------------------------------

@dataclass
class Paquet:
    version: str
    version_base: int
    commit: str
    cree_le: str
    fichiers: dict[str, str]          # chemin -> empreinte SHA-256
    contenus: dict[str, bytes] = field(repr=False)


def _premier_dossier(nom: str) -> str:
    return nom.split("/", 1)[0]


def _chemin_sur(nom: str) -> bool:
    """Refuse tout chemin qui sortirait du dossier du logiciel ou toucherait aux données."""
    parties = nom.split("/")
    return (bool(nom) and "\\" not in nom and ":" not in nom and not nom.startswith("/")
            and all(p not in ("", ".", "..") for p in parties)
            and parties[0] not in PROTEGES)


def ouvrir_paquet(chemin_zip: Path | str) -> Paquet:
    """Lit et contrôle entièrement le paquet AVANT de toucher à quoi que ce soit."""
    chemin_zip = Path(chemin_zip)
    if not chemin_zip.is_file():
        raise ErreurMiseAJour(f"fichier introuvable : {chemin_zip}")
    try:
        with zipfile.ZipFile(chemin_zip) as z:
            noms = set(z.namelist())
            if MANIFESTE not in noms:
                raise ErreurMiseAJour(f"{chemin_zip.name} n'est pas un paquet {NOM_LOGICIEL}.")
            manifeste = json.loads(z.read(MANIFESTE))
            contenus = {n: z.read(n) for n in noms - {MANIFESTE}}
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError) as e:
        raise ErreurMiseAJour(f"{chemin_zip.name} est abîmé ou incomplet ({e}). "
                              "Télécharge-le à nouveau.") from None

    if manifeste.get("logiciel") != NOM_LOGICIEL:
        raise ErreurMiseAJour(f"{chemin_zip.name} n'est pas un paquet {NOM_LOGICIEL}.")
    cle_version(manifeste["version"])
    fichiers = manifeste["fichiers"]
    dangereux = [n for n in fichiers if not _chemin_sur(n)]
    if dangereux:
        raise ErreurMiseAJour("paquet refusé, chemins interdits : " + ", ".join(dangereux))
    if set(fichiers) != set(contenus):
        raise ErreurMiseAJour(f"{chemin_zip.name} ne correspond pas à son manifeste "
                              "(fichiers manquants ou en trop).")
    abimes = [n for n, empreinte in fichiers.items() if _sha256(contenus[n]) != empreinte]
    if abimes:
        raise ErreurMiseAJour(f"{chemin_zip.name} est abîmé ou a été modifié : "
                              + ", ".join(abimes[:10]))
    return Paquet(manifeste["version"], int(manifeste["version_base"]), manifeste["commit"],
                  manifeste["cree_le"], fichiers, contenus)


def nouveaux_reglages(reference: dict, ecole: dict, prefixe: str = "") -> list[str]:
    """Réglages du config.toml livré qui manquent dans celui de l'école ("section.cle")."""
    manquants = []
    for cle, valeur in reference.items():
        nom = f"{prefixe}{cle}"
        if cle not in ecole:
            manquants.append(f"[{nom}]" if isinstance(valeur, dict) else nom)
        elif isinstance(valeur, dict) and isinstance(ecole[cle], dict):
            manquants += nouveaux_reglages(valeur, ecole[cle], f"{nom}.")
    return manquants


# --- Vérification du logiciel installé ----------------------------------------------

def verifier_installation(config: Config) -> list[str]:
    """Contrôle que CE code tourne avec la configuration et la base de l'école.

    Lancée par `pt mettre-a-jour` dans un programme séparé, avec le nouveau code.
    Lève une exception au moindre problème. Renvoie un petit compte rendu.
    """
    import pointage

    # Chaque module est chargé : une bibliothèque manquante ou une erreur de
    # syntaxe se voit ici, et pas à 7 h 30 devant le portail.
    for module in pkgutil.iter_modules(pointage.__path__):
        importlib.import_module(f"pointage.{module.name}")

    conn = db.connecter(config.chemin("base"))
    try:
        db.initialiser(conn)  # applique les migrations de la base
        controle = conn.execute("PRAGMA quick_check").fetchone()[0]
        if controle != "ok":
            raise RuntimeError(f"base abîmée : {controle}")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        comptes = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        version = db.version_base(conn)
    finally:
        conn.close()
    return [f"Logiciel {pointage.__version__}, base en version {version}.",
            f"Élèves : {comptes.get('eleves', 0)}, personnel : {comptes.get('personnel', 0)}, "
            f"pointages : {comptes.get('pointages', 0)}."]


def verifier_par_sous_processus(racine: Path) -> tuple[bool, str]:
    """Lance `main.py verifier-installation` avec le code fraîchement installé.

    Un programme séparé est indispensable : celui qui installe a encore l'ancien
    code en mémoire, il ne peut pas tester le nouveau lui-même.
    """
    try:
        resultat = subprocess.run([sys.executable, "main.py", "verifier-installation"], cwd=racine,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return False, "la vérification ne s'est pas terminée en 5 minutes."
    return resultat.returncode == 0, (resultat.stdout + resultat.stderr).strip()


def installer_bibliotheques(racine: Path) -> None:
    """pip install -r requirements.txt (internet nécessaire)."""
    resultat = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                              cwd=racine, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    if resultat.returncode != 0:
        fin = "\n".join((resultat.stdout + resultat.stderr).strip().splitlines()[-15:])
        raise RuntimeError("installation des bibliothèques impossible (internet coupé ?) :\n" + fin)


# --- Installation à l'école -------------------------------------------------------

@dataclass
class Resultat:
    ancienne: str
    nouvelle: str
    secours: Path
    nouveaux_reglages: list[str]
    notes: str
    verification: str


def _copier_base(source: Path, cible: Path) -> None:
    """Copie cohérente d'une base SQLite (API backup), même si un journal WAL traîne."""
    src, dst = sqlite3.connect(source), sqlite3.connect(cible)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _ecrire(chemin: Path, contenu: bytes) -> None:
    """Écrit d'abord à côté puis remplace : jamais de fichier à moitié écrit."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_name(chemin.name + ".maj")
    temporaire.write_bytes(contenu)
    os.replace(temporaire, chemin)


def _dossier_secours(config: Config, ancienne: str, nouvelle: str, maintenant: datetime) -> Path:
    base = config.chemin("sauvegardes") / (
        f"{PREFIXE_SECOURS}{maintenant:%Y-%m-%d_%H%M%S}_{ancienne}_vers_{nouvelle}")
    dossier, n = base, 1
    while dossier.exists():
        n += 1
        dossier = base.with_name(f"{base.name}_{n}")
    dossier.mkdir(parents=True)
    return dossier


def _nettoyer_secours(config: Config) -> None:
    dossiers = sorted(config.chemin("sauvegardes").glob(f"{PREFIXE_SECOURS}*"))
    for ancien in dossiers[:-SECOURS_GARDES]:
        shutil.rmtree(ancien, ignore_errors=True)


def _prendre_verrous(config: Config) -> list:
    pris = []
    try:
        for nom in (verrou.SERVICE, verrou.WEB):
            pris.append(verrou.prendre(config, nom))
    except verrou.DejaEnCours as e:
        for v in pris:
            verrou.liberer(v)
        quoi = "le service (pt run)" if str(e) == verrou.SERVICE else "le tableau de bord (pt web)"
        raise ErreurMiseAJour(
            f"{quoi} tourne encore. Arrête-le d'abord (Planificateur de tâches > clic droit "
            "sur la tâche > Terminer, ou Ctrl+C dans sa fenêtre), puis relance.") from None
    return pris


def _restaurer(racine: Path, secours: Path, journal: dict, config: Config, avec_base: bool) -> None:
    """Remet les fichiers (et la base si demandé) tels qu'ils étaient avant la mise à jour."""
    code = secours / "code"
    for nom in journal["remplaces"] + journal["supprimes"]:
        cible = racine / nom
        cible.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(code / nom, cible)
    for nom in journal["crees"]:
        (racine / nom).unlink(missing_ok=True)
    if (code / INSTALLE).exists():
        shutil.copy2(code / INSTALLE, racine / INSTALLE)
    else:
        (racine / INSTALLE).unlink(missing_ok=True)
    if avec_base and (secours / "pointage.db").exists():
        _copier_base(secours / "pointage.db", config.chemin("base"))


def appliquer(racine: Path, chemin_zip: Path | str, config: Config, *, forcer: bool = False,
              verifier: Callable[[Path], tuple[bool, str]] = verifier_par_sous_processus,
              installer: Callable[[Path], None] = installer_bibliotheques,
              etape: Callable[[str], None] = lambda message: None,
              maintenant: datetime | None = None) -> Resultat:
    """Installe un paquet à l'école. En cas d'échec, tout est remis comme avant."""
    maintenant = maintenant or datetime.now()
    if (racine / ".git").exists() and not forcer:
        raise ErreurMiseAJour("ce dossier est un dossier de développement (git) : on n'y "
                              "installe pas de paquet. Utilise git pour le mettre à jour.")
    paquet = ouvrir_paquet(chemin_zip)
    ancienne = version_installee(racine)
    if cle_version(paquet.version) <= cle_version(ancienne) and not forcer:
        raise ErreurMiseAJour(
            f"la version {ancienne} est installée, le paquet est en {paquet.version}. "
            "Rien à faire" + (" (déjà installée)." if paquet.version == ancienne else
                              " : le paquet est plus ancien."))
    etape(f"Paquet vérifié : version {paquet.version} (installée : {ancienne}).")

    verrous = _prendre_verrous(config)
    try:
        return _installer(racine, paquet, ancienne, config, verifier, installer, etape, maintenant)
    finally:
        for v in verrous:
            verrou.liberer(v)


def _installer(racine, paquet, ancienne, config, verifier, installer, etape, maintenant) -> Resultat:
    base = config.chemin("base")
    config_ecole = racine / CONFIG

    # 1. Ce qui va changer. Les réglages de l'école restent : le config.toml livré est
    # déposé à côté, comme référence pour les réglages qui lui manqueraient.
    a_ecrire = dict(paquet.contenus)
    if CONFIG in a_ecrire and config_ecole.exists():
        a_ecrire[CONFIG_REFERENCE] = a_ecrire.pop(CONFIG)
    ancien_manifeste = {}
    if (racine / INSTALLE).exists():
        ancien_manifeste = json.loads((racine / INSTALLE).read_text(encoding="utf-8"))
    # Fichiers d'une ancienne version qui n'existent plus dans la nouvelle.
    a_supprimer = sorted(n for n in ancien_manifeste.get("fichiers", {})
                         if n not in paquet.fichiers and n != CONFIG and _chemin_sur(n)
                         and (racine / n).exists())
    journal = {
        "ancienne": ancienne,
        "nouvelle": paquet.version,
        "date": maintenant.isoformat(timespec="seconds"),
        "remplaces": sorted(n for n in a_ecrire if (racine / n).exists()),
        "crees": sorted(n for n in a_ecrire if not (racine / n).exists()),
        "supprimes": a_supprimer,
        "version_base_avant": None,
        "statut": "en_cours",
    }

    # 2. Mise de côté : base, fichiers remplacés ou supprimés, manifeste actuel.
    secours = _dossier_secours(config, ancienne, paquet.version, maintenant)
    if base.exists():
        _copier_base(base, secours / "pointage.db")
        conn = sqlite3.connect(secours / "pointage.db")
        journal["version_base_avant"] = db.version_base(conn)
        conn.close()
    for nom in journal["remplaces"] + journal["supprimes"] + [INSTALLE]:
        if (racine / nom).exists():
            (secours / "code" / nom).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(racine / nom, secours / "code" / nom)
    _ecrire_journal(secours, journal)
    etape(f"Base et fichiers actuels mis de côté dans {secours}.")

    # 3. Installation, vérification, et retour en arrière au moindre problème.
    exigences_avant = (secours / "code" / "requirements.txt")
    try:
        for nom, contenu in a_ecrire.items():
            _ecrire(racine / nom, contenu)
        for nom in a_supprimer:
            (racine / nom).unlink()
        etape(f"{len(a_ecrire)} fichier(s) installé(s), {len(a_supprimer)} retiré(s).")

        if "requirements.txt" in a_ecrire and (
                not exigences_avant.exists()
                or _sans_cr(exigences_avant.read_bytes()) != _sans_cr(a_ecrire["requirements.txt"])):
            etape("Nouvelles bibliothèques : installation (internet nécessaire)...")
            installer(racine)

        etape("Vérification du nouveau logiciel avec la configuration et la base de l'école...")
        ok, sortie = verifier(racine)
        if not ok:
            raise RuntimeError("la vérification a échoué :\n" + sortie)
    except Exception as e:
        etape("PROBLÈME : retour à la version précédente...")
        try:
            _restaurer(racine, secours, journal, config, avec_base=True)
        except Exception as e2:
            raise ErreurMiseAJour(
                f"la mise à jour a échoué ({e}) ET le retour en arrière aussi ({e2}). "
                f"Ne lance pas le service. Tout l'ancien état est dans {secours} "
                "(dossier code/ et pointage.db) : appelle ARIA_tech.") from e2
        journal["statut"] = "echec"
        _ecrire_journal(secours, journal)
        raise ErreurMiseAJour(f"mise à jour vers {paquet.version} annulée, la version {ancienne} "
                              f"est remise en place et la base est intacte.\nCause : {e}") from e

    manifeste = {"logiciel": NOM_LOGICIEL, "version": paquet.version,
                 "version_base": paquet.version_base, "commit": paquet.commit,
                 "cree_le": paquet.cree_le, "fichiers": paquet.fichiers}
    (racine / INSTALLE).write_text(json.dumps(manifeste, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    journal["statut"] = "installee"
    _ecrire_journal(secours, journal)
    _nettoyer_secours(config)

    reglages = []
    if CONFIG in paquet.contenus and config_ecole.exists():
        reglages = nouveaux_reglages(tomllib.loads(paquet.contenus[CONFIG].decode("utf-8-sig")),
                                     lire_toml(config_ecole))
    notes = ""
    if NOTES in paquet.contenus:
        notes = notes_de_version(paquet.contenus[NOTES].decode("utf-8"), ancienne, paquet.version)
    return Resultat(ancienne, paquet.version, secours, reglages, notes, sortie)


def _sans_cr(contenu: bytes) -> bytes:
    """Mêmes lignes, que le fichier ait des fins de ligne Windows (\\r\\n) ou non."""
    return contenu.replace(b"\r\n", b"\n")


def _ecrire_journal(secours: Path, journal: dict) -> None:
    (secours / "secours.json").write_text(json.dumps(journal, indent=2, ensure_ascii=False),
                                          encoding="utf-8")


# --- Retour à la version précédente -------------------------------------------------

@dataclass
class Annulation:
    secours: Path
    journal: dict
    base_a_restaurer: bool   # la base a changé de structure : il faut reprendre l'ancienne


def preparer_annulation(racine: Path, config: Config) -> Annulation:
    """Trouve la dernière mise à jour installée et dit ce que l'annulation impliquera."""
    candidats = []
    for dossier in config.chemin("sauvegardes").glob(f"{PREFIXE_SECOURS}*"):
        fichier = dossier / "secours.json"
        if fichier.exists():
            journal = json.loads(fichier.read_text(encoding="utf-8"))
            if journal.get("statut") == "installee":
                candidats.append((journal["date"], dossier, journal))
    if not candidats:
        raise ErreurMiseAJour("aucune mise à jour à annuler.")
    _, secours, journal = max(candidats, key=lambda c: c[0])
    installee = version_installee(racine)
    if installee != journal["nouvelle"]:
        raise ErreurMiseAJour(f"la dernière mise à jour connue mène à {journal['nouvelle']}, mais "
                              f"la version {installee} est installée. Annulation impossible.")

    base_a_restaurer = False
    if journal.get("version_base_avant") is not None and config.chemin("base").exists():
        conn = sqlite3.connect(config.chemin("base"))
        try:
            base_a_restaurer = db.version_base(conn) != journal["version_base_avant"]
        finally:
            conn.close()
    return Annulation(secours, journal, base_a_restaurer)


def annuler(racine: Path, config: Config, annulation: Annulation) -> None:
    """Remet la version précédente.

    Si la structure de la base n'a pas changé, la base actuelle est gardée telle quelle :
    aucun pointage n'est perdu. Sinon, l'ancien code ne saurait pas la lire, on reprend
    la base mise de côté avant la mise à jour (la base actuelle est elle aussi gardée).
    """
    verrous = _prendre_verrous(config)
    try:
        if annulation.base_a_restaurer:
            _copier_base(config.chemin("base"), annulation.secours / "pointage_avant_annulation.db")
        _restaurer(racine, annulation.secours, annulation.journal, config,
                   avec_base=annulation.base_a_restaurer)
        annulation.journal["statut"] = "annulee"
        _ecrire_journal(annulation.secours, annulation.journal)
    finally:
        for v in verrous:
            verrou.liberer(v)
