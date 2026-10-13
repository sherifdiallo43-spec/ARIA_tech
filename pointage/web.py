"""Tableau de bord web d'ARIA_point.

Il tourne sur le PC de l'école et s'ouvre dans le navigateur. Deux pages :
- Aujourd'hui : présents, absents, retards et derniers pointages, en direct
- Rapports : les PDF du jour et du mois, à télécharger ou à générer

Un seul mot de passe (celui de l'administration). Le relevé du prestataire
n'est volontairement PAS accessible ici : c'est un document pour Chérif, pas
pour le personnel de l'école.
"""
from __future__ import annotations

import hmac
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import db, reports
from .config import Config

# Dossiers de rapports que la page peut afficher et servir (jamais "prestataire").
DOSSIERS_RAPPORTS = {"journaliers": "Rapports du jour", "mensuels": "Rapports du mois"}
TYPES = {"arrivee": "Arrivée", "sortie": "Sortie"}


def create_app(config: Config, mot_de_passe: str) -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    # Clé qui signe les cookies de connexion. Nouvelle à chaque démarrage : relancer
    # le service déconnecte tout le monde, ce qui est acceptable et plus sûr.
    app.secret_key = secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    @app.template_filter("heure")
    def filtre_heure(texte):
        """"07:42" -> "07h42"."""
        return texte.replace(":", "h") if texte else ""

    @app.context_processor
    def contexte_commun():
        return {"nom_ecole": config.nom_ecole, "connecte": session.get("ok", False)}

    def conn():
        if "conn" not in g:
            g.conn = db.connecter(config.chemin("base"))
        return g.conn

    @app.teardown_appcontext
    def fermer(_exception):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.before_request
    def exiger_connexion():
        if request.endpoint not in ("connexion", "static") and not session.get("ok"):
            return redirect(url_for("connexion"))

    # --- Connexion ---------------------------------------------------------------

    @app.route("/connexion", methods=["GET", "POST"])
    def connexion():
        if request.method == "POST":
            saisi = request.form.get("mot_de_passe", "")
            if hmac.compare_digest(saisi.encode(), mot_de_passe.encode()):
                session.clear()
                session["ok"] = True
                return redirect(url_for("aujourdhui"))
            time.sleep(1)  # ralentit quelqu'un qui essaierait des mots de passe au hasard
            flash("Mot de passe incorrect.")
        return render_template("connexion.html")

    @app.post("/deconnexion")
    def deconnexion():
        session.clear()
        return redirect(url_for("connexion"))

    # --- Aujourd'hui ---------------------------------------------------------------

    @app.route("/")
    def aujourdhui():
        try:
            jour = date.fromisoformat(request.args.get("jour", ""))
        except ValueError:
            jour = date.today()
        stats = reports.calculer_jour(conn(), jour)

        derniers = conn().execute(
            "SELECT p.horodatage, p.type, p.en_retard, e.prenom, e.nom, e.classe "
            "FROM pointages p LEFT JOIN eleves e ON e.zk_user_id = p.zk_user_id "
            "WHERE substr(p.horodatage, 1, 10) = ? ORDER BY p.horodatage DESC LIMIT 12",
            (jour.isoformat(),)).fetchall()

        # La pointeuse est "en ligne" si elle a été lue récemment.
        derniere = db.lire_etat(conn(), "derniere_sync")
        intervalle = int(config.pointeuse.get("intervalle_sync_secondes", 30))
        en_ligne = bool(derniere) and (datetime.now() - db.texte_dt(derniere)
                                       < timedelta(seconds=intervalle * 4 + 30))
        return render_template(
            "aujourdhui.html", stats=stats, jour=jour, derniers=derniers, types=TYPES,
            jour_texte=reports._date_longue(jour), est_aujourdhui=jour == date.today(),
            veille=(jour - timedelta(days=1)).isoformat(),
            lendemain=(jour + timedelta(days=1)).isoformat() if jour < date.today() else None,
            derniere_lecture=derniere[11:16].replace(":", "h") if derniere else None,
            en_ligne=en_ligne)

    # --- Rapports ------------------------------------------------------------------

    def lister_rapports():
        liste = {}
        for dossier, titre in DOSSIERS_RAPPORTS.items():
            chemin = config.chemin("rapports") / dossier
            fichiers = sorted(chemin.glob("*.pdf"), reverse=True) if chemin.exists() else []
            liste[dossier] = (titre, [f.name for f in fichiers])
        return liste

    @app.route("/rapports")
    def rapports_page():
        return render_template("rapports.html", liste=lister_rapports(),
                               aujourdhui=date.today().isoformat(),
                               ce_mois=date.today().strftime("%Y-%m"))

    @app.post("/rapports/generer")
    def generer():
        try:
            if request.form.get("type") == "jour":
                jour = date.fromisoformat(request.form.get("jour", ""))
                reports.generer_rapport_jour(conn(), config, jour)
            else:
                annee, mois = (int(x) for x in request.form.get("mois", "").split("-"))
                reports.generer_rapport(conn(), config, annee, mois)
            flash("Rapport généré.")
        except ValueError:
            flash("Date invalide.")
        return redirect(url_for("rapports_page"))

    @app.route("/rapports/<dossier>/<fichier>")
    def telecharger(dossier, fichier):
        if dossier not in DOSSIERS_RAPPORTS or not fichier.endswith(".pdf"):
            abort(404)
        return send_from_directory(config.chemin("rapports") / dossier, fichier)

    return app
