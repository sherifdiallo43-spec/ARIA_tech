"""Personnel (professeurs, surveillants...) : inscription, emploi du temps, présence.

Différences avec les élèves :
- aucun message aux parents, aucun abonnement ;
- chacun a son emploi du temps (une ligne par cours, du lundi au samedi) ;
- autant de pointages que nécessaire dans la journée, en alternance
  arrivée, sortie, arrivée, sortie... (un prof peut partir entre deux cours).

Pour chaque cours de l'emploi du temps : à l'heure, en retard (de N minutes)
ou manqué. Heures = temps de présence (somme des intervalles arrivée -> sortie).
Une arrivée sans sortie (oubli) n'est pas comptée dans les heures, mais la
personne est considérée présente pour ses cours suivants de la journée.
"""
from __future__ import annotations

import calendar
import csv
import io
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .db import dt_texte, texte_dt
from .eleves import lire_texte

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
COLONNES = ("zk_user_id", "nom", "prenom", "fonction")
COLONNES_EDT = ("zk_user_id", "jour", "debut", "fin")


def _lire_csv(chemin: Path, obligatoires: tuple[str, ...]) -> csv.DictReader:
    texte = lire_texte(chemin)
    dialecte = csv.Sniffer().sniff(texte[:2048], delimiters=";,")
    lecteur = csv.DictReader(io.StringIO(texte), dialect=dialecte)
    manquantes = set(obligatoires) - set(lecteur.fieldnames or [])
    if manquantes:
        raise ValueError(f"colonnes manquantes dans le CSV : {', '.join(sorted(manquantes))}")
    return lecteur


# --- Inscription -----------------------------------------------------------------

def normaliser_heure(texte: str) -> str:
    """"8h", "8:00", "08h30" -> "08:00", "08:30". Lève ValueError si illisible."""
    brut = texte.strip().lower().replace("h", ":")
    if brut.endswith(":"):
        brut += "00"
    try:
        heures, minutes = (int(x) for x in brut.split(":"))
        return time(heures, minutes).strftime("%H:%M")
    except ValueError:
        raise ValueError(f"heure invalide : {texte!r} (exemple attendu : 08:00)") from None


def inscrire(conn: sqlite3.Connection, zk_user_id: str, nom: str, prenom: str, fonction: str,
             aujourd_hui: date) -> str:
    """Crée la fiche, ou la met à jour si ce numéro de pointeuse existe déjà.
    Renvoie "cree" ou "maj". L'appelant fait le commit."""
    champs = {"zk_user_id": zk_user_id, "nom": nom, "prenom": prenom, "fonction": fonction}
    vides = [nom_champ for nom_champ, valeur in champs.items() if not valeur]
    if vides:
        raise ValueError(f"champ(s) vide(s) : {', '.join(vides)}")
    if conn.execute("SELECT 1 FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone():
        raise ValueError(f"le numéro de pointeuse {zk_user_id} est déjà celui d'un élève")

    existe = conn.execute("SELECT id FROM personnel WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if existe:
        conn.execute("UPDATE personnel SET nom = ?, prenom = ?, fonction = ?, actif = 1 WHERE id = ?",
                     (nom, prenom, fonction, existe["id"]))
        return "maj"
    conn.execute("INSERT INTO personnel (zk_user_id, nom, prenom, fonction, date_inscription) "
                 "VALUES (?, ?, ?, ?, ?)", (zk_user_id, nom, prenom, fonction, aujourd_hui.isoformat()))
    return "cree"


def importer_csv(conn: sqlite3.Connection, chemin: Path,
                 aujourd_hui: date) -> tuple[int, int, list[str]]:
    """Importe le personnel (zk_user_id, nom, prenom, fonction).
    Une ligne invalide n'arrête pas l'import. Renvoie (créés, mis à jour, erreurs)."""
    crees, maj, erreurs = 0, 0, []
    for numero_ligne, ligne in enumerate(_lire_csv(chemin, COLONNES), start=2):
        valeurs = {c: (ligne.get(c) or "").strip() for c in COLONNES}
        try:
            if inscrire(conn, **valeurs, aujourd_hui=aujourd_hui) == "cree":
                crees += 1
            else:
                maj += 1
        except ValueError as e:
            erreurs.append(f"ligne {numero_ligne} : {e}")
    conn.commit()
    return crees, maj, erreurs


def _lire_creneau(conn: sqlite3.Connection, ligne: dict) -> dict:
    zk_id = (ligne.get("zk_user_id") or "").strip()
    if not trouver(conn, zk_id):
        raise ValueError(f"numéro {zk_id!r} inconnu : importe d'abord ce membre du personnel")
    jour = (ligne.get("jour") or "").strip().lower()
    if jour not in JOURS:
        raise ValueError(f"jour invalide : {ligne.get('jour')!r} (lundi ... samedi)")
    debut = normaliser_heure(ligne.get("debut") or "")
    fin = normaliser_heure(ligne.get("fin") or "")
    if debut >= fin:
        raise ValueError(f"le cours finit ({fin}) avant de commencer ({debut})")
    return {"zk_user_id": zk_id, "jour": jour, "debut": debut, "fin": fin,
            "classe": (ligne.get("classe") or "").strip(), "matiere": (ligne.get("matiere") or "").strip()}


def importer_emploi_du_temps(conn: sqlite3.Connection, chemin: Path) -> tuple[int, int, list[str]]:
    """Importe l'emploi du temps : une ligne par cours (zk_user_id, jour, debut, fin,
    et facultativement classe, matiere).

    Pour chaque membre présent dans le fichier, ses anciens cours sont REMPLACÉS
    par ceux du fichier. Un membre avec au moins une ligne en erreur garde son
    ancien emploi du temps (jamais de demi-emploi du temps).
    Renvoie (nombre de cours importés, nombre de membres mis à jour, erreurs).
    """
    par_membre: dict[str, list[dict]] = {}
    en_erreur: set[str] = set()
    erreurs = []
    for numero_ligne, ligne in enumerate(_lire_csv(chemin, COLONNES_EDT), start=2):
        try:
            creneau = _lire_creneau(conn, ligne)
            par_membre.setdefault(creneau["zk_user_id"], []).append(creneau)
        except ValueError as e:
            erreurs.append(f"ligne {numero_ligne} : {e}")
            en_erreur.add((ligne.get("zk_user_id") or "").strip())

    valides = {zk: cours for zk, cours in par_membre.items() if zk not in en_erreur}
    with conn:  # une seule transaction : tout ou rien
        for zk_id, cours in valides.items():
            conn.execute("DELETE FROM creneaux WHERE zk_user_id = ?", (zk_id,))
            conn.executemany(
                "INSERT INTO creneaux (zk_user_id, jour, debut, fin, classe, matiere) "
                "VALUES (:zk_user_id, :jour, :debut, :fin, :classe, :matiere)", cours)
    for zk_id in sorted(set(par_membre) & en_erreur):
        erreurs.append(f"emploi du temps de {zk_id} non modifié à cause des erreurs ci-dessus")
    return sum(len(c) for c in valides.values()), len(valides), erreurs


# --- Lecture ---------------------------------------------------------------------

def trouver(conn: sqlite3.Connection, zk_user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM personnel WHERE zk_user_id = ? AND actif = 1",
                        (zk_user_id,)).fetchone()


def lister(conn: sqlite3.Connection, au: date | None = None) -> list[sqlite3.Row]:
    """Personnel actif, inscrit au plus tard le jour `au` (tous si `au` est None)."""
    limite = (au or date.max).isoformat()
    return conn.execute("SELECT * FROM personnel WHERE actif = 1 AND date_inscription <= ? "
                        "ORDER BY nom, prenom", (limite,)).fetchall()


def _emplois_du_temps(conn: sqlite3.Connection) -> dict[tuple[str, str], list[dict]]:
    """(zk_user_id, jour de la semaine) -> cours de ce jour, dans l'ordre."""
    resultat: dict[tuple[str, str], list[dict]] = {}
    for r in conn.execute("SELECT * FROM creneaux ORDER BY debut"):
        resultat.setdefault((r["zk_user_id"], r["jour"]), []).append(dict(r))
    return resultat


def creneaux_du_jour(conn: sqlite3.Connection, zk_user_id: str, jour: date) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM creneaux WHERE zk_user_id = ? AND jour = ? ORDER BY debut",
        (zk_user_id, JOURS[jour.weekday()]))]


def _a(jour: date, heure: str) -> datetime:
    return datetime.combine(jour, time.fromisoformat(heure))


# --- Classement d'un pointage ----------------------------------------------------

def classer(conn: sqlite3.Connection, membre: sqlite3.Row, moment: datetime,
            tolerance_minutes: int) -> tuple[str, bool]:
    """Pointages pairs de la journée = arrivées, impairs = sorties.
    La première arrivée est en retard si elle dépasse le début du premier cours."""
    debut_journee = moment.replace(hour=0, minute=0, second=0)
    avant = conn.execute(
        "SELECT COUNT(*) FROM pointages WHERE zk_user_id = ? AND horodatage >= ? AND horodatage < ?",
        (membre["zk_user_id"], dt_texte(debut_journee), dt_texte(moment))).fetchone()[0]
    if avant % 2:
        return "sortie", False
    cours = creneaux_du_jour(conn, membre["zk_user_id"], moment.date())
    en_retard = (avant == 0 and bool(cours)
                 and moment > _a(moment.date(), cours[0]["debut"]) + timedelta(minutes=tolerance_minutes))
    return "arrivee", en_retard


# --- Calculs pour les rapports ---------------------------------------------------

def _moments(conn: sqlite3.Connection, debut: date, fin: date) -> dict[tuple[str, str], list[datetime]]:
    """Pour chaque (membre, jour) : ses pointages dans l'ordre, entre debut (inclus) et fin (exclu)."""
    lignes = conn.execute(
        "SELECT zk_user_id, horodatage FROM pointages "
        "WHERE horodatage >= ? AND horodatage < ? "
        "AND zk_user_id IN (SELECT zk_user_id FROM personnel) ORDER BY horodatage",
        (debut.isoformat(), fin.isoformat()))
    resultat: dict[tuple[str, str], list[datetime]] = {}
    for r in lignes:
        resultat.setdefault((r["zk_user_id"], r["horodatage"][:10]), []).append(texte_dt(r["horodatage"]))
    return resultat


def bilan_journee(moments: list[datetime]) -> dict:
    """Première arrivée, dernière sortie, minutes de présence et oubli éventuel."""
    paires = [(moments[i], moments[i + 1]) for i in range(0, len(moments) - 1, 2)]
    return {
        "arrivee": moments[0].strftime("%H:%M"),
        "sortie": paires[-1][1].strftime("%H:%M") if paires else None,
        "minutes": sum(int((s - a).total_seconds() // 60) for a, s in paires),
        "incomplet": len(moments) % 2 == 1,
    }


def _intervalles(moments: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Périodes de présence. Une arrivée sans sortie vaut jusqu'à la fin de la journée."""
    fin_journee = datetime.combine(moments[0].date(), time.max)
    return [(moments[i], moments[i + 1] if i + 1 < len(moments) else fin_journee)
            for i in range(0, len(moments), 2)]


def analyser_cours(cours: list[dict], moments: list[datetime], jour: date,
                   tolerance_minutes: int) -> list[dict]:
    """Pour chaque cours : etat "ok", "retard" (avec minutes) ou "manque"."""
    presences = _intervalles(moments) if moments else []
    resultat = []
    for c in cours:
        debut, fin = _a(jour, c["debut"]), _a(jour, c["fin"])
        arrivees = [max(a, debut) for a, s in presences if a < fin and s > debut]
        if not arrivees:
            resultat.append({**c, "etat": "manque", "retard": 0})
            continue
        retard = int((min(arrivees) - debut).total_seconds() // 60)
        etat = "retard" if retard > tolerance_minutes else "ok"
        resultat.append({**c, "etat": etat, "retard": retard if etat == "retard" else 0})
    return resultat


def _identite(membre: sqlite3.Row) -> dict:
    return {"nom": f"{membre['prenom']} {membre['nom']}", "fonction": membre["fonction"]}


def plages(cours: list[dict]) -> str:
    """Cours consécutifs fusionnés : "08h-10h, 15h-17h"."""
    blocs: list[list[str]] = []
    for c in cours:
        if blocs and c["debut"] <= blocs[-1][1]:
            blocs[-1][1] = max(blocs[-1][1], c["fin"])
        else:
            blocs.append([c["debut"], c["fin"]])
    return ", ".join(f"{_court(d)}-{_court(f)}" for d, f in blocs)


def _court(heure: str) -> str:
    """"08:00" -> "08h", "10:30" -> "10h30"."""
    return heure.replace(":00", "h").replace(":", "h")


def calculer_jour(conn: sqlite3.Connection, jour: date, tolerance_minutes: int) -> list[dict]:
    """Une ligne par membre : statut (present, retard, absent, repos), cours analysés, présence."""
    moments = _moments(conn, jour, jour + timedelta(days=1))
    edt = _emplois_du_temps(conn)
    lignes = []
    for m in lister(conn, jour):
        cours = edt.get((m["zk_user_id"], JOURS[jour.weekday()]), [])
        siens = moments.get((m["zk_user_id"], jour.isoformat()), [])
        analyse = analyser_cours(cours, siens, jour, tolerance_minutes)
        ligne = {**_identite(m), "prevu": plages(cours), "cours": analyse,
                 "retards": sum(1 for c in analyse if c["etat"] == "retard"),
                 "manques": sum(1 for c in analyse if c["etat"] == "manque"),
                 "arrivee": None, "sortie": None, "minutes": 0, "incomplet": False}
        if siens:
            ligne.update(bilan_journee(siens))
            ligne["statut"] = "retard" if ligne["retards"] else "present"
        else:
            ligne["statut"] = "absent" if cours else "repos"
        lignes.append(ligne)
    return lignes


def calculer_mois(conn: sqlite3.Connection, annee: int, mois: int, tolerance_minutes: int,
                  aujourd_hui: date | None = None) -> list[dict]:
    """Une ligne par membre : jours prévus, absences, retards, cours manqués, minutes.

    Un jour n'est « prévu » que si la personne a cours ce jour de la semaine ET
    que l'établissement a fonctionné (au moins un pointage) : les fériés et les
    vacances ne comptent pas. Les cours d'un jour d'absence comptent comme manqués.
    """
    aujourd_hui = aujourd_hui or date.today()
    premier = date(annee, mois, 1)
    dernier = date(annee, mois, calendar.monthrange(annee, mois)[1])
    lendemain = dernier + timedelta(days=1)
    jours_ouverts = sorted(date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT substr(horodatage, 1, 10) FROM pointages WHERE horodatage >= ? AND horodatage < ?",
        (premier.isoformat(), lendemain.isoformat())))
    moments = _moments(conn, premier, lendemain)
    edt = _emplois_du_temps(conn)

    lignes = []
    for m in lister(conn, dernier):
        zk_id = m["zk_user_id"]
        prevus = [j for j in jours_ouverts
                  if m["date_inscription"] <= j.isoformat() and j <= aujourd_hui
                  and edt.get((zk_id, JOURS[j.weekday()]))]
        analyses = [analyser_cours(edt[(zk_id, JOURS[j.weekday()])], moments.get((zk_id, j.isoformat()), []),
                                   j, tolerance_minutes) for j in prevus]
        bilans = [bilan_journee(v) for (z, _), v in moments.items() if z == zk_id]
        lignes.append({
            **_identite(m),
            "prevus": len(prevus),
            "absences": sum(1 for j in prevus if (zk_id, j.isoformat()) not in moments),
            "retards": sum(1 for a in analyses for c in a if c["etat"] == "retard"),
            "manques": sum(1 for a in analyses for c in a if c["etat"] == "manque"),
            "oublis": sum(1 for b in bilans if b["incomplet"]),
            "minutes": sum(b["minutes"] for b in bilans),
        })
    return lignes


def duree(minutes: int) -> str:
    """135 -> "2h15"."""
    return f"{minutes // 60}h{minutes % 60:02d}"
