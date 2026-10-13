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


def creer_pointeuse(config) -> Pointeuse:
    """Choisit la vraie pointeuse ou le simulateur selon config.toml."""
    reglages = config.pointeuse
    if reglages["mode"] == "simulee":
        return PointeuseSimulee(config.chemin("pointeuse_simulee"))
    if reglages["mode"] == "zk":
        return ZKPointeuse(
            ip=reglages["ip"],
            port=int(reglages.get("port", 4370)),
            timeout=int(reglages.get("timeout", 5)),
            mot_de_passe=int(config.secrets.get("ZK_MOT_DE_PASSE") or 0),
            forcer_udp=bool(reglages.get("forcer_udp", False)),
        )
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
        sortie = _vers(jour, 17, rng.uniform(0, 25))
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
