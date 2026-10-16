"""Vérification : les numéros du boîtier correspondent-ils à ceux de la base ?

Le lien entre un doigt et une personne, c'est uniquement le numéro (zk_user_id).
Il est saisi à deux endroits (CSV sur l'ordi, menu sur le boîtier) : une faute de
frappe d'un côté envoie l'alerte au mauvais parent ou ne la fait jamais partir.
Cette comparaison repère les écarts avant le premier jour.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Ecarts:
    inconnus_sur_boitier: list[tuple[str, str]] = field(default_factory=list)  # (numéro, nom saisi sur le boîtier)
    eleves_sans_doigt: list[sqlite3.Row] = field(default_factory=list)
    personnel_sans_doigt: list[sqlite3.Row] = field(default_factory=list)
    numeros_en_double: list[str] = field(default_factory=list)  # même numéro chez un élève ET un membre du personnel
    ok: int = 0  # numéros présents des deux côtés

    @property
    def propre(self) -> bool:
        return not (self.inconnus_sur_boitier or self.eleves_sans_doigt
                    or self.personnel_sans_doigt or self.numeros_en_double)


def comparer(conn: sqlite3.Connection, utilisateurs_boitier: list[tuple[str, str]]) -> Ecarts:
    """Compare la liste [(numéro, nom)] lue sur le boîtier avec les tables eleves et personnel."""
    sur_boitier = {str(zk_id): nom for zk_id, nom in utilisateurs_boitier}
    eleves = conn.execute("SELECT * FROM eleves ORDER BY classe, nom, prenom").fetchall()
    personnel = conn.execute("SELECT * FROM personnel ORDER BY nom, prenom").fetchall()
    ids_eleves = {e["zk_user_id"] for e in eleves}
    ids_personnel = {p["zk_user_id"] for p in personnel}

    ecarts = Ecarts()
    ecarts.numeros_en_double = sorted(ids_eleves & ids_personnel, key=_cle_numero)
    connus = ids_eleves | ids_personnel
    ecarts.inconnus_sur_boitier = sorted(
        ((zk_id, nom) for zk_id, nom in sur_boitier.items() if zk_id not in connus),
        key=lambda t: _cle_numero(t[0]))
    ecarts.eleves_sans_doigt = [e for e in eleves
                                if e["actif_scolarite"] and e["zk_user_id"] not in sur_boitier]
    ecarts.personnel_sans_doigt = [p for p in personnel
                                   if p["actif"] and p["zk_user_id"] not in sur_boitier]
    ecarts.ok = len(connus & set(sur_boitier))
    return ecarts


def _cle_numero(zk_id: str) -> tuple[int, str]:
    """Tri numérique ("2" avant "10") avec repli alphabétique."""
    return (int(zk_id), zk_id) if zk_id.isdigit() else (10**12, zk_id)
