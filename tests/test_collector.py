from datetime import datetime

from pointage.collector import classer, synchroniser
from pointage.device import PointeuseSimulee

LUNDI = datetime(2026, 10, 5)


def a(heure, minute=0, seconde=0):
    return LUNDI.replace(hour=heure, minute=minute, second=seconde)


def compter(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_deux_lectures_ne_creent_aucun_doublon(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    pointeuse.pointer("2", a(8, 10))

    premiere = synchroniser(conn, pointeuse, config, maintenant=a(8, 15))
    seconde = synchroniser(conn, pointeuse, config, maintenant=a(8, 16))

    assert premiere.nouveaux == 2
    assert seconde.nouveaux == 0 and seconde.deja_connus == 2
    assert compter(conn, "pointages") == 2
    assert compter(conn, "notifications") == 2


def test_rattrapage_apres_coupure(conn, config, trois_eleves):
    """Pendant la coupure, la pointeuse stocke ; à la reconnexion on récupère tout."""
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    synchroniser(conn, pointeuse, config, maintenant=a(7, 41))
    # ... coupure réseau : trois pointages s'accumulent dans la mémoire du boîtier ...
    pointeuse.pointer("2", a(7, 45))
    pointeuse.pointer("3", a(7, 50))
    pointeuse.pointer("1", a(12, 5))
    resultat = synchroniser(conn, pointeuse, config, maintenant=a(12, 30))
    assert resultat.nouveaux == 3 and resultat.deja_connus == 1


def test_doigt_repose_deux_fois_compte_une_seule_fois(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    pointeuse.pointer("1", a(7, 42))
    resultat = synchroniser(conn, pointeuse, config, maintenant=a(7, 45))
    assert resultat.nouveaux == 1 and resultat.rebonds == 1
    # Relire ne change rien : le rebond est de nouveau reconnu comme tel.
    resultat = synchroniser(conn, pointeuse, config, maintenant=a(7, 46))
    assert resultat.nouveaux == 0 and compter(conn, "pointages") == 1


def test_doigt_repose_20_minutes_apres_n_est_pas_une_sortie(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    pointeuse.pointer("1", a(8, 0))   # moins de 30 min : ce n'est pas une sortie
    synchroniser(conn, pointeuse, config, maintenant=a(8, 1))
    assert compter(conn, "pointages") == 1


def test_premier_pointage_arrivee_suivant_sortie(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    pointeuse.pointer("1", a(10, 15))  # parti plus tôt (malade) : c'est bien une sortie
    synchroniser(conn, pointeuse, config, maintenant=a(10, 16))
    types = [r[0] for r in conn.execute("SELECT type FROM pointages ORDER BY horodatage")]
    assert types == ["arrivee", "sortie"]


def test_une_seule_alerte_par_type_et_par_jour(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("1", a(7, 40))
    pointeuse.pointer("1", a(17, 0))
    pointeuse.pointer("1", a(17, 45))  # deuxième sortie : enregistrée, mais pas de 2e message
    synchroniser(conn, pointeuse, config, maintenant=a(17, 46))
    assert compter(conn, "pointages") == 3
    assert compter(conn, "notifications") == 2


def test_classification_et_retards(config):
    h = config.horaires
    assert classer(a(7, 59), False, h) == ("arrivee", False)
    assert classer(a(8, 0), False, h) == ("arrivee", False)
    assert classer(a(8, 5), False, h) == ("arrivee", True)
    assert classer(a(11, 30), False, h) == ("arrivee", True)   # arrivée très tardive
    assert classer(a(17, 0), True, h) == ("sortie", False)
    assert classer(a(10, 0), True, h) == ("sortie", False)     # départ anticipé
    assert classer(a(16, 30), False, h) == ("sortie", False)   # arrivée oubliée


def test_empreinte_inconnue_enregistree_sans_alerte(conn, config, trois_eleves):
    pointeuse = PointeuseSimulee()
    pointeuse.pointer("99", a(7, 40))  # un enseignant, par exemple
    synchroniser(conn, pointeuse, config, maintenant=a(7, 41))
    assert compter(conn, "pointages") == 1
    assert compter(conn, "notifications") == 0
