"""Rapports PDF pour l'administration : un rapport par jour et un par mois.

Rapport du jour : qui a pointé, qui n'a pas pointé (absents), les retards, et
les oublis (arrivée pointée sans sortie, ou l'inverse).
Rapport du mois : assiduité par classe, élèves à surveiller et alertes WhatsApp.
Relevé du prestataire (mensuel, pour Chérif) : paiements, montant dû, parents à relancer.

Deux étapes séparées : calculer (des chiffres, faciles à tester) puis mettre
en page (le PDF). On pourra réutiliser les mêmes chiffres ailleurs plus tard,
par exemple dans un tableau de bord web.
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import personnel
from .config import Config, lire_heure
from .db import dt_texte
from .subscriptions import statut_abonnement

log = logging.getLogger(__name__)

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

LIBELLES_NOTIFS = {
    "envoye": "Envoyées",
    "en_attente": "En attente",
    "echec": "Échecs",
    "ignore_abonnement": "Non envoyées (abonnement inactif)",
    "ignore_trop_ancien": "Non envoyées (récupérées trop tard)",
}


# --- Calculs communs -------------------------------------------------------------

def _texte_jour(jour: date) -> str:
    return dt_texte(datetime.combine(jour, time()))


# Les pointages du personnel ne doivent pas fausser les chiffres des élèves
# (un prof qui pointe un samedi ne fait pas de ce samedi un jour d'école).
_SEULEMENT_ELEVES = "AND zk_user_id IN (SELECT zk_user_id FROM eleves) "


def _resume_pointages(conn: sqlite3.Connection, debut: str, fin: str) -> dict:
    """Pour chaque (élève, jour) : heure d'arrivée, heure de sortie, retard.

    Un élève absent n'a tout simplement pas d'entrée dans ce dictionnaire.
    """
    lignes = conn.execute(
        "SELECT zk_user_id, substr(horodatage, 1, 10) AS jour, "
        "  MIN(CASE WHEN type = 'arrivee' THEN substr(horodatage, 12, 5) END) AS arrivee, "
        "  MAX(CASE WHEN type = 'sortie' THEN substr(horodatage, 12, 5) END) AS sortie, "
        "  MAX(en_retard) AS retard "
        "FROM pointages WHERE horodatage >= ? AND horodatage < ? " + _SEULEMENT_ELEVES +
        "GROUP BY zk_user_id, jour", (debut, fin))
    return {(r["zk_user_id"], r["jour"]): r for r in lignes}


def _eleves_inscrits(conn: sqlite3.Connection, au: date) -> list[sqlite3.Row]:
    """Élèves scolarisés et déjà inscrits à cette date (un nouvel élève n'est
    pas compté absent pour les jours d'avant son inscription)."""
    return conn.execute(
        "SELECT * FROM eleves WHERE actif_scolarite = 1 AND date_inscription <= ? "
        "ORDER BY classe, nom, prenom", (au.isoformat(),)).fetchall()


def _notifications(conn: sqlite3.Connection, debut: str, fin: str) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT n.statut, COUNT(*) FROM notifications n JOIN pointages p ON p.id = n.pointage_id "
        "WHERE p.horodatage >= ? AND p.horodatage < ? GROUP BY n.statut", (debut, fin))}


def _identite(eleve: sqlite3.Row) -> dict:
    return {"nom": f"{eleve['prenom']} {eleve['nom']}", "classe": eleve["classe"]}


# --- Rapport du jour -------------------------------------------------------------

@dataclass
class StatsJour:
    jour: date
    nb_eleves: int
    presents: int
    absents: list[dict]         # nom, classe, tel
    retards: list[dict]         # nom, classe, heure
    sans_sortie: list[dict]     # nom, classe, heure (arrivée)
    sans_arrivee: list[dict]    # nom, classe, heure (sortie)
    par_classe: list[dict]      # classe, effectif, presents, absents, retards, incomplets,
                                # eleves (une ligne par élève : nom, statut, arrivee, sortie)
    notifications: dict[str, int]


def calculer_jour(conn: sqlite3.Connection, jour: date) -> StatsJour:
    debut, fin = _texte_jour(jour), _texte_jour(jour + timedelta(days=1))
    resume = _resume_pointages(conn, debut, fin)
    eleves = _eleves_inscrits(conn, jour)

    absents, retards, sans_sortie, sans_arrivee = [], [], [], []
    classes: dict[str, dict] = {}
    for e in eleves:
        c = classes.setdefault(e["classe"], {"classe": e["classe"], "effectif": 0, "presents": 0,
                                             "absents": 0, "retards": 0, "incomplets": 0,
                                             "eleves": []})
        c["effectif"] += 1
        r = resume.get((e["zk_user_id"], jour.isoformat()))
        if r is None:
            absents.append({**_identite(e), "tel": e["tel_parent"]})
            c["absents"] += 1
            c["eleves"].append({"nom": _identite(e)["nom"], "statut": "absent",
                                "arrivee": None, "sortie": None})
            continue
        c["presents"] += 1
        if r["retard"]:
            retards.append({**_identite(e), "heure": r["arrivee"]})
            c["retards"] += 1
        if r["arrivee"] and not r["sortie"]:
            sans_sortie.append({**_identite(e), "heure": r["arrivee"]})
        if r["sortie"] and not r["arrivee"]:
            sans_arrivee.append({**_identite(e), "heure": r["sortie"]})
        if not (r["arrivee"] and r["sortie"]):
            c["incomplets"] += 1
        c["eleves"].append({"nom": _identite(e)["nom"],
                            "statut": "retard" if r["retard"] else "present",
                            "arrivee": r["arrivee"], "sortie": r["sortie"]})

    return StatsJour(
        jour=jour, nb_eleves=len(eleves), presents=len(eleves) - len(absents),
        absents=absents, retards=sorted(retards, key=lambda r: r["heure"], reverse=True),
        sans_sortie=sans_sortie, sans_arrivee=sans_arrivee,
        par_classe=sorted(classes.values(), key=lambda c: c["classe"]),
        notifications=_notifications(conn, debut, fin),
    )


# --- Rapport du mois -------------------------------------------------------------

@dataclass
class StatsMois:
    annee: int
    mois: int
    nb_eleves: int
    jours_ecole: int
    total_pointages: int
    taux_assiduite: float           # entre 0 et 1
    total_absences: int             # en journées-élèves
    total_retards: int
    total_oublis: int               # journées avec arrivée ou sortie non pointée
    par_classe: list[dict]          # classe, effectif, taux, absences, retards
    a_surveiller: list[dict]        # nom, classe, absences, retards, oublis (10 premiers)
    abonnements: dict[str, int]     # actif / essai / expire
    revenus: int                    # FCFA encaissés dans le mois
    nb_paiements: int
    part_pourcent: int              # part du prestataire
    montant_du: int                 # FCFA à reverser au prestataire
    notifications: dict[str, int]


def _bornes_mois(annee: int, mois: int) -> tuple[str, str]:
    """Début du mois (inclus) et début du mois suivant (exclu), en texte ISO."""
    suivant = date(annee + 1, 1, 1) if mois == 12 else date(annee, mois + 1, 1)
    return _texte_jour(date(annee, mois, 1)), _texte_jour(suivant)


def calculer_stats(conn: sqlite3.Connection, annee: int, mois: int, part_pourcent: int = 100,
                   aujourd_hui: date | None = None) -> StatsMois:
    aujourd_hui = aujourd_hui or date.today()
    debut, fin = _bornes_mois(annee, mois)
    dernier_jour = date(annee, mois, calendar.monthrange(annee, mois)[1])
    resume = _resume_pointages(conn, debut, fin)
    eleves = _eleves_inscrits(conn, dernier_jour)

    # Jour d'école = jour où au moins un élève a pointé (week-ends et fériés exclus d'office).
    jours = sorted({jour for _, jour in resume})
    total_pointages = conn.execute(
        "SELECT COUNT(*) FROM pointages WHERE horodatage >= ? AND horodatage < ? " + _SEULEMENT_ELEVES,
        (debut, fin)).fetchone()[0]

    classes: dict[str, dict] = {}
    suivis = []
    total_attendus = total_presences = 0
    for e in eleves:
        # On n'attend l'élève qu'à partir de son inscription.
        attendus = [j for j in jours if j >= e["date_inscription"]]
        presences = [resume[(e["zk_user_id"], j)] for j in attendus if (e["zk_user_id"], j) in resume]
        absences = len(attendus) - len(presences)
        retards = sum(1 for r in presences if r["retard"])
        oublis = sum(1 for r in presences if not (r["arrivee"] and r["sortie"]))

        c = classes.setdefault(e["classe"], {"classe": e["classe"], "effectif": 0, "attendus": 0,
                                             "presences": 0, "absences": 0, "retards": 0})
        c["effectif"] += 1
        c["attendus"] += len(attendus)
        c["presences"] += len(presences)
        c["absences"] += absences
        c["retards"] += retards
        total_attendus += len(attendus)
        total_presences += len(presences)
        if absences or retards or oublis:
            suivis.append({**_identite(e), "absences": absences, "retards": retards, "oublis": oublis})

    for c in classes.values():
        c["taux"] = c["presences"] / c["attendus"] if c["attendus"] else 0.0

    # État des abonnements au dernier jour du mois (ou aujourd'hui si le mois est en cours).
    reference = min(aujourd_hui, dernier_jour)
    abonnements = Counter(statut_abonnement(e, reference) for e in eleves)
    revenus, nb_paiements = conn.execute(
        "SELECT COALESCE(SUM(montant), 0), COUNT(*) FROM paiements "
        "WHERE date_paiement >= ? AND date_paiement < ?", (debut[:10], fin[:10])
    ).fetchone()

    return StatsMois(
        annee=annee, mois=mois, nb_eleves=len(eleves), jours_ecole=len(jours),
        total_pointages=total_pointages,
        taux_assiduite=total_presences / total_attendus if total_attendus else 0.0,
        total_absences=sum(c["absences"] for c in classes.values()),
        total_retards=sum(c["retards"] for c in classes.values()),
        total_oublis=sum(s["oublis"] for s in suivis),
        par_classe=sorted(classes.values(), key=lambda c: c["classe"]),
        a_surveiller=sorted(suivis, key=lambda s: (-s["absences"], -s["retards"], -s["oublis"]))[:10],
        abonnements={k: abonnements.get(k, 0) for k in ("actif", "essai", "expire")},
        revenus=revenus, nb_paiements=nb_paiements, part_pourcent=part_pourcent,
        montant_du=revenus * part_pourcent // 100,
        notifications=_notifications(conn, debut, fin),
    )


# --- Relevé du prestataire --------------------------------------------------------
# Document pour Chérif (le prestataire), séparé du rapport de l'école : paiements
# du mois, montant que l'école doit reverser, parents à relancer.

@dataclass
class ReleveMois:
    annee: int
    mois: int
    abonnements: dict[str, int]     # actif / essai / expire
    paiements: list[dict]           # date, nom, classe, mois, montant
    revenus: int
    part_pourcent: int
    montant_du: int
    messages_envoyes: int           # pour estimer la facture Twilio
    a_relancer: list[dict]          # abonnement expiré : nom, classe, tel, fin


def calculer_releve(conn: sqlite3.Connection, annee: int, mois: int, part_pourcent: int = 100,
                    aujourd_hui: date | None = None) -> ReleveMois:
    aujourd_hui = aujourd_hui or date.today()
    debut, fin = _bornes_mois(annee, mois)
    dernier_jour = date(annee, mois, calendar.monthrange(annee, mois)[1])
    reference = min(aujourd_hui, dernier_jour)
    eleves = _eleves_inscrits(conn, dernier_jour)

    paiements = [
        {"date": r["date_paiement"], "nom": f"{r['prenom']} {r['nom']}", "classe": r["classe"],
         "mois": r["mois_couverts"], "montant": r["montant"]}
        for r in conn.execute(
            "SELECT p.date_paiement, p.mois_couverts, p.montant, e.prenom, e.nom, e.classe "
            "FROM paiements p JOIN eleves e ON e.id = p.eleve_id "
            "WHERE p.date_paiement >= ? AND p.date_paiement < ? ORDER BY p.date_paiement, p.id",
            (debut[:10], fin[:10]))
    ]
    revenus = sum(p["montant"] for p in paiements)
    statuts = {e["zk_user_id"]: statut_abonnement(e, reference) for e in eleves}
    a_relancer = [
        {**_identite(e), "tel": e["tel_parent"], "fin": e["abonnement_fin"] or e["essai_fin"]}
        for e in eleves if statuts[e["zk_user_id"]] == "expire"
    ]
    envoyes = _notifications(conn, debut, fin).get("envoye", 0)
    compte = Counter(statuts.values())
    return ReleveMois(
        annee=annee, mois=mois,
        abonnements={k: compte.get(k, 0) for k in ("actif", "essai", "expire")},
        paiements=paiements, revenus=revenus, part_pourcent=part_pourcent,
        montant_du=revenus * part_pourcent // 100, messages_envoyes=envoyes,
        a_relancer=sorted(a_relancer, key=lambda r: (r["classe"], r["nom"])),
    )


# --- Mise en page ----------------------------------------------------------------
# Style : trait de couleur avant chaque titre, en-têtes de tableau bleu nuit,
# lignes alternées, statuts en couleur (vert présent, rouge absent, orange retard).

ACCENT = "#1f9e89"      # vert d'eau : traits de titre
NUIT = "#14213d"        # bleu nuit : titres et en-têtes de tableau
ZEBRE = "#f3f5f8"       # gris très clair : une ligne sur deux
GRIS = "#6b7280"        # textes secondaires
VERT = "#1a8f6a"
ROUGE = "#c0392b"
ORANGE = "#d9891a"


def _t(texte) -> str:
    """Les polices intégrées du PDF ne connaissent que l'alphabet latin-1 :
    on remplace par "?" les rares caractères hors de cet alphabet."""
    return str(texte).encode("latin-1", "replace").decode("latin-1")


def _pct(valeur: float) -> str:
    return f"{valeur * 100:.1f} %".replace(".", ",")


def _fcfa(montant: int) -> str:
    return f"{montant:,} FCFA".replace(",", " ")


def _heure(texte: str | None) -> str:
    """"07:42" -> "07h42"."""
    return texte.replace(":", "h") if texte else ""


def _date_longue(jour: date) -> str:
    return f"{JOURS_FR[jour.weekday()].capitalize()} {jour.day} {MOIS_FR[jour.month - 1]} {jour.year}"


def _style(couleur: str, gras: bool = True, italique: bool = False):
    from fpdf.fonts import FontFace

    return FontFace(emphasis=("B" if gras else "") + ("I" if italique else ""), color=couleur)


def _trait(pdf) -> None:
    """Le petit trait de couleur qui précède chaque titre."""
    pdf.set_fill_color(ACCENT)
    pdf.rect(pdf.l_margin, pdf.get_y(), 16, 1.3, style="F")
    pdf.set_fill_color("#ffffff")  # sinon cette couleur déteint sur les lignes des tableaux
    pdf.ln(4)


def _nouveau_pdf(type_rapport: str, nom_ecole: str, periode: str):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    _trait(pdf)
    pdf.set_text_color(GRIS)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, _t(type_rapport.upper()), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(NUIT)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, _t(nom_ecole), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(GRIS)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, _t(periode), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)
    return pdf


def _titre(pdf, texte: str) -> None:
    if pdf.will_page_break(45):  # pas de titre seul en bas de page, séparé de son tableau
        pdf.add_page()
    _trait(pdf)
    pdf.set_text_color(NUIT)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, _t(texte.upper()), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)


def _texte(pdf, texte: str, gras: str = "") -> None:
    """Une ligne de texte ; `gras` est un début de phrase écrit en gras."""
    pdf.set_text_color(NUIT)
    if gras:
        pdf.set_font("Helvetica", "B", 10)
        pdf.write(6, _t(gras + " "))
    pdf.set_font("Helvetica", "", 10)
    pdf.write(6, _t(texte))
    pdf.ln(9)


def _tableau(pdf, entetes: list[str], lignes: list[list], largeurs: tuple[int, ...]) -> None:
    """Tableau stylé. Une cellule peut être un texte, ou un couple (texte, style)
    pour la colorer (par exemple le statut d'un élève)."""
    from fpdf.fonts import FontFace

    pdf.set_text_color(NUIT)
    pdf.set_font("Helvetica", "", 10)
    with pdf.table(col_widths=largeurs, width=sum(largeurs), align="LEFT", text_align="LEFT",
                   line_height=8, padding=(0, 3), borders_layout="NONE",
                   cell_fill_color=ZEBRE, cell_fill_mode="ROWS",
                   headings_style=FontFace(emphasis="B", color="#ffffff", fill_color=NUIT)) as table:
        rangee = table.row()
        for entete in entetes:
            rangee.cell(_t(entete))
        for ligne in lignes:
            rangee = table.row()
            for valeur in ligne:
                if isinstance(valeur, tuple):
                    rangee.cell(_t(valeur[0]), style=valeur[1])
                else:
                    rangee.cell(_t(valeur))
    pdf.ln(3)


def _tableau_ou_rien(pdf, entetes: list[str], lignes: list[list], largeurs: tuple[int, ...],
                     si_vide: str) -> None:
    if lignes:
        _tableau(pdf, entetes, lignes, largeurs)
        pdf.ln(4)
    else:
        _texte(pdf, si_vide)


def _pied(pdf, genere_le: datetime) -> None:
    pdf.ln(2)
    pdf.set_text_color(GRIS)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 6, _t(f"Généré automatiquement par ARIA_point (ARIA_tech) le {genere_le:%d/%m/%Y à %Hh%M}."),
             new_x="LMARGIN", new_y="NEXT")


def _enregistrer(pdf, chemin: Path) -> Path:
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(chemin))
    return chemin


def _tableau_notifications(pdf, notifications: dict[str, int]) -> None:
    _tableau(pdf, ["Statut", "Messages"],
             [[libelle, notifications.get(statut, 0)] for statut, libelle in LIBELLES_NOTIFS.items()],
             (120, 60))


def _ligne_eleve(eleve: dict) -> list:
    """Élève, Statut, Arrivée, Sortie (statut et heures manquantes en couleur)."""
    non_pointee = ("non pointée", _style(ORANGE, gras=False, italique=True))
    if eleve["statut"] == "absent":
        return [eleve["nom"], ("Absent(e)", _style(ROUGE)), "-", "-"]
    statut = ("Retard", _style(ORANGE)) if eleve["statut"] == "retard" else ("Présent(e)", _style(VERT))
    arrivee = _heure(eleve["arrivee"]) if eleve["arrivee"] else non_pointee
    sortie = _heure(eleve["sortie"]) if eleve["sortie"] else non_pointee
    return [eleve["nom"], statut, arrivee, sortie]


def generer_pdf_jour(stats: StatsJour, nom_ecole: str, chemin: Path,
                     genere_le: datetime | None = None) -> Path:
    date_txt = _date_longue(stats.jour)
    pdf = _nouveau_pdf("Rapport quotidien", nom_ecole, date_txt)

    _titre(pdf, "Vue d'ensemble")
    _tableau(pdf, ["Indicateur", "Nombre"], [
        ["Élèves inscrits", stats.nb_eleves],
        ["Ont pointé (présents)", (str(stats.presents), _style(VERT))],
        ["N'ont pas pointé (absents)", (str(len(stats.absents)), _style(ROUGE))],
        ["Arrivés en retard", (str(len(stats.retards)), _style(ORANGE))],
        ["Arrivée pointée, sortie non pointée", len(stats.sans_sortie)],
        ["Sortie pointée, arrivée non pointée", len(stats.sans_arrivee)],
    ], (120, 60))
    pdf.ln(4)

    # Même organisation que le rapport mensuel : une ligne par classe, puis
    # seulement les élèves qui demandent une action (pas la liste de toute l'école).
    _titre(pdf, "Présence par classe")
    _tableau_ou_rien(pdf, ["Classe", "Effectif", "Présents", "Absents", "Retards"],
                     [[c["classe"], c["effectif"],
                       (str(c["presents"]), _style(VERT)),
                       (str(c["absents"]), _style(ROUGE)) if c["absents"] else "0",
                       (str(c["retards"]), _style(ORANGE)) if c["retards"] else "0"]
                      for c in stats.par_classe], (50, 32, 33, 33, 32), "Aucun élève inscrit.")

    _titre(pdf, "Élèves à suivre aujourd'hui")
    a_suivre = [(c["classe"], e) for c in stats.par_classe for e in c["eleves"]
                if e["statut"] != "present" or not (e["arrivee"] and e["sortie"])]
    _tableau_ou_rien(pdf, ["Élève", "Classe", "Statut", "Arrivée", "Sortie"],
                     [[ligne[0], classe, *ligne[1:]]
                      for classe, ligne in ((cl, _ligne_eleve(e)) for cl, e in a_suivre)],
                     (65, 30, 30, 27, 28), "Aucune absence, aucun retard, aucun oubli aujourd'hui.")

    _titre(pdf, "Absents du jour : parents à contacter")
    _tableau_ou_rien(pdf, ["Élève", "Classe", "Téléphone du parent"],
                     [[a["nom"], a["classe"], a["tel"]] for a in stats.absents],
                     (75, 40, 65), "Aucun absent aujourd'hui.")

    _titre(pdf, "Alertes WhatsApp du jour")
    _tableau_notifications(pdf, stats.notifications)

    _pied(pdf, genere_le or datetime.now())
    return _enregistrer(pdf, chemin)


def generer_pdf(stats: StatsMois, nom_ecole: str, chemin: Path,
                genere_le: datetime | None = None) -> Path:
    pdf = _nouveau_pdf("Rapport mensuel", nom_ecole,
                       f"{MOIS_FR[stats.mois - 1].capitalize()} {stats.annee}")

    _titre(pdf, "Vue d'ensemble")
    _tableau(pdf, ["Indicateur", "Valeur"], [
        ["Élèves inscrits", stats.nb_eleves],
        ["Jours d'école comptés", stats.jours_ecole],
        ["Nombre total de pointages", stats.total_pointages],
        ["Taux d'assiduité", (_pct(stats.taux_assiduite), _style(VERT))],
        ["Absences (journées-élèves)", (str(stats.total_absences), _style(ROUGE))],
        ["Retards", (str(stats.total_retards), _style(ORANGE))],
        ["Journées avec pointage incomplet", stats.total_oublis],
    ], (120, 60))
    pdf.ln(4)

    _titre(pdf, "Assiduité par classe")
    _tableau_ou_rien(pdf, ["Classe", "Effectif", "Assiduité", "Absences", "Retards"],
                     [[c["classe"], c["effectif"], _pct(c["taux"]), c["absences"], c["retards"]]
                      for c in stats.par_classe], (50, 32, 33, 33, 32), "Aucun élève inscrit.")

    _titre(pdf, "Élèves à surveiller")
    _tableau_ou_rien(pdf, ["Élève", "Classe", "Absences", "Retards", "Oublis"],
                     [[s["nom"], s["classe"],
                       (str(s["absences"]), _style(ROUGE)) if s["absences"] else "0",
                       (str(s["retards"]), _style(ORANGE)) if s["retards"] else "0",
                       s["oublis"]] for s in stats.a_surveiller],
                     (65, 35, 27, 27, 26), "Aucune absence, aucun retard ce mois-ci.")

    _titre(pdf, "Alertes WhatsApp")
    _tableau_notifications(pdf, stats.notifications)

    _pied(pdf, genere_le or datetime.now())
    return _enregistrer(pdf, chemin)


def generer_pdf_releve(releve: ReleveMois, nom_ecole: str, chemin: Path,
                       genere_le: datetime | None = None) -> Path:
    pdf = _nouveau_pdf("Relevé du prestataire", nom_ecole,
                       f"{MOIS_FR[releve.mois - 1].capitalize()} {releve.annee}")

    _titre(pdf, "Montant dû par l'école")
    _tableau(pdf, ["Élément", "Valeur"], [
        ["Paiements encaissés ce mois", len(releve.paiements)],
        ["Total encaissé", _fcfa(releve.revenus)],
        [(f"Montant dû au prestataire ({releve.part_pourcent} %)", _style(NUIT)),
         (_fcfa(releve.montant_du), _style(VERT))],
        ["Messages WhatsApp envoyés (coût Twilio)", releve.messages_envoyes],
    ], (120, 60))
    pdf.ln(4)

    _titre(pdf, "Abonnements des parents")
    a = releve.abonnements
    _tableau(pdf, ["Situation", "Élèves"], [
        ["Abonnement payé", (str(a["actif"]), _style(VERT))],
        ["Essai gratuit en cours", a["essai"]],
        ["Abonnement expiré", (str(a["expire"]), _style(ROUGE)) if a["expire"] else "0"],
    ], (120, 60))
    pdf.ln(4)

    _titre(pdf, "Détail des paiements du mois")
    _tableau_ou_rien(pdf, ["Date", "Élève", "Classe", "Mois payés", "Montant"],
                     [[date.fromisoformat(p["date"]).strftime("%d/%m/%Y"), p["nom"], p["classe"],
                       p["mois"], _fcfa(p["montant"])] for p in releve.paiements],
                     (28, 57, 28, 30, 37), "Aucun paiement enregistré ce mois-ci.")

    _titre(pdf, "Parents à relancer (abonnement expiré)")
    _tableau_ou_rien(pdf, ["Élève", "Classe", "Téléphone du parent", "Expiré depuis"],
                     [[r["nom"], r["classe"], r["tel"],
                       (date.fromisoformat(r["fin"]) + timedelta(days=1)).strftime("%d/%m/%Y")]
                      for r in releve.a_relancer],
                     (60, 30, 50, 40), "Aucun abonnement expiré.")

    _pied(pdf, genere_le or datetime.now())
    return _enregistrer(pdf, chemin)


# --- Rapport du personnel (confidentiel, pour le directeur) -------------------------

CONFIDENTIEL = "Rapport du personnel · confidentiel, destiné au directeur"
STATUTS_PERSONNEL = {"present": ("Présent(e)", VERT), "retard": ("Retard", ORANGE),
                     "absent": ("Absent(e)", ROUGE), "repos": ("Pas de cours", GRIS)}


def _ligne_personnel(m: dict) -> list:
    libelle, couleur = STATUTS_PERSONNEL[m["statut"]]
    prevu = m["prevu"] or "-"
    if m["statut"] in ("absent", "repos"):
        return [m["nom"], m["fonction"], prevu,
                (libelle, _style(couleur, gras=m["statut"] == "absent")), "-", "-", "-"]
    sortie = _heure(m["sortie"]) if m["sortie"] else ("oubliée", _style(ORANGE, gras=False, italique=True))
    return [m["nom"], m["fonction"], prevu, (libelle, _style(couleur)),
            _heure(m["arrivee"]), sortie, personnel.duree(m["minutes"])]


def _constat(cours: dict):
    if cours["etat"] == "retard":
        return (f"Retard de {cours['retard']} min", _style(ORANGE))
    return ("Cours manqué", _style(ROUGE))


def generer_pdf_personnel_jour(lignes: list[dict], jour: date, nom_ecole: str, chemin: Path,
                               tolerance: int, genere_le: datetime | None = None) -> Path:
    date_txt = _date_longue(jour)
    pdf = _nouveau_pdf(CONFIDENTIEL, nom_ecole, date_txt)
    compte = Counter(m["statut"] for m in lignes)

    _titre(pdf, "Vue d'ensemble")
    _tableau(pdf, ["Indicateur", "Nombre"], [
        ["Personnel attendu ce jour", len(lignes) - compte["repos"]],
        ["Présents", (str(compte["present"] + compte["retard"]), _style(VERT))],
        ["Absents", (str(compte["absent"]), _style(ROUGE))],
        ["Cours commencés en retard", (str(sum(m["retards"] for m in lignes)), _style(ORANGE))],
        ["Cours manqués", (str(sum(m["manques"] for m in lignes)), _style(ROUGE))],
        ["Sorties non pointées", sum(1 for m in lignes if m["incomplet"])],
    ], (120, 60))
    pdf.ln(4)

    _titre(pdf, f"Présence · {date_txt}")
    _tableau_ou_rien(pdf, ["Nom", "Fonction", "Cours", "Statut", "Arrivée", "Sortie", "Heures"],
                     [_ligne_personnel(m) for m in lignes], (35, 30, 28, 28, 20, 20, 19),
                     "Aucun membre du personnel inscrit.")

    _titre(pdf, "Retards et cours manqués")
    _tableau_ou_rien(pdf, ["Nom", "Cours", "Classe", "Matière", "Constat"],
                     [[m["nom"], f"{_heure(c['debut'])}-{_heure(c['fin'])}", c["classe"] or "-",
                       c["matiere"] or "-", _constat(c)]
                      for m in lignes for c in m["cours"] if c["etat"] != "ok"],
                     (40, 28, 25, 42, 45), "Tous les cours prévus ont commencé à l'heure.")
    _texte(pdf, f"arrivée plus de {tolerance} min après le début du cours. Cours manqué : personne "
                f"absente pendant toute la durée du cours. Heures : temps de présence (une sortie "
                f"non pointée n'est pas comptée).", gras="Retard :")

    _pied(pdf, genere_le or datetime.now())
    return _enregistrer(pdf, chemin)


def generer_pdf_personnel_mois(lignes: list[dict], annee: int, mois: int, nom_ecole: str, chemin: Path,
                               tolerance: int, genere_le: datetime | None = None) -> Path:
    pdf = _nouveau_pdf(CONFIDENTIEL, nom_ecole, f"{MOIS_FR[mois - 1].capitalize()} {annee}")

    _titre(pdf, "Présence et heures du mois")
    _tableau_ou_rien(pdf, ["Nom", "Fonction", "Jours", "Absences", "Retards", "Manqués", "Heures"],
                     [[m["nom"], m["fonction"], m["prevus"],
                       (str(m["absences"]), _style(ROUGE)) if m["absences"] else "0",
                       (str(m["retards"]), _style(ORANGE)) if m["retards"] else "0",
                       (str(m["manques"]), _style(ROUGE)) if m["manques"] else "0",
                       personnel.duree(m["minutes"])] for m in lignes],
                     (34, 32, 18, 24, 22, 25, 25), "Aucun membre du personnel inscrit.")

    _texte(pdf, "jours où l'établissement a fonctionné et où la personne avait cours.", gras="Jours :")
    _texte(pdf, "jours prévus sans aucun pointage.", gras="Absences :")
    _texte(pdf, f"cours commencés plus de {tolerance} min en retard.", gras="Retards :")
    _texte(pdf, "cours où la personne n'était pas là, jours d'absence compris.", gras="Manqués :")
    _texte(pdf, "temps de présence (arrivée -> sortie). Une sortie non pointée n'est pas comptée.",
           gras="Heures :")

    _pied(pdf, genere_le or datetime.now())
    return _enregistrer(pdf, chemin)


# --- Génération (manuelle ou automatique) ------------------------------------------

def _tolerance(config: Config) -> int:
    return int(config.personnel.get("tolerance_retard_minutes", 5))


def chemin_rapport_jour(config: Config, jour: date) -> Path:
    return config.chemin("rapports") / "journaliers" / f"rapport_{jour.isoformat()}.pdf"


def chemin_rapport(config: Config, annee: int, mois: int) -> Path:
    return config.chemin("rapports") / "mensuels" / f"rapport_{annee}-{mois:02d}.pdf"


def generer_rapport_jour(conn: sqlite3.Connection, config: Config, jour: date) -> Path:
    chemin = generer_pdf_jour(calculer_jour(conn, jour), config.nom_ecole,
                              chemin_rapport_jour(config, jour))
    log.info("Rapport du jour généré : %s", chemin)
    return chemin


def generer_rapport(conn: sqlite3.Connection, config: Config, annee: int, mois: int) -> Path:
    part = int(config.abonnement.get("part_prestataire_pourcent", 100))
    chemin = generer_pdf(calculer_stats(conn, annee, mois, part), config.nom_ecole,
                         chemin_rapport(config, annee, mois))
    log.info("Rapport mensuel généré : %s", chemin)
    return chemin


def chemin_personnel_jour(config: Config, jour: date) -> Path:
    return config.chemin("rapports") / "personnel" / "journaliers" / f"personnel_{jour.isoformat()}.pdf"


def chemin_personnel_mois(config: Config, annee: int, mois: int) -> Path:
    return config.chemin("rapports") / "personnel" / "mensuels" / f"personnel_{annee}-{mois:02d}.pdf"


def generer_rapport_personnel_jour(conn: sqlite3.Connection, config: Config, jour: date) -> Path:
    tolerance = _tolerance(config)
    chemin = generer_pdf_personnel_jour(personnel.calculer_jour(conn, jour, tolerance), jour,
                                        config.nom_ecole, chemin_personnel_jour(config, jour), tolerance)
    log.info("Rapport du personnel du jour généré : %s", chemin)
    return chemin


def generer_rapport_personnel_mois(conn: sqlite3.Connection, config: Config, annee: int, mois: int) -> Path:
    tolerance = _tolerance(config)
    chemin = generer_pdf_personnel_mois(personnel.calculer_mois(conn, annee, mois, tolerance), annee, mois,
                                        config.nom_ecole, chemin_personnel_mois(config, annee, mois), tolerance)
    log.info("Rapport du personnel du mois généré : %s", chemin)
    return chemin


def periode_jour(jour: date) -> str:
    return _date_longue(jour)


def periode_mois(annee: int, mois: int) -> str:
    return f"{MOIS_FR[mois - 1]} {annee}"


def rapports_personnel_recents(config: Config, aujourd_hui: date) -> list[tuple[Path, str]]:
    """Rapports du personnel qui peuvent encore partir par email : 7 derniers jours
    et mois précédent. Plus ancien, on ne l'envoie plus."""
    liste = [(chemin_personnel_jour(config, j), periode_jour(j))
             for j in (aujourd_hui - timedelta(days=n) for n in range(7, -1, -1))]
    annee, mois = mois_precedent(aujourd_hui)
    liste.append((chemin_personnel_mois(config, annee, mois), periode_mois(annee, mois)))
    return liste


def mois_precedent(jour: date) -> tuple[int, int]:
    return (jour.year - 1, 12) if jour.month == 1 else (jour.year, jour.month - 1)


def _a_des_pointages(conn: sqlite3.Connection, debut: str, fin: str) -> bool:
    return conn.execute("SELECT 1 FROM pointages WHERE horodatage >= ? AND horodatage < ? LIMIT 1",
                        (debut, fin)).fetchone() is not None


def rapports_automatiques(conn: sqlite3.Connection, config: Config,
                          maintenant: datetime | None = None) -> list[Path]:
    """Génère les rapports manquants. Appelé par le service toutes les heures.

    - Rapport du jour : à partir de l'heure `journalier_apres` (18 h par défaut).
      On rattrape aussi les 7 derniers jours : si le PC a été éteint avant
      18 h un vendredi, le rapport de ce vendredi sera produit le lundi.
    - Rapport du mois écoulé : dès le 1er du mois suivant.
    Un jour ou un mois sans aucun pointage (week-end, vacances) n'a pas de rapport.
    """
    maintenant = maintenant or datetime.now()
    crees = []
    aujourd_hui = maintenant.date()
    jours = [aujourd_hui - timedelta(days=n) for n in range(7, 0, -1)]
    if maintenant.time() >= lire_heure(config.rapports.get("journalier_apres", "18:00")):
        jours.append(aujourd_hui)
    for jour in jours:
        if (not chemin_rapport_jour(config, jour).exists()
                and _a_des_pointages(conn, _texte_jour(jour), _texte_jour(jour + timedelta(days=1)))):
            crees.append(generer_rapport_jour(conn, config, jour))

    annee, mois = mois_precedent(aujourd_hui)
    if not chemin_rapport(config, annee, mois).exists() and _a_des_pointages(conn, *_bornes_mois(annee, mois)):
        crees.append(generer_rapport(conn, config, annee, mois))
    if not chemin_releve(config, annee, mois).exists() and _a_des_pointages(conn, *_bornes_mois(annee, mois)):
        crees.append(generer_releve(conn, config, annee, mois))

    # Rapports du personnel, seulement si l'école a inscrit du personnel.
    if personnel.lister(conn):
        for jour in jours:
            if (not chemin_personnel_jour(config, jour).exists()
                    and _a_des_pointages(conn, _texte_jour(jour), _texte_jour(jour + timedelta(days=1)))):
                crees.append(generer_rapport_personnel_jour(conn, config, jour))
        if (not chemin_personnel_mois(config, annee, mois).exists()
                and _a_des_pointages(conn, *_bornes_mois(annee, mois))):
            crees.append(generer_rapport_personnel_mois(conn, config, annee, mois))
    return crees


def chemin_releve(config: Config, annee: int, mois: int) -> Path:
    return config.chemin("rapports") / "prestataire" / f"releve_{annee}-{mois:02d}.pdf"


def generer_releve(conn: sqlite3.Connection, config: Config, annee: int, mois: int) -> Path:
    part = int(config.abonnement.get("part_prestataire_pourcent", 100))
    chemin = generer_pdf_releve(calculer_releve(conn, annee, mois, part), config.nom_ecole,
                                chemin_releve(config, annee, mois))
    log.info("Relevé du prestataire généré : %s", chemin)
    return chemin
