"""Fiches élèves : inscription, import depuis un fichier CSV (Excel), téléphones."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
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

    existe = conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if existe:
        if not existe["actif_scolarite"] and cle_nom(existe["nom"], existe["prenom"]) != cle_nom(nom, prenom):
            # Numéro d'un ancien élève donné à un nouveau : l'historique de l'ancien
            # (pointages, absences) lui serait attribué, et les doigts de l'ancien
            # sont peut-être encore sur le boîtier sous ce numéro.
            raise ValueError(
                f"le numéro {zk_user_id} était celui de {existe['prenom']} {existe['nom']}, parti(e) "
                f"de l'école. Donne un nouveau numéro à {prenom} {nom} (pt rentree indique le "
                "prochain numéro libre).")
        conn.execute(
            "UPDATE eleves SET nom = ?, prenom = ?, classe = ?, tel_parent = ?, actif_scolarite = 1, "
            "date_depart = NULL WHERE id = ?",
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


def cle_nom(nom: str, prenom: str) -> str:
    """« Awa  DIOP » et « awa Diop », « Aïda » et « Aida » : la même personne."""
    texte = unicodedata.normalize("NFKD", f"{prenom} {nom}")
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return " ".join(texte.casefold().split())


def lire_csv(chemin: Path) -> list[tuple[int, dict[str, str]]]:
    """Lignes du CSV (séparateur ; ou ,) : [(numéro de ligne, {colonne: valeur})]."""
    texte = lire_texte(chemin)
    dialecte = csv.Sniffer().sniff(texte[:2048], delimiters=";,")
    lecteur = csv.DictReader(io.StringIO(texte), dialect=dialecte)
    manquantes = set(COLONNES) - set(lecteur.fieldnames or [])
    if manquantes:
        raise ValueError(f"colonnes manquantes dans le CSV : {', '.join(sorted(manquantes))}")
    return [(numero, {c: (ligne.get(c) or "").strip() for c in COLONNES})
            for numero, ligne in enumerate(lecteur, start=2)]  # ligne 1 = en-têtes


def importer_csv(conn: sqlite3.Connection, chemin: Path, aujourd_hui: date,
                 essai_jours: int) -> tuple[int, int, list[str]]:
    """Importe un CSV avec les colonnes de COLONNES.

    Une ligne invalide n'arrête pas l'import : elle est signalée et on continue.
    Renvoie (nombre créés, nombre mis à jour, liste des erreurs).
    """
    crees, maj, erreurs = 0, 0, []
    for numero_ligne, valeurs in lire_csv(chemin):
        try:
            if inscrire(conn, **valeurs, aujourd_hui=aujourd_hui, essai_jours=essai_jours) == "cree":
                crees += 1
            else:
                maj += 1
        except ValueError as e:
            erreurs.append(f"ligne {numero_ligne} : {e}")
    conn.commit()
    return crees, maj, erreurs


# --- Départs et rentrée -----------------------------------------------------------

def marquer_partis(conn: sqlite3.Connection, zk_ids: list[str], depart: date) -> list[sqlite3.Row]:
    """Élèves qui quittent l'école à partir du jour `depart` (premier jour d'absence prévue).

    Plus d'alerte d'absence pour eux, ils sortent des rapports à partir de ce jour.
    Rien n'est effacé : leur historique reste, et une réinscription (même nom, même
    numéro) les réactive. Renvoie les élèves trouvés. L'appelant fait le commit.
    """
    inconnus = [z for z in zk_ids
                if not conn.execute("SELECT 1 FROM eleves WHERE zk_user_id = ?", (z,)).fetchone()]
    if inconnus:
        raise ValueError(f"aucun élève avec le(s) numéro(s) {', '.join(inconnus)}")
    partis = []
    for zk_id in zk_ids:
        conn.execute("UPDATE eleves SET actif_scolarite = 0, date_depart = ? "
                     "WHERE zk_user_id = ? AND actif_scolarite = 1", (depart.isoformat(), zk_id))
        partis.append(conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_id,)).fetchone())
    return partis


def prochain_numero_libre(conn: sqlite3.Connection) -> int:
    """Après le plus grand numéro d'élève jamais donné (partis compris) : jamais de réutilisation."""
    numeros = [int(r[0]) for r in conn.execute("SELECT zk_user_id FROM eleves") if r[0].isdigit()]
    personnel = {r[0] for r in conn.execute("SELECT zk_user_id FROM personnel")}
    numero = max(numeros, default=0) + 1
    while str(numero) in personnel:
        numero += 1
    return numero


@dataclass
class Rentree:
    """Ce que la liste de la nouvelle année change par rapport à la base."""
    nouveaux: list[dict] = field(default_factory=list)
    maintenus: int = 0
    changent_de_classe: int = 0
    revenants: list[sqlite3.Row] = field(default_factory=list)  # partis, de retour cette année
    partants: list[sqlite3.Row] = field(default_factory=list)   # inscrits mais absents de la liste
    erreurs: list[str] = field(default_factory=list)
    actifs_avant: int = 0

    @property
    def trop_de_partants(self) -> bool:
        """Plus de la moitié de l'école sur le départ : fichier incomplet, plutôt qu'un exode."""
        return self.actifs_avant > 0 and len(self.partants) > self.actifs_avant / 2


def rentree(conn: sqlite3.Connection, chemin: Path, jour: date, essai_jours: int,
            appliquer: bool) -> Rentree:
    """La liste COMPLÈTE des élèves de la nouvelle année remplace l'ancienne.

    Présents dans la liste : créés, ou mis à jour (classe, téléphone), ou réactivés.
    Inscrits mais absents de la liste : marqués partis à partir de `jour`.

    Avec appliquer=False, rien n'est gardé : le calcul est fait pour de vrai puis
    annulé (rollback). L'aperçu montré à l'école est donc exactement ce qui se passera.
    Une seule ligne en erreur et rien n'est appliqué : un élève dont la ligne est
    fausse serait sinon marqué parti.
    """
    conn.commit()  # rien d'autre en cours : le rollback final n'annule que la rentrée
    avant = {r["zk_user_id"]: r for r in conn.execute("SELECT * FROM eleves")}
    resultat = Rentree(actifs_avant=sum(1 for r in avant.values() if r["actif_scolarite"]))
    vus: dict[str, int] = {}
    try:
        for numero_ligne, valeurs in lire_csv(chemin):
            zk_id = valeurs["zk_user_id"]
            if zk_id in vus:
                resultat.erreurs.append(f"ligne {numero_ligne} : numéro {zk_id} déjà utilisé "
                                        f"ligne {vus[zk_id]} (deux élèves, un seul numéro)")
                continue
            vus[zk_id] = numero_ligne
            try:
                inscrire(conn, **valeurs, aujourd_hui=jour, essai_jours=essai_jours)
            except ValueError as e:
                resultat.erreurs.append(f"ligne {numero_ligne} : {e}")
                continue
            ancien = avant.get(zk_id)
            if ancien is None:
                resultat.nouveaux.append(valeurs)
            elif not ancien["actif_scolarite"]:
                resultat.revenants.append(ancien)
            else:
                resultat.maintenus += 1
                resultat.changent_de_classe += ancien["classe"] != valeurs["classe"]

        partants = sorted((r for z, r in avant.items() if r["actif_scolarite"] and z not in vus),
                          key=lambda r: (r["classe"], r["nom"], r["prenom"]))
        resultat.partants = partants
        marquer_partis(conn, [r["zk_user_id"] for r in partants], jour)
    except Exception:
        conn.rollback()
        raise
    if appliquer and not resultat.erreurs:
        conn.commit()
    else:
        conn.rollback()
    return resultat
