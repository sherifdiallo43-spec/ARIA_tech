"""Copie quotidienne des sauvegardes sur une clé USB (ou un disque externe)."""
import sqlite3
from datetime import datetime

from pointage import backup

MAINTENANT = datetime(2026, 10, 17, 18, 0)


def _cle_usb(tmp_path, nom="ARIA_point_sauvegardes"):
    """Simule une clé USB branchée sous la lettre F:, avec le dossier convenu."""
    racine = tmp_path / "F"
    (racine / nom).mkdir(parents=True)
    return racine


def test_copie_trouvee_quelle_que_soit_la_lettre_de_la_cle(conn, config, trois_eleves, tmp_path):
    config.sauvegarde["copie_externe"] = "ARIA_point_sauvegardes"
    autre_lecteur = tmp_path / "E"
    autre_lecteur.mkdir()
    cle = _cle_usb(tmp_path)

    copie = backup.copier_vers_externe(conn, config, MAINTENANT, racines=[autre_lecteur, cle])

    assert copie.parent == cle / "ARIA_point_sauvegardes"
    lecture = sqlite3.connect(copie)
    assert lecture.execute("SELECT COUNT(*) FROM eleves").fetchone()[0] == 3
    lecture.close()
    assert not list(copie.parent.glob("*.tmp"))  # pas de fichier temporaire oublié


def test_cle_absente_rien_ne_casse(conn, config, trois_eleves, tmp_path):
    config.sauvegarde["copie_externe"] = "ARIA_point_sauvegardes"
    assert backup.copier_externe_si_besoin(conn, config, MAINTENANT, racines=[tmp_path]) is None
    assert backup.alerte_copie_externe(conn, config, MAINTENANT).startswith("Aucune sauvegarde")


def test_une_copie_par_jour_seulement(conn, config, trois_eleves, tmp_path):
    config.sauvegarde["copie_externe"] = "ARIA_point_sauvegardes"
    cle = _cle_usb(tmp_path)
    assert backup.copier_externe_si_besoin(conn, config, MAINTENANT, racines=[cle]) is not None
    plus_tard = MAINTENANT.replace(hour=22)
    assert backup.copier_externe_si_besoin(conn, config, plus_tard, racines=[cle]) is None


def test_alerte_apres_3_jours_sans_copie(conn, config, trois_eleves, tmp_path):
    config.sauvegarde["copie_externe"] = "ARIA_point_sauvegardes"
    backup.copier_vers_externe(conn, config, MAINTENANT, racines=[_cle_usb(tmp_path)])
    assert backup.alerte_copie_externe(conn, config, datetime(2026, 10, 19, 18, 0)) is None
    assert "3 jours" in backup.alerte_copie_externe(conn, config, datetime(2026, 10, 20, 18, 0))


def test_pas_de_reglage_pas_d_alerte(conn, config, trois_eleves):
    config.sauvegarde["copie_externe"] = ""
    assert backup.copier_externe_si_besoin(conn, config, MAINTENANT) is None
    assert backup.alerte_copie_externe(conn, config, MAINTENANT) is None


def test_copie_abimee_jamais_gardee(conn, config, trois_eleves, tmp_path, monkeypatch):
    """Si la vérification échoue (clé arrachée, secteur défectueux), rien n'est laissé sur la clé."""
    import pytest

    config.sauvegarde["copie_externe"] = "ARIA_point_sauvegardes"
    cle = _cle_usb(tmp_path)
    backup.sauvegarder(conn, config, MAINTENANT)
    monkeypatch.setattr(backup.shutil, "copyfile",
                        lambda src, dst: open(dst, "wb").write(b"pas une base sqlite" * 100))
    with pytest.raises(Exception):
        backup.copier_vers_externe(conn, config, MAINTENANT, racines=[cle])
    assert not list((cle / "ARIA_point_sauvegardes").iterdir())
