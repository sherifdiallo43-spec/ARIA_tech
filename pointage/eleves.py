"""Fiches élèves : inscription, import depuis un fichier CSV (Excel), téléphones."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

COLONNES = ("zk_user_id", "nom", "prenom", "classe", "tel_parent")

# Format international E.164 : un "+", puis 8 à 15 chiffres.
TEL_INTERNATIONAL = re.compile(r"^\+[1-9]\d{7,14}$")


def normaliser_telephone(brut: str) -> str:
    """Met un numéro au format international (+221...). Lève ValueError si impossible.

    Les écoles écrivent les numéros de mille façons. Toutes celles-ci donnent
    "+221771234567" : "77 123 45 67", "771234567", "00221771234567",
    "221 77 123 45 67", "+221 77-123-45-67".
    """
    tel = re.sub(r"[\s.\-()]", "", brut)
    if tel.startswith("00"):
        tel = "+" + tel[2:]
    elif re.fullmatch(r"[37]\d{8}", tel):          # numéro sénégalais sans indicatif
        tel = "+221" + tel
    elif re.fullmatch(r"221[37]\d{8}", tel):       # indicatif sans le "+"
        tel = "+" + tel
    if not TEL_INTERNATIONAL.fullmatch(tel):
        raise ValueError(f"numéro de téléphone invalide : {brut!r}")
    return tel


def inscrire(conn: sqlite3.Connection, zk_user_id: str, nom: str, prenom: str, classe: str,
             tel_parent: str, aujourd_hui: date, essai_jours: int) -> str:
    """Crée la fiche, ou la met à jour si ce numéro de pointeuse existe déjà.

    Une mise à jour ne touche pas à l'abonnement : réimporter la liste des
    élèves en cours d'année ne redonne pas d'essai gratuit.
    Renvoie "cree" ou "maj". L'appelant fait le commit.
    """
    champs = {"zk_user_id": zk_user_id, "nom": nom, "prenom": prenom,
              "classe": classe, "tel_parent": tel_parent}
    vides = [nom_champ for nom_champ, valeur in champs.items() if not valeur]
    if vides:
        raise ValueError(f"champ(s) vide(s) : {', '.join(vides)}")
    if conn.execute("SELECT 1 FROM personnel WHERE zk_user_id = ?", (zk_user_id,)).fetchone():
        raise ValueError(f"le numéro de pointeuse {zk_user_id} est déjà celui d'un membre du personnel")
    tel = normaliser_telephone(tel_parent)

    existe = conn.execute("SELECT id FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if existe:
        conn.execute(
            "UPDATE eleves SET nom = ?, prenom = ?, classe = ?, tel_parent = ?, actif_scolarite = 1 "
            "WHERE id = ?",
            (nom, prenom, classe, tel, existe["id"]),
        )
        return "maj"
    conn.execute(
        "INSERT INTO eleves (zk_user_id, nom, prenom, classe, tel_parent, date_inscription, essai_fin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (zk_user_id, nom, prenom, classe, tel, aujourd_hui.isoformat(),
         (aujourd_hui + timedelta(days=essai_jours)).isoformat()),
    )
    return "cree"


def lire_texte(chemin: Path) -> str:
    """Excel enregistre souvent en Windows-1252 et non en UTF-8 : on essaie les deux."""
    contenu = Path(chemin).read_bytes()
    try:
        return contenu.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contenu.decode("cp1252")


def importer_csv(conn: sqlite3.Connection, chemin: Path, aujourd_hui: date,
                 essai_jours: int) -> tuple[int, int, list[str]]:
    """Importe un CSV (séparateur ; ou ,) avec les colonnes de COLONNES.

    Une ligne invalide n'arrête pas l'import : elle est signalée et on continue.
    Renvoie (nombre créés, nombre mis à jour, liste des erreurs).
    """
    texte = lire_texte(chemin)
    dialecte = csv.Sniffer().sniff(texte[:2048], delimiters=";,")
    lecteur = csv.DictReader(io.StringIO(texte), dialect=dialecte)
    manquantes = set(COLONNES) - set(lecteur.fieldnames or [])
    if manquantes:
        raise ValueError(f"colonnes manquantes dans le CSV : {', '.join(sorted(manquantes))}")

    crees, maj, erreurs = 0, 0, []
    for numero_ligne, ligne in enumerate(lecteur, start=2):  # ligne 1 = en-têtes
        valeurs = {c: (ligne.get(c) or "").strip() for c in COLONNES}
        try:
            if inscrire(conn, **valeurs, aujourd_hui=aujourd_hui, essai_jours=essai_jours) == "cree":
                crees += 1
            else:
                maj += 1
        except ValueError as e:
            erreurs.append(f"ligne {numero_ligne} : {e}")
    conn.commit()
    return crees, maj, erreurs
