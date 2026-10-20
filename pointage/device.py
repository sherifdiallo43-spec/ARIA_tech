"""Communication avec la pointeuse.

Le reste du logiciel ne parle jamais directement à pyzk : il passe par
l'interface `Pointeuse`. On peut ainsi remplacer le vrai boîtier par un
simulateur (développement, tests, démos devant une école) sans rien changer
au reste du code.
"""
from __future__ import annotations

import json
import logging
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from .db import dt_texte, texte_dt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PointageBrut:
    """Un pointage tel que la pointeuse le donne : qui, et quand."""
    zk_user_id: str
    horodatage: datetime


@dataclass(frozen=True)
class FicheEmpreintes:
    """Une personne enregistrée sur un boîtier, avec ses empreintes, prête à être recopiée."""
    zk_user_id: str
    nom: str
    privilege: int          # 0 = utilisateur, 14 = administrateur du boîtier
    mot_de_passe: str
    groupe: str
    carte: int
    doigts: tuple[tuple[int, int, bytes], ...]  # (numéro du doigt 0-9, valide, modèle d'empreinte)


@dataclass
class CarnetEmpreintes:
    """Tout ce qu'un boîtier sait des personnes : de quoi en remplir un autre à l'identique."""
    fiches: list[FicheEmpreintes]
    taille_fiche: int  # format des fiches utilisateur (28 ou 72 octets), propre au firmware

    @property
    def nb_doigts(self) -> int:
        return sum(len(f.doigts) for f in self.fiches)

    def incompletes_dans(self, autre: CarnetEmpreintes) -> list[str]:
        """Numéros absents de `autre`, ou présents avec moins d'empreintes qu'ici."""
        doigts_autre = {f.zk_user_id: len(f.doigts) for f in autre.fiches}
        return [f.zk_user_id for f in self.fiches
                if doigts_autre.get(f.zk_user_id, -1) < len(f.doigts)]


class Pointeuse(Protocol):
    def lire_pointages(self) -> list[PointageBrut]: ...
    def synchroniser_heure(self) -> None: ...
    def lister_utilisateurs(self) -> list[tuple[str, str]]: ...
    def lire_puis_effacer(self, traiter: Callable[[list[PointageBrut]], None]) -> int: ...


class ZKPointeuse:
    """Vraie pointeuse ZKTeco, jointe en TCP/IP (port 4370) grâce à pyzk.

    On ne garde pas la connexion ouverte : on se connecte, on lit, on se
    déconnecte. Si le réseau coupe entre deux lectures, il n'y a donc aucune
    connexion "zombie" à gérer. La pointeuse continue de stocker les
    empreintes dans sa mémoire flash, et la lecture suivante les récupère.
    """

    def __init__(self, ip: str, port: int = 4370, timeout: int = 5,
                 mot_de_passe: int = 0, forcer_udp: bool = False):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.mot_de_passe = mot_de_passe
        self.forcer_udp = forcer_udp

    @contextmanager
    def _connexion(self):
        from zk import ZK  # importé ici : le simulateur et les tests n'ont pas besoin de pyzk

        zk = ZK(self.ip, port=self.port, timeout=self.timeout, password=self.mot_de_passe,
                force_udp=self.forcer_udp, ommit_ping=True)
        conn = zk.connect()
        try:
            yield conn
        finally:
            conn.disconnect()

    def lire_pointages(self) -> list[PointageBrut]:
        """Renvoie TOUTE la mémoire de la pointeuse (le tri des doublons se fait après)."""
        with self._connexion() as conn:
            enregistrements = conn.get_attendance()
        return [PointageBrut(str(a.user_id), a.timestamp.replace(microsecond=0))
                for a in enregistrements]

    def synchroniser_heure(self) -> None:
        """Met la pointeuse à l'heure du PC. Une horloge qui dérive fausse les retards."""
        with self._connexion() as conn:
            conn.set_time(datetime.now())
        log.info("Pointeuse %s mise à l'heure.", self.ip)

    def lister_utilisateurs(self) -> list[tuple[str, str]]:
        with self._connexion() as conn:
            return [(str(u.user_id), u.name) for u in conn.get_users()]

    def lire_puis_effacer(self, traiter: Callable[[list[PointageBrut]], None]) -> int:
        """Lit toute la mémoire, la confie à `traiter`, puis l'efface.

        Le boîtier est bloqué pendant l'opération (quelques secondes) : sans
        cela, un élève pourrait pointer entre la lecture et l'effacement, et
        son pointage serait effacé sans avoir été lu.
        Si `traiter` lève une exception, la mémoire n'est PAS effacée.
        """
        with self._connexion() as conn:
            conn.disable_device()
            try:
                bruts = [PointageBrut(str(a.user_id), a.timestamp.replace(microsecond=0))
                         for a in conn.get_attendance()]
                traiter(bruts)
                conn.clear_attendance()
            finally:
                conn.enable_device()  # toujours débloquer, même en cas d'erreur
        log.info("Mémoire de la pointeuse vidée (%d pointages).", len(bruts))
        return len(bruts)

    def lire_empreintes(self) -> CarnetEmpreintes:
        """Lit toutes les personnes du boîtier et leurs empreintes."""
        with self._connexion() as conn:
            utilisateurs = conn.get_users()
            modeles = conn.get_templates()
            taille_fiche = int(conn.user_packet_size)
        # Les empreintes sont rangées par numéro interne du boîtier (uid), pas par zk_user_id.
        par_uid: dict[int, list[tuple[int, int, bytes]]] = {}
        for m in modeles:
            par_uid.setdefault(m.uid, []).append((m.fid, m.valid, m.template))
        fiches = []
        for u in utilisateurs:
            nom = "" if u.name == f"NN-{u.user_id}" else u.name  # pyzk invente "NN-12" quand le nom est vide
            fiches.append(FicheEmpreintes(str(u.user_id), nom, int(u.privilege), u.password,
                                          u.group_id, int(u.card), tuple(sorted(par_uid.get(u.uid, [])))))
        return CarnetEmpreintes(fiches, taille_fiche)

    def ecrire_empreintes(self, carnet: CarnetEmpreintes,
                          progression: Callable[[int, int], None] | None = None) -> int:
        """Enregistre sur ce boîtier chaque personne du carnet, avec ses empreintes.

        Une personne déjà présente (même zk_user_id) est mise à jour. Les personnes
        du boîtier absentes du carnet ne sont pas touchées. Le boîtier est bloqué
        pendant l'écriture : personne ne peut pointer, à faire hors des heures de passage.
        """
        from zk.finger import Finger
        from zk.user import User

        with self._connexion() as conn:
            # Chaque boîtier numérote ses fiches à sa façon (uid interne). Réutiliser l'uid de
            # la source écraserait une autre personne : on garde celui du boîtier cible,
            # et les nouveaux arrivants prennent les uid libres après le plus grand.
            existants = {u.user_id: u.uid for u in conn.get_users()}
            if not existants:
                # Boîtier neuf : pyzk ne peut pas deviner le format de ses fiches (il ne le
                # déduit que des fiches existantes). Même modèle que la source = même format.
                conn.user_packet_size = carnet.taille_fiche
            self._verifier_capacite(conn, carnet, existants)
            prochain_uid = max(existants.values(), default=0) + 1

            conn.disable_device()
            try:
                for i, fiche in enumerate(carnet.fiches, 1):
                    uid = existants.get(fiche.zk_user_id)
                    if uid is None:
                        uid, prochain_uid = prochain_uid, prochain_uid + 1
                    utilisateur = User(uid, fiche.nom, fiche.privilege, fiche.mot_de_passe,
                                       fiche.groupe, fiche.zk_user_id, fiche.carte)
                    conn.save_user_template(utilisateur, [Finger(uid, fid, valide, modele)
                                                          for fid, valide, modele in fiche.doigts])
                    if progression:
                        progression(i, len(carnet.fiches))
            finally:
                conn.enable_device()  # toujours débloquer, même si l'écriture plante en route
        log.info("%d fiche(s) écrite(s) sur %s.", len(carnet.fiches), self.ip)
        return len(carnet.fiches)

    def _verifier_capacite(self, conn, carnet: CarnetEmpreintes, existants: dict) -> None:
        """Refuse AVANT d'écrire quoi que ce soit si le boîtier n'a pas la place.

        Une personne déjà présente garde sa place : seules les nouvelles comptent.
        Certains boîtiers annoncent une capacité de 0 (inconnue) : on ne vérifie pas.
        """
        conn.read_sizes()
        nouvelles = [f for f in carnet.fiches if f.zk_user_id not in existants]
        doigts_en_plus = sum(len(f.doigts) for f in nouvelles)
        if conn.users_cap and conn.users + len(nouvelles) > conn.users_cap:
            raise ValueError(f"Pointeuse {self.ip} pleine : {conn.users} personnes + {len(nouvelles)} "
                             f"nouvelles, pour {conn.users_cap} places. Rien n'a été écrit.")
        if conn.fingers_cap and conn.fingers + doigts_en_plus > conn.fingers_cap:
            raise ValueError(f"Pointeuse {self.ip} pleine : {conn.fingers} empreintes + {doigts_en_plus} "
                             f"nouvelles, pour {conn.fingers_cap} places. Rien n'a été écrit.")


class MultiPointeuse:
    """Regroupe plusieurs pointeuses physiques en une seule interface.

    Lit chaque boîtier l'un après l'autre et fusionne les résultats.
    Si un boîtier est injoignable, les autres sont quand même lus.
    """

    def __init__(self, pointeuses: list[ZKPointeuse]):
        self.pointeuses = pointeuses
        self.lecture_complete = False  # vrai si TOUS les boîtiers ont répondu à la dernière lecture

    def lire_pointages(self) -> list[PointageBrut]:
        tous = []
        self.lecture_complete = True
        for p in self.pointeuses:
            try:
                tous.extend(p.lire_pointages())
            except Exception as e:
                self.lecture_complete = False
                log.warning("Pointeuse %s injoignable (%s), passage à la suivante.", p.ip, e)
        return tous

    def synchroniser_heure(self) -> None:
        for p in self.pointeuses:
            try:
                p.synchroniser_heure()
            except Exception as e:
                log.warning("Mise à l'heure impossible pour %s (%s).", p.ip, e)

    def lister_utilisateurs(self) -> list[tuple[str, str]]:
        tous = []
        vus = set()
        for p in self.pointeuses:
            try:
                for zk_id, nom in p.lister_utilisateurs():
                    if zk_id not in vus:
                        tous.append((zk_id, nom))
                        vus.add(zk_id)
            except Exception as e:
                log.warning("Pointeuse %s injoignable (%s).", p.ip, e)
        return tous

    def lire_puis_effacer(self, traiter: Callable[[list[PointageBrut]], None]) -> int:
        """Comme ZKPointeuse.lire_puis_effacer, mais pour plusieurs boîtiers à la fois.

        Chaque boîtier joignable est bloqué (disable_device) AVANT la lecture et
        reste bloqué jusqu'à ce que `traiter` ait traité l'ensemble fusionné des
        pointages : sinon, un élève pourrait pointer sur un boîtier entre la
        lecture et l'effacement, et ce pointage serait perdu sans avoir été
        enregistré. Le boîtier n'est effacé que si `traiter` n'a pas levé
        d'exception, mais il est toujours débloqué, même en cas d'erreur.
        """
        from contextlib import ExitStack

        tous: list[PointageBrut] = []
        bloquees: list[tuple[ZKPointeuse, object]] = []  # (pointeuse, connexion) bloquées et lues
        with ExitStack() as pile:
            for p in self.pointeuses:
                try:
                    conn = pile.enter_context(p._connexion())
                    conn.disable_device()
                except Exception as e:
                    log.warning("Pointeuse %s injoignable (%s), passage à la suivante.", p.ip, e)
                    continue
                try:
                    bruts = [PointageBrut(str(a.user_id), a.timestamp.replace(microsecond=0))
                            for a in conn.get_attendance()]
                except Exception as e:
                    # Bloquée mais pas lue : on la débloque avant de l'abandonner,
                    # sinon elle resterait bloquée jusqu'au prochain redémarrage.
                    log.warning("Lecture de %s impossible (%s), passage à la suivante.", p.ip, e)
                    try:
                        conn.enable_device()
                    except Exception as e2:
                        log.warning("Déblocage impossible pour %s (%s).", p.ip, e2)
                    continue
                tous.extend(bruts)
                bloquees.append((p, conn))

            try:
                traiter(tous)
                for p, conn in bloquees:
                    try:
                        conn.clear_attendance()
                        log.info("Mémoire de %s vidée.", p.ip)
                    except Exception as e:
                        log.warning("Vidage impossible pour %s (%s).", p.ip, e)
            finally:
                for p, conn in bloquees:
                    try:
                        conn.enable_device()
                    except Exception as e:
                        log.warning("Déblocage impossible pour %s (%s).", p.ip, e)
        return len(tous)


class PointeuseSimulee:
    """Imite la mémoire flash d'une pointeuse : une liste de pointages qui grossit.

    Avec un chemin, la mémoire est gardée dans un fichier JSON. La commande
    `simuler` peut alors ajouter des pointages pendant que `run` (un autre
    programme) les lit, exactement comme avec le vrai boîtier.
    Sans chemin, tout reste en mémoire (utilisé par les tests).
    """

    def __init__(self, chemin: Path | None = None):
        self.chemin = Path(chemin) if chemin else None
        self._memoire: list[PointageBrut] = []
        self._charger()

    def _charger(self) -> None:
        if self.chemin and self.chemin.exists():
            donnees = json.loads(self.chemin.read_text(encoding="utf-8"))
            self._memoire = [PointageBrut(d["id"], texte_dt(d["t"])) for d in donnees]

    def _enregistrer(self) -> None:
        if self.chemin:
            self.chemin.parent.mkdir(parents=True, exist_ok=True)
            donnees = [{"id": p.zk_user_id, "t": dt_texte(p.horodatage)} for p in self._memoire]
            self.chemin.write_text(json.dumps(donnees), encoding="utf-8")

    def ajouter(self, pointages: list[PointageBrut]) -> None:
        self._charger()
        self._memoire.extend(pointages)
        self._enregistrer()

    def pointer(self, zk_user_id: str, horodatage: datetime) -> None:
        self.ajouter([PointageBrut(zk_user_id, horodatage.replace(microsecond=0))])

    def lire_pointages(self) -> list[PointageBrut]:
        self._charger()
        return list(self._memoire)

    def synchroniser_heure(self) -> None:
        log.info("Pointeuse simulée : rien à mettre à l'heure.")

    def lister_utilisateurs(self) -> list[tuple[str, str]]:
        return [(zk_id, "") for zk_id in sorted({p.zk_user_id for p in self.lire_pointages()})]

    def lire_puis_effacer(self, traiter: Callable[[list[PointageBrut]], None]) -> int:
        bruts = self.lire_pointages()
        traiter(bruts)  # en cas d'exception, on s'arrête ici : rien n'est effacé
        self._memoire = []
        self._enregistrer()
        return len(bruts)


def _creer_zk(reglages: dict, secrets: dict) -> ZKPointeuse:
    return ZKPointeuse(
        ip=reglages["ip"],
        port=int(reglages.get("port", 4370)),
        timeout=int(reglages.get("timeout", 5)),
        mot_de_passe=int(secrets.get("ZK_MOT_DE_PASSE") or 0),
        forcer_udp=bool(reglages.get("forcer_udp", False)),
    )


def lister_pointeuses_zk(config) -> list[tuple[str, ZKPointeuse]]:
    """Chaque vrai boîtier déclaré dans config.toml, avec son nom (son IP à défaut)."""
    if config.pointeuse["mode"] != "zk":
        return []
    reglages = getattr(config, "pointeuses", None) or [config.pointeuse]
    return [(r.get("nom") or r["ip"], _creer_zk(r, config.secrets)) for r in reglages]


def creer_pointeuse(config) -> Pointeuse:
    """Choisit la vraie pointeuse ou le simulateur selon config.toml.

    Supporte une seule pointeuse ([pointeuse]) ou plusieurs ([[pointeuses]]).
    """
    reglages = config.pointeuse
    if reglages["mode"] == "simulee":
        return PointeuseSimulee(config.chemin("pointeuse_simulee"))
    if reglages["mode"] == "zk":
        liste = getattr(config, "pointeuses", None)
        if liste and len(liste) > 1:
            return MultiPointeuse([_creer_zk(p, config.secrets) for p in liste])
        return _creer_zk(reglages, config.secrets)
    raise ValueError(f"Mode de pointeuse inconnu : {reglages['mode']!r} (attendu : 'zk' ou 'simulee')")


# --- Génération de pointages réalistes pour le simulateur ---------------------

def _vers(jour: date, heure: int, minute: float) -> datetime:
    return datetime(jour.year, jour.month, jour.day, heure) + timedelta(minutes=minute)


def generer_journee(zk_ids: list[str], jour: date, rng: random.Random) -> list[PointageBrut]:
    """Fabrique une journée d'école à deux pointages : ~5 % d'absents, ~15 % de
    retards, quelques oublis de pointage et quelques doigts posés deux fois."""
    pointages: list[PointageBrut] = []
    for zk_id in zk_ids:
        if rng.random() < 0.05:
            continue  # absent
        if rng.random() < 0.15:
            arrivee = _vers(jour, 8, rng.uniform(1, 40))            # en retard
        else:
            arrivee = _vers(jour, 7, rng.uniform(20, 59))
        # Le samedi, les cours s'arrêtent à midi.
        sortie = _vers(jour, 12 if jour.weekday() == 5 else 17, rng.uniform(0, 25))
        tirage = rng.random()
        if tirage < 0.04:
            moments = [arrivee]          # a oublié de pointer en sortant
        elif tirage < 0.06:
            moments = [sortie]           # a oublié de pointer en arrivant
        else:
            moments = [arrivee, sortie]
        for moment in moments:
            moment = moment.replace(microsecond=0)
            pointages.append(PointageBrut(zk_id, moment))
            if rng.random() < 0.03:  # l'élève repose son doigt 30 s plus tard
                pointages.append(PointageBrut(zk_id, moment + timedelta(seconds=30)))
    return pointages


def _heure_du_jour(jour: date, texte: str, decalage_minutes: float) -> datetime:
    heures, minutes = (int(x) for x in texte.split(":"))
    return _vers(jour, heures, minutes + decalage_minutes)


def generer_journee_personnel(emplois: list[tuple[str, list[dict]]], jour: date,
                              rng: random.Random) -> list[PointageBrut]:
    """Journée du personnel d'après l'emploi du temps. `emplois` : (zk_id, cours du jour).
    ~5 % d'absents, ~15 % de retards au premier cours, sortie puis retour quand deux
    cours sont séparés d'au moins 2 h, quelques sorties oubliées."""
    pointages: list[PointageBrut] = []
    for zk_id, cours in emplois:
        if rng.random() < 0.05:
            continue
        en_retard = rng.random() < 0.15
        moments = [_heure_du_jour(jour, cours[0]["debut"],
                                  rng.uniform(8, 30) if en_retard else -rng.uniform(2, 20))]
        for precedent, suivant in zip(cours, cours[1:]):
            trou = (_heure_du_jour(jour, suivant["debut"], 0)
                    - _heure_du_jour(jour, precedent["fin"], 0)).total_seconds() / 60
            if trou >= 120:
                moments.append(_heure_du_jour(jour, precedent["fin"], rng.uniform(0, 10)))
                moments.append(_heure_du_jour(jour, suivant["debut"], -rng.uniform(2, 15)))
        moments.append(_heure_du_jour(jour, cours[-1]["fin"], rng.uniform(0, 15)))
        if rng.random() < 0.04:
            moments = moments[:-1]  # a oublié de pointer en partant
        pointages += [PointageBrut(zk_id, m.replace(microsecond=0)) for m in moments]
    return pointages
