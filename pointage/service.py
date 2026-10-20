"""Le service qui tourne en continu sur le PC de l'école.

Toutes les 30 s : lire la pointeuse, puis envoyer les messages en attente.
Toutes les heures : sauvegarde (si plus de 24 h), copie sur clé USB (si branchée), rapports du jour et du mois (si absents),
envoi au directeur des rapports du personnel pas encore envoyés,
et vidage de la mémoire de la pointeuse (si elle dépasse le seuil).
Toutes les 6 h : remettre la pointeuse à l'heure.

Chaque tâche attrape ses propres erreurs : une pointeuse débranchée ou une
coupure internet ne doit jamais arrêter le service. Il réessaie au cycle suivant.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import schedule

from . import backup, collector, courriel, db, notifier, purge, reports
from .config import Config
from .device import creer_pointeuse

log = logging.getLogger(__name__)


def lancer(config: Config) -> None:
    conn = db.connecter(config.chemin("base"))
    db.initialiser(conn)
    pointeuse = creer_pointeuse(config)
    envoyeur = notifier.creer_envoyeur(config)
    envoyeur_email = courriel.creer_envoyeur(config)
    intervalle = int(config.pointeuse.get("intervalle_sync_secondes", 30))

    def cycle_pointage():
        try:
            collector.synchroniser(conn, pointeuse, config)
        except Exception as e:
            log.warning("Lecture de la pointeuse impossible (%s). Nouvel essai dans %d s.", e, intervalle)
        try:
            notifier.creer_alertes_absence(conn, config)
        except Exception:
            log.exception("Erreur pendant la préparation des alertes d'absence.")
        try:
            notifier.envoyer_en_attente(conn, envoyeur, config)
        except Exception:
            log.exception("Erreur pendant l'envoi des messages.")

    def taches_horaires():
        try:
            backup.sauvegarder_si_besoin(conn, config)
            reports.rapports_automatiques(conn, config)
        except Exception:
            log.exception("Erreur pendant la sauvegarde ou le rapport.")
        try:
            backup.copier_externe_si_besoin(conn, config)
        except Exception:
            log.exception("Erreur pendant la copie de la sauvegarde sur la clé USB.")
        try:
            courriel.envoyer_en_attente(conn, config, envoyeur_email,
                                        reports.rapports_personnel_recents(config, date.today()))
        except Exception:
            log.exception("Erreur pendant l'envoi des rapports au directeur.")
        try:
            purge.vider_si_necessaire(conn, pointeuse, config)
        except Exception as e:
            log.warning("Vidage de la pointeuse impossible (%s). Rien n'a été effacé.", e)

    def mise_a_l_heure():
        try:
            pointeuse.synchroniser_heure()
        except Exception as e:
            log.warning("Mise à l'heure de la pointeuse impossible (%s).", e)

    # Un premier passage immédiat, puis la planification.
    mise_a_l_heure()
    cycle_pointage()
    taches_horaires()
    schedule.every(intervalle).seconds.do(cycle_pointage)
    schedule.every(1).hours.do(taches_horaires)
    schedule.every(6).hours.do(mise_a_l_heure)

    log.info("Service démarré (pointeuse : %s, messages : %s). Ctrl+C pour arrêter.",
             config.pointeuse["mode"], config.notifications["mode"])
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Arrêt demandé.")
    finally:
        schedule.clear()
        conn.close()
