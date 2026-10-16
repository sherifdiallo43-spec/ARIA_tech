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


class MultiPointeuse:
    """Regroupe plusieurs pointeuses physiques en une seule interface.

    Lit chaque boîtier l'un après l'autre et fusionne les résultats.
    Si un boîtier est injoignable, les autres sont quand même lus.
    """

    def __init__(self, pointeuses: list[ZKPointeuse]):
        self.pointeuses = pointeuses

    def lire_pointages(self) -> list[PointageBrut]:
        tous = []
        for p in self.pointeuses:
            try:
                tous.extend(p.lire_pointages())
            except Exception as e:
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
        tous = []
        connectees = []
        for p in self.pointeuses:
            try:
                tous.extend(p.lire_pointages())
                connectees.append(p)
            except Exception as e:
                log.warning("Pointeuse %s injoignable (%s). Ses pointages ne seront pas effacés.", p.ip, e)
        traiter(tous)
        for p in connectees:
            try:
                with p._connexion() as conn:
                    conn.clear_attendance()
                log.info("Mémoire de %s vidée.", p.ip)
            except Exception as e:
                log.warning("Vidage impossible pour %s (%s).", p.ip, e)
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
