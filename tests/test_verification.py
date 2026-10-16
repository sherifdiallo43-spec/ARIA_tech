from datetime import date

from pointage import verification

AUJOURDHUI = date(2026, 10, 1)


def _ajouter_prof(conn, zk_id):
    conn.execute("INSERT INTO personnel (zk_user_id, nom, prenom, fonction, date_inscription) "
                 "VALUES (?, 'Diagne', 'Mamadou', 'Maths', ?)", (zk_id, AUJOURDHUI.isoformat()))
    conn.commit()


def test_tout_coherent(conn, trois_eleves):
    _ajouter_prof(conn, "900")
    ecarts = verification.comparer(conn, [("1", ""), ("2", ""), ("3", ""), ("900", "")])
    assert ecarts.propre
    assert ecarts.ok == 4


def test_doigts_manquants(conn, trois_eleves):
    _ajouter_prof(conn, "900")
    ecarts = verification.comparer(conn, [("1", "")])
    assert [e["zk_user_id"] for e in ecarts.eleves_sans_doigt] == ["2", "3"]
    assert [p["zk_user_id"] for p in ecarts.personnel_sans_doigt] == ["900"]
    assert not ecarts.propre


def test_numero_inconnu_sur_le_boitier(conn, trois_eleves):
    """Faute de frappe à l'enregistrement : le doigt de l'élève 3 a été mis sous le numéro 30."""
    ecarts = verification.comparer(conn, [("1", ""), ("2", ""), ("30", "Fatou")])
    assert ecarts.inconnus_sur_boitier == [("30", "Fatou")]
    assert [e["zk_user_id"] for e in ecarts.eleves_sans_doigt] == ["3"]


def test_numero_partage_eleve_et_personnel(conn, trois_eleves):
    _ajouter_prof(conn, "2")
    ecarts = verification.comparer(conn, [("1", ""), ("2", ""), ("3", "")])
    assert ecarts.numeros_en_double == ["2"]
    assert not ecarts.propre


def test_eleve_parti_non_reclame(conn, trois_eleves):
    conn.execute("UPDATE eleves SET actif_scolarite = 0 WHERE zk_user_id = '3'")
    conn.commit()
    ecarts = verification.comparer(conn, [("1", ""), ("2", "")])
    assert ecarts.propre
