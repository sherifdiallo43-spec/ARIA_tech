"""Point d'entrée du logiciel. Tape `py main.py --help` pour voir les commandes."""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

from pointage import (archive, backup, collector, courriel, db, eleves, notifier, personnel, purge,
                      reports, service, verification)
from pointage.config import Config, charger
from pointage.device import (PointeuseSimulee, PointageBrut, creer_pointeuse, generer_journee,
                             generer_journee_personnel, lister_pointeuses_zk)
from pointage.subscriptions import enregistrer_paiement, statut_abonnement


def configurer_logs(config: Config) -> None:
    """Journal à l'écran ET dans logs/pointage.log (5 fichiers de 1 Mo max)."""
    dossier = config.chemin("logs")
    dossier.mkdir(parents=True, exist_ok=True)
    format_ = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fichier = RotatingFileHandler(dossier / "pointage.log", maxBytes=1_000_000,
                                  backupCount=5, encoding="utf-8")
    ecran = logging.StreamHandler()
    for gestionnaire in (fichier, ecran):
        gestionnaire.setFormatter(format_)
    logging.basicConfig(level=logging.INFO, handlers=[fichier, ecran])
    logging.getLogger("twilio").setLevel(logging.WARNING)  # sinon chaque envoi affiche tous les en-têtes HTTP


def ouvrir_base(config: Config):
    conn = db.connecter(config.chemin("base"))
    db.initialiser(conn)
    return conn


# --- Commandes -------------------------------------------------------------------

def cmd_init(config, args):
    ouvrir_base(config).close()
    print(f"Base prête : {config.chemin('base')}")


def cmd_import_eleves(config, args):
    conn = ouvrir_base(config)
    crees, maj, erreurs = eleves.importer_csv(conn, args.fichier, date.today(),
                                              config.abonnement["essai_jours"])
    print(f"{crees} élève(s) créé(s), {maj} mis à jour.")
    for erreur in erreurs:
        print(f"  ERREUR {erreur}")


def cmd_import_personnel(config, args):
    conn = ouvrir_base(config)
    crees, maj, erreurs = personnel.importer_csv(conn, args.fichier, date.today())
    print(f"{crees} membre(s) du personnel créé(s), {maj} mis à jour.")
    for erreur in erreurs:
        print(f"  ERREUR {erreur}")


def cmd_import_emploi_du_temps(config, args):
    conn = ouvrir_base(config)
    cours, membres, erreurs = personnel.importer_emploi_du_temps(conn, args.fichier)
    print(f"{cours} cours importé(s) pour {membres} membre(s) du personnel.")
    for erreur in erreurs:
        print(f"  ERREUR {erreur}")


def cmd_rapport_personnel(config, args):
    conn = ouvrir_base(config)
    if args.mois:
        annee, mois = (int(x) for x in args.mois.split("-"))
        chemin = reports.generer_rapport_personnel_mois(conn, config, annee, mois)
        periode = reports.periode_mois(annee, mois)
    else:
        jour = date.fromisoformat(args.jour) if args.jour else date.today()
        chemin = reports.generer_rapport_personnel_jour(conn, config, jour)
        periode = reports.periode_jour(jour)
    print(f"Rapport du personnel créé : {chemin}")
    if args.envoyer:
        courriel.envoyer_rapport(conn, config, courriel.creer_envoyeur(config), chemin, periode)
        print(f"Envoyé à {config.directeur.get('email')}.")


def cmd_test_email(config, args):
    try:
        courriel.tester(config, args.adresse)
    except Exception as e:
        sys.exit(f"Échec de l'envoi à {args.adresse} : {e}")
    print(f"Email envoyé à {args.adresse} (mode {config.directeur.get('mode', 'console')}).")


def cmd_paiement(config, args):
    conn = ouvrir_base(config)
    fin = enregistrer_paiement(conn, args.zk_id, args.mois, config.abonnement["prix_mensuel"],
                               date.today(), montant=args.montant, note=args.note)
    print(f"Paiement enregistré. Abonnement valable jusqu'au {fin:%d/%m/%Y} inclus.")


def cmd_sync(config, args):
    conn = ouvrir_base(config)
    try:
        resultat = collector.synchroniser(conn, creer_pointeuse(config), config)
        print(f"Pointeuse : {resultat.lus} lus, {resultat.nouveaux} nouveaux, "
              f"{resultat.deja_connus} déjà connus, {resultat.rebonds} doublons ignorés.")
    except Exception as e:
        # On n'abandonne pas : les messages déjà en attente peuvent quand même partir.
        print(f"Pointeuse injoignable ({type(e).__name__} : {e}). "
              f"Vérifie qu'elle est allumée, son adresse IP dans config.toml et le câble réseau.")
    envois = notifier.envoyer_en_attente(conn, notifier.creer_envoyeur(config), config)
    print(f"Messages : {envois}")


def cmd_run(config, args):
    service.lancer(config)


def cmd_simuler(config, args):
    if config.pointeuse["mode"] != "simulee":
        sys.exit("La commande simuler ne marche qu'avec mode = \"simulee\" dans config.toml.")
    conn = ouvrir_base(config)
    zk_ids = [r["zk_user_id"] for r in conn.execute(
        "SELECT zk_user_id FROM eleves WHERE actif_scolarite = 1")]
    equipe = personnel.lister(conn)
    if not zk_ids and not equipe:
        sys.exit("Aucun élève : lance d'abord import-eleves.")
    pointeuse = PointeuseSimulee(config.chemin("pointeuse_simulee"))
    rng = random.Random(args.graine)

    if args.jours:
        # Historique : les N derniers jours de classe (lundi à samedi), sans aujourd'hui.
        pointages, jour = [], date.today()
        jours_faits = 0
        while jours_faits < args.jours:
            jour -= timedelta(days=1)
            if jour.weekday() < 6:  # au Sénégal : cours du lundi au samedi
                pointages += generer_journee(zk_ids, jour, rng)
                cours = [(m["zk_user_id"], personnel.creneaux_du_jour(conn, m["zk_user_id"], jour))
                         for m in equipe]
                pointages += generer_journee_personnel([c for c in cours if c[1]], jour, rng)
                jours_faits += 1
    else:
        # Pointages "en direct" pour quelques élèves : maintenant, ou aujourd'hui
        # à l'heure demandée (pratique pour une démo hors des heures de cours).
        moment = datetime.now().replace(microsecond=0)
        if args.heure:
            heures, minutes = (int(x) for x in args.heure.split(":"))
            moment = moment.replace(hour=heures, minute=minutes, second=0)
        choisis = rng.sample(zk_ids, min(args.n, len(zk_ids)))
        choisis += rng.sample([m["zk_user_id"] for m in equipe], min(args.profs, len(equipe)))
        pointages = [PointageBrut(zk_id, moment) for zk_id in choisis]
    pointeuse.ajouter(pointages)
    print(f"{len(pointages)} pointage(s) ajouté(s) dans la pointeuse simulée.")


def cmd_rapport(config, args):
    conn = ouvrir_base(config)
    if args.mois:
        annee, mois = (int(x) for x in args.mois.split("-"))
        print(f"Rapport mensuel créé : {reports.generer_rapport(conn, config, annee, mois)}")
    else:
        jour = date.fromisoformat(args.jour) if args.jour else date.today()
        print(f"Rapport du jour créé : {reports.generer_rapport_jour(conn, config, jour)}")


def cmd_releve(config, args):
    conn = ouvrir_base(config)
    if args.mois:
        annee, mois = (int(x) for x in args.mois.split("-"))
    else:
        annee, mois = reports.mois_precedent(date.today())
    print(f"Relevé du prestataire créé : {reports.generer_releve(conn, config, annee, mois)}")


def cmd_sauvegarder(config, args):
    conn = ouvrir_base(config)
    print(f"Sauvegarde créée : {backup.sauvegarder(conn, config)}")
    if config.sauvegarde.get("copie_externe"):
        copie = backup.copier_vers_externe(conn, config)
        print(f"Copie externe    : {copie}" if copie else
              f"Copie externe    : IMPOSSIBLE, dossier « {config.sauvegarde['copie_externe']} » "
              "introuvable. La clé USB est-elle branchée ?")


def cmd_archiver(config, args):
    _, fin_annee = (int(x) for x in args.annee.split("-"))
    date_fin = date.fromisoformat(args.fin) if args.fin else date(fin_annee, 7, 31)
    print(f"Tous les pointages, messages et paiements jusqu'au {date_fin:%d/%m/%Y} inclus "
          f"vont être archivés puis RETIRÉS de la base principale.\n"
          f"Les fiches élèves sont conservées. Une sauvegarde complète est faite avant.")
    if not args.oui and input("Tape ARCHIVER pour confirmer : ").strip() != "ARCHIVER":
        print("Annulé, rien n'a été modifié.")
        return
    resultat = archive.archiver_annee(ouvrir_base(config), config, args.annee, date_fin)
    print(f"Archive : {resultat['archive']}")
    print(f"Lignes archivées : {resultat['comptes']}")
    print(f"Sauvegarde de sécurité : {resultat['sauvegarde']}")


def cmd_vider_pointeuse(config, args):
    print("La mémoire de la pointeuse va être lue, mise en sécurité (base + sauvegarde + copie brute),\n"
          "puis EFFACÉE. Le boîtier sera bloqué quelques secondes.")
    if not args.oui and input("Tape VIDER pour confirmer : ").strip() != "VIDER":
        print("Annulé, rien n'a été effacé.")
        return
    try:
        resume = purge.vider_memoire(ouvrir_base(config), creer_pointeuse(config), config)
    except Exception as e:
        sys.exit(f"Vidage impossible ({type(e).__name__} : {e}). Rien n'a été effacé.")
    print(f"{resume['lus']} pointage(s) mis en sécurité ({resume['nouveaux']} nouveaux), mémoire vidée.")
    print(f"Copie brute de la mémoire : {resume['copie_brute']}")
    print(f"Sauvegarde de la base     : {resume['sauvegarde']}")


def cmd_statut(config, args):
    conn = ouvrir_base(config)
    aujourd_hui = date.today()
    liste = conn.execute("SELECT * FROM eleves WHERE actif_scolarite = 1").fetchall()
    statuts = {"actif": 0, "essai": 0, "expire": 0}
    for e in liste:
        statuts[statut_abonnement(e, aujourd_hui)] += 1
    pointages_jour = conn.execute("SELECT COUNT(*) FROM pointages WHERE substr(horodatage, 1, 10) = ?",
                                  (aujourd_hui.isoformat(),)).fetchone()[0]
    attente = conn.execute("SELECT COUNT(*) FROM notifications WHERE statut = 'en_attente'").fetchone()[0]
    print(f"École               : {config.nom_ecole}")
    print(f"Élèves              : {len(liste)} (payé : {statuts['actif']}, essai : {statuts['essai']}, "
          f"expiré : {statuts['expire']})")
    print(f"Personnel           : {len(personnel.lister(conn))}")
    print(f"Pointages du jour   : {pointages_jour}")
    print(f"Messages en attente : {attente}")
    print(f"Dernière lecture    : {db.lire_etat(conn, 'derniere_sync', 'jamais')}")
    print(f"Dernière sauvegarde : {db.lire_etat(conn, 'derniere_sauvegarde', 'jamais')}")
    if config.sauvegarde.get("copie_externe"):
        print(f"Copie sur clé USB   : {db.lire_etat(conn, 'derniere_copie_externe', 'jamais')}")
        alerte = backup.alerte_copie_externe(conn, config)
        if alerte:
            print(f"  ATTENTION : {alerte}")
    else:
        print("Copie sur clé USB   : NON RÉGLÉE (copie_externe dans [sauvegarde] de config.toml)")
    print(f"Mémoire pointeuse   : {db.lire_etat(conn, 'memoire_pointeuse', '?')} pointage(s) "
          f"(vidage auto au-delà de {config.pointeuse.get('vider_au_dela', 0)}, "
          f"dernier vidage : {db.lire_etat(conn, 'dernier_vidage', 'jamais')})")


def cmd_test_whatsapp(config, args):
    _tester_envoi(config, args, "twilio", "WhatsApp")


def cmd_test_sms(config, args):
    _tester_envoi(config, args, "twilio_sms", "SMS")


def _tester_envoi(config, args, mode, nom_canal):
    """Envoie un message de test par Twilio, quel que soit le mode réglé dans config.toml."""
    tel = eleves.normaliser_telephone(args.numero)
    config.notifications["mode"] = mode
    envoyeur = notifier.creer_envoyeur(config)
    message = f"{config.nom_ecole} : test ARIA_point. Si tu lis ce message, {nom_canal} fonctionne."
    variables = {"1": config.nom_ecole, "2": "Élève Test", "3": "est arrivé(e) à l'école", "4": "07h42"}
    try:
        envoyeur.envoyer(tel, message, variables)
    except (notifier.ErreurDefinitive, notifier.ErreurTemporaire) as e:
        conseil = ""
        if "63015" in str(e):  # téléphone pas encore inscrit au bac à sable
            conseil = (" Bac à sable : ce téléphone a-t-il envoyé le message 'join ...' au numéro Twilio"
                       " (il expire au bout de 72 h environ) ?")
        sys.exit(f"Échec de l'envoi à {tel} : {e}{conseil}")
    print(f"Message envoyé à {tel}. Regarde ton téléphone.")


def cmd_web(config, args):
    from waitress import serve

    from pointage import web
    from pointage.web import create_app

    mot_de_passe = config.secrets.get("DASHBOARD_PASSWORD", "")
    if len(mot_de_passe) < web.MOT_DE_PASSE_MIN:
        sys.exit(f"Mets un mot de passe d'au moins {web.MOT_DE_PASSE_MIN} caractères dans .env "
                 "(DASHBOARD_PASSWORD=...). Une phrase est plus facile à retenir.")
    ouvrir_base(config).close()  # crée la base si elle n'existe pas encore
    # 127.0.0.1 : seul ce PC peut ouvrir le tableau de bord. 0.0.0.0 : tout le réseau de l'école.
    hote = "0.0.0.0" if args.reseau else "127.0.0.1"
    print(f"Tableau de bord : http://localhost:{args.port}"
          + ("  (aussi accessible depuis le réseau de l'école)" if args.reseau else ""))
    serve(create_app(config, mot_de_passe), host=hote, port=args.port)


def cmd_utilisateurs(config, args):
    try:
        utilisateurs = creer_pointeuse(config).lister_utilisateurs()
    except Exception as e:
        sys.exit(f"Pointeuse injoignable ({type(e).__name__} : {e}).")
    for zk_id, nom in utilisateurs:
        print(f"{zk_id:>6}  {nom}")


def cmd_verifier_numeros(config, args):
    """Compare les numéros enregistrés sur le boîtier avec ceux de la base."""
    try:
        utilisateurs = creer_pointeuse(config).lister_utilisateurs()
    except Exception as e:
        sys.exit(f"Pointeuse injoignable ({type(e).__name__} : {e}).")
    ecarts = verification.comparer(ouvrir_base(config), utilisateurs)
    print(f"{ecarts.ok} numéro(s) en règle (présents sur le boîtier ET dans la base).")

    if ecarts.numeros_en_double:
        print("\nDANGER : même numéro pour un élève ET un membre du personnel :")
        print("  " + ", ".join(ecarts.numeros_en_double))
    if ecarts.inconnus_sur_boitier:
        print("\nSur le boîtier mais ABSENTS de la base (faute de frappe ? CSV pas importé ?) :")
        for zk_id, nom in ecarts.inconnus_sur_boitier:
            print(f"  {zk_id:>6}  {nom}")
    if ecarts.eleves_sans_doigt:
        print(f"\nÉlèves dans la base sans empreinte sur le boîtier ({len(ecarts.eleves_sans_doigt)}) :")
        for e in ecarts.eleves_sans_doigt:
            print(f"  {e['zk_user_id']:>6}  {e['classe']:<8} {e['prenom']} {e['nom']}")
    if ecarts.personnel_sans_doigt:
        print(f"\nPersonnel dans la base sans empreinte sur le boîtier ({len(ecarts.personnel_sans_doigt)}) :")
        for p in ecarts.personnel_sans_doigt:
            print(f"  {p['zk_user_id']:>6}  {p['prenom']} {p['nom']}")
    if ecarts.propre:
        print("\nTout est cohérent.")


def cmd_copier_empreintes(config, args):
    """Recopie les personnes et leurs empreintes d'un boîtier vers tous les autres."""
    boitiers = lister_pointeuses_zk(config)
    if len(boitiers) < 2:
        sys.exit("Il faut au moins 2 pointeuses en mode \"zk\" (blocs [[pointeuses]] dans config.toml).")
    if args.source:
        choisis = [b for b in boitiers if args.source in (b[0], b[1].ip)]
        if not choisis:
            sys.exit(f"Aucune pointeuse ne s'appelle « {args.source} ». Noms possibles : "
                     + ", ".join(nom for nom, _ in boitiers))
        nom_source, source = choisis[0]
    else:
        nom_source, source = boitiers[0]
    cibles = [b for b in boitiers if b[1] is not source]

    try:
        carnet = source.lire_empreintes()
    except Exception as e:
        sys.exit(f"Lecture de « {nom_source} » impossible ({type(e).__name__} : {e}).")
    print(f"« {nom_source} » : {len(carnet.fiches)} personne(s), {carnet.nb_doigts} empreinte(s).")
    if not carnet.fiches:
        sys.exit("Rien à copier : enregistre d'abord les doigts sur ce boîtier.")
    print(f"Elles vont être recopiées sur : {', '.join(nom for nom, _ in cibles)}.\n"
          "Les personnes déjà présentes sont mises à jour, les autres ne sont pas touchées.\n"
          "Chaque boîtier est BLOQUÉ pendant la copie (plusieurs minutes pour 1 000 élèves) :\n"
          "à faire hors des heures d'arrivée et de sortie.")
    if not args.oui and input("Tape COPIER pour confirmer : ").strip() != "COPIER":
        print("Annulé, rien n'a été copié.")
        return

    def progression(fait, total):
        if fait % 100 == 0 or fait == total:
            print(f"  {fait}/{total}")

    echecs = []
    for nom, cible in cibles:
        print(f"\nCopie vers « {nom} »...")
        try:
            cible.ecrire_empreintes(carnet, progression)
            # On relit le boîtier : seule une relecture prouve que les empreintes y sont.
            incompletes = carnet.incompletes_dans(cible.lire_empreintes())
        except Exception as e:
            print(f"  ÉCHEC ({type(e).__name__} : {e})")
            echecs.append(nom)
            continue
        if incompletes:
            print(f"  ATTENTION : {len(incompletes)} personne(s) absente(s) ou incomplète(s) après la copie : "
                  + ", ".join(incompletes[:30]) + (" ..." if len(incompletes) > 30 else ""))
            echecs.append(nom)
        else:
            print("  Vérifié : toutes les personnes et empreintes sont présentes.")
    if echecs:
        sys.exit(f"\nÀ relancer pour : {', '.join(echecs)}. La copie peut être refaite sans risque.")
    print("\nCopie terminée sur tous les boîtiers.")


# --- Analyse de la ligne de commande -------------------------------------------

def construire_parseur() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pointage scolaire : pointeuse ZKTeco -> alertes WhatsApp.")
    sous = p.add_subparsers(dest="commande", required=True)

    sous.add_parser("init", help="crée la base de données").set_defaults(func=cmd_init)

    s = sous.add_parser("import-eleves", help="importe ou met à jour les élèves depuis un CSV")
    s.add_argument("fichier")
    s.set_defaults(func=cmd_import_eleves)

    s = sous.add_parser("import-personnel", help="importe ou met à jour les profs et le personnel depuis un CSV")
    s.add_argument("fichier")
    s.set_defaults(func=cmd_import_personnel)

    s = sous.add_parser("import-emploi-du-temps", help="importe l'emploi du temps du personnel (une ligne par cours)")
    s.add_argument("fichier")
    s.set_defaults(func=cmd_import_emploi_du_temps)

    s = sous.add_parser("rapport-personnel", help="rapport PDF du personnel pour le directeur (jour ou mois)")
    groupe = s.add_mutually_exclusive_group()
    groupe.add_argument("--jour", help="AAAA-MM-JJ (par défaut : aujourd'hui)")
    groupe.add_argument("--mois", help="AAAA-MM : rapport mensuel")
    s.add_argument("--envoyer", action="store_true", help="l'envoie aussi par email au directeur")
    s.set_defaults(func=cmd_rapport_personnel)

    s = sous.add_parser("test-email", help="envoie un email de test (réglages [directeur] et SMTP_...)")
    s.add_argument("adresse")
    s.set_defaults(func=cmd_test_email)

    s = sous.add_parser("paiement", help="enregistre un paiement d'abonnement")
    s.add_argument("zk_id", help="numéro de l'élève dans la pointeuse")
    s.add_argument("--mois", type=int, default=1)
    s.add_argument("--montant", type=int, help="par défaut : prix mensuel x nombre de mois")
    s.add_argument("--note")
    s.set_defaults(func=cmd_paiement)

    sous.add_parser("sync", help="une lecture de la pointeuse + envoi des messages").set_defaults(func=cmd_sync)
    sous.add_parser("run", help="lance le service en continu").set_defaults(func=cmd_run)

    s = sous.add_parser("test-sms", help="envoie un SMS de test (Twilio)")
    s.add_argument("numero", help="ex. 76 500 08 43")
    s.set_defaults(func=cmd_test_sms)

    s = sous.add_parser("test-whatsapp", help="envoie un message WhatsApp de test (Twilio)")
    s.add_argument("numero", help="ex. 76 500 08 43")
    s.set_defaults(func=cmd_test_whatsapp)

    s = sous.add_parser("web", help="lance le tableau de bord web")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reseau", action="store_true",
                   help="accessible depuis les autres appareils du réseau (par défaut : ce PC seulement)")
    s.set_defaults(func=cmd_web)

    s = sous.add_parser("simuler", help="ajoute des pointages dans la pointeuse simulée")
    s.add_argument("--jours", type=int, help="génère un historique sur N jours de classe passés (lundi-samedi)")
    s.add_argument("--n", type=int, default=5, help="nombre d'élèves qui pointent maintenant")
    s.add_argument("--profs", type=int, default=0, help="nombre de membres du personnel qui pointent maintenant")
    s.add_argument("--heure", help="HH:MM : faire pointer aujourd'hui à cette heure plutôt que maintenant")
    s.add_argument("--graine", type=int, help="pour obtenir toujours la même simulation")
    s.set_defaults(func=cmd_simuler)

    s = sous.add_parser("rapport", help="génère un rapport PDF (du jour par défaut, ou d'un mois)")
    groupe = s.add_mutually_exclusive_group()
    groupe.add_argument("--jour", help="AAAA-MM-JJ (par défaut : aujourd'hui)")
    groupe.add_argument("--mois", help="AAAA-MM : rapport mensuel")
    s.set_defaults(func=cmd_rapport)

    s = sous.add_parser("releve", help="relevé mensuel du prestataire : paiements, montant dû, relances")
    s.add_argument("--mois", help="AAAA-MM (par défaut : le mois précédent)")
    s.set_defaults(func=cmd_releve)

    sous.add_parser("sauvegarder", help="sauvegarde immédiate de la base").set_defaults(func=cmd_sauvegarder)

    s = sous.add_parser("archiver", help="archivage de fin d'année scolaire")
    s.add_argument("--annee", required=True, help="ex. 2026-2027")
    s.add_argument("--fin", help="dernier jour archivé AAAA-MM-JJ (par défaut : 31 juillet)")
    s.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    s.set_defaults(func=cmd_archiver)

    s = sous.add_parser("vider-pointeuse", help="met tout en sécurité puis vide la mémoire du boîtier")
    s.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    s.set_defaults(func=cmd_vider_pointeuse)

    sous.add_parser("statut", help="résumé de l'état du système").set_defaults(func=cmd_statut)
    sous.add_parser("utilisateurs", help="liste les utilisateurs de la pointeuse").set_defaults(func=cmd_utilisateurs)
    sous.add_parser("verifier-numeros",
                    help="compare les numéros du boîtier avec la base (doigts manquants, fautes de frappe)"
                    ).set_defaults(func=cmd_verifier_numeros)

    s = sous.add_parser("copier-empreintes",
                        help="recopie les personnes et empreintes d'un boîtier vers les autres")
    s.add_argument("--source", help="nom ou IP du boîtier de départ (par défaut : le premier de config.toml)")
    s.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    s.set_defaults(func=cmd_copier_empreintes)
    return p


def main() -> None:
    args = construire_parseur().parse_args()
    config = charger()
    configurer_logs(config)
    try:
        args.func(config, args)
    except (ValueError, FileExistsError) as e:
        sys.exit(f"Erreur : {e}")


if __name__ == "__main__":
    main()
