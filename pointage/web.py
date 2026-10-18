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
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import backup, db, reports
from .config import Config

# Dossiers de rapports que la page peut afficher et servir (jamais "prestataire").
DOSSIERS_RAPPORTS = {"journaliers": "Rapports du jour", "mensuels": "Rapports du mois"}
TYPES = {"arrivee": "Arrivée", "sortie": "Sortie"}

ESSAIS_MAX = 5          # mots de passe faux autorisés...
BLOCAGE_MINUTES = 15    # ...par appareil, avant un blocage de 15 minutes
MOT_DE_PASSE_MIN = 12   # longueur minimale du mot de passe (vérifiée au lancement)
INACTIVITE_MINUTES = 30 # sans clic pendant 30 min, il faut se reconnecter (PC partagé du secrétariat)


class LimiteurConnexion:
    """Bloque un appareil (adresse IP) après trop de mots de passe faux.

    Sans cette limite, un programme pourrait essayer des milliers de mots de
    passe à la suite. Le compteur est en mémoire : il repart à zéro si le
    tableau de bord redémarre, ce qui suffit pour un réseau d'école.
    """

    def __init__(self, horloge=time.monotonic):
        self.horloge = horloge
        self.echecs: dict[str, list[float]] = {}
        self.verrou = threading.Lock()

    def _recents(self, ip: str) -> list[float]:
        limite = self.horloge() - BLOCAGE_MINUTES * 60
        self.echecs[ip] = [t for t in self.echecs.get(ip, []) if t > limite]
        return self.echecs[ip]

    def bloque(self, ip: str) -> bool:
        with self.verrou:
            return len(self._recents(ip)) >= ESSAIS_MAX

    def echec(self, ip: str) -> None:
        with self.verrou:
            self._recents(ip).append(self.horloge())

    def reussite(self, ip: str) -> None:
        with self.verrou:
            self.echecs.pop(ip, None)


def create_app(config: Config, mot_de_passe: str, horloge=time.time) -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    # Clé qui signe les cookies de connexion. Nouvelle à chaque démarrage : relancer
    # le service déconnecte tout le monde, ce qui est acceptable et plus sûr.
    app.secret_key = secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    limiteur = LimiteurConnexion()

    @app.after_request
    def entetes_securite(reponse):
        # Interdit d'afficher le tableau de bord dans un autre site (piège à clics),
        # de deviner le type des fichiers, de charger des scripts d'ailleurs, et
        # de garder en cache des pages qui contiennent des données d'élèves.
        reponse.headers["X-Frame-Options"] = "DENY"
        reponse.headers["X-Content-Type-Options"] = "nosniff"
        reponse.headers["Referrer-Policy"] = "no-referrer"
        reponse.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'")
        reponse.headers["Cache-Control"] = "no-store"
        return reponse

    @app.template_filter("heure")
    def filtre_heure(texte):
        """"07:42" -> "07h42"."""
        return texte.replace(":", "h") if texte else ""

    def jeton_csrf() -> str:
        """Jeton anti-CSRF propre à la session, inséré dans chaque formulaire POST.

        Empêche un autre site de faire exécuter une action (générer un rapport,
        déconnecter) au navigateur d'un administrateur déjà connecté.
        """
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    @app.context_processor
    def contexte_commun():
        return {"nom_ecole": config.nom_ecole, "connecte": session.get("ok", False),
                "csrf_token": jeton_csrf()}

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
    def verifier_csrf():
        # Toute action (POST) doit porter le jeton de la session. Un formulaire
        # soumis depuis un autre site ne l'a pas : il est rejeté.
        if request.method == "POST":
            attendu = session.get("csrf", "")
            recu = request.form.get("csrf", "")
            if not attendu or not hmac.compare_digest(attendu, recu):
                abort(400)
        return None

    @app.before_request
    def exiger_connexion():
        if request.endpoint in ("connexion", "static"):
            return None
        if not session.get("ok"):
            return redirect(url_for("connexion"))
        # Session abandonnée sur un PC partagé : au-delà de 30 min sans activité,
        # quelqu'un d'autre est peut-être devant l'écran.
        if horloge() - session.get("vu", 0) > INACTIVITE_MINUTES * 60:
            session.clear()
            flash("Session expirée après 30 minutes sans activité. Reconnecte-toi.")
            return redirect(url_for("connexion"))
        # L'actualisation automatique de la page (toutes les 30 s) n'est pas une
        # activité humaine : sinon une page laissée ouverte ne se fermerait jamais.
        if not request.args.get("auto"):
            session["vu"] = horloge()
        return None

    # --- Connexion ---------------------------------------------------------------

    @app.route("/connexion", methods=["GET", "POST"])
    def connexion():
        if request.method == "POST":
            saisi = request.form.get("mot_de_passe", "")
            ip = request.remote_addr or "?"
            if limiteur.bloque(ip):
                flash(f"Trop d'essais. Réessaie dans {BLOCAGE_MINUTES} minutes.")
                return render_template("connexion.html"), 429
            if hmac.compare_digest(saisi.encode(), mot_de_passe.encode()):
                limiteur.reussite(ip)
                session.clear()
                session["ok"] = True
                session["vu"] = horloge()
                return redirect(url_for("aujourdhui"))
            limiteur.echec(ip)
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
            "SELECT p.horodatage, p.type, p.en_retard, COALESCE(e.prenom, s.prenom) AS prenom, "
            "  COALESCE(e.nom, s.nom) AS nom, COALESCE(e.classe, s.fonction) AS classe "
            "FROM pointages p LEFT JOIN eleves e ON e.zk_user_id = p.zk_user_id "
            "LEFT JOIN personnel s ON s.zk_user_id = p.zk_user_id "
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
            en_ligne=en_ligne, alerte_sauvegarde=backup.alerte_copie_externe(conn(), config))

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
