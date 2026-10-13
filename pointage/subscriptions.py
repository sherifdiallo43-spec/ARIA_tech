"""Abonnements : essai gratuit, paiements mensuels, élève actif ou non.

Choix important : on ne stocke PAS de case "is_active". On stocke des dates
de fin (essai et abonnement payé) et on calcule l'état à la demande.
Une case cochée peut rester à "actif" si quelqu'un oublie de la décocher ;
une date, elle, expire toute seule.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta

from .db import texte_d


def ajouter_mois(jour: date, mois: int) -> date:
    """Ajoute des mois en restant sur un jour qui existe (31 janvier + 1 mois = 28 février)."""
    total = jour.month - 1 + mois
    annee, numero_mois = jour.year + total // 12, total % 12 + 1
    dernier_jour = calendar.monthrange(annee, numero_mois)[1]
    return date(annee, numero_mois, min(jour.day, dernier_jour))


def fin_couverture(eleve: sqlite3.Row) -> date:
    """Dernier jour couvert, par l'essai gratuit ou par un paiement."""
    fins = [texte_d(eleve["essai_fin"])]
    if eleve["abonnement_fin"]:
        fins.append(texte_d(eleve["abonnement_fin"]))
    return max(fins)


def abonnement_actif(eleve: sqlite3.Row, jour: date) -> bool:
    """Le "is_active" du cahier des charges, calculé pour un jour donné."""
    return jour <= fin_couverture(eleve)


def statut_abonnement(eleve: sqlite3.Row, jour: date) -> str:
    """"actif" (payé), "essai" (gratuit) ou "expire"."""
    if eleve["abonnement_fin"] and jour <= texte_d(eleve["abonnement_fin"]):
        return "actif"
    if jour <= texte_d(eleve["essai_fin"]):
        return "essai"
    return "expire"


def enregistrer_paiement(conn: sqlite3.Connection, zk_user_id: str, mois: int, prix_mensuel: int,
                         aujourd_hui: date, montant: int | None = None,
                         note: str | None = None) -> date:
    """Enregistre un paiement et prolonge l'abonnement. Renvoie la nouvelle date de fin.

    La période payée commence le lendemain de la couverture actuelle (essai ou
    mois déjà payés) : payer pendant l'essai ne fait pas perdre de jours.
    Si l'abonnement avait expiré, elle commence aujourd'hui.
    """
    if mois < 1:
        raise ValueError("le nombre de mois doit être au moins 1")
    eleve = conn.execute("SELECT * FROM eleves WHERE zk_user_id = ?", (zk_user_id,)).fetchone()
    if eleve is None:
        raise ValueError(f"aucun élève avec le numéro de pointeuse {zk_user_id!r}")

    debut = max(aujourd_hui, fin_couverture(eleve) + timedelta(days=1))
    nouvelle_fin = ajouter_mois(debut, mois) - timedelta(days=1)
    montant = prix_mensuel * mois if montant is None else montant

    conn.execute("UPDATE eleves SET abonnement_fin = ? WHERE id = ?",
                 (nouvelle_fin.isoformat(), eleve["id"]))
    conn.execute(
        "INSERT INTO paiements (eleve_id, montant, mois_couverts, date_paiement, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (eleve["id"], montant, mois, aujourd_hui.isoformat(), note),
    )
    conn.commit()
    return nouvelle_fin
