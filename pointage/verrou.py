"""Verrous : savoir si le service ou le tableau de bord tourne déjà sur ce PC.

Le service (`pt run`) et le tableau de bord (`pt web`) verrouillent chacun un petit
fichier dans le dossier de la base tant qu'ils tournent. C'est le système (Windows)
qui tient le verrou : si le programme s'arrête, même brutalement (plantage, coupure
de courant), le verrou disparaît avec lui. Un simple fichier « en cours » resterait,
lui, après un plantage et bloquerait tout à tort.

Utilité :
- empêcher deux services de tourner en même temps (messages envoyés en double) ;
- refuser une mise à jour pendant que le service tourne, et empêcher le service de
  redémarrer pendant qu'elle s'installe.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from .config import Config

SERVICE = "service"
WEB = "web"


class DejaEnCours(RuntimeError):
    """Le verrou est déjà tenu par un autre programme."""


def chemin_verrou(config: Config, nom: str) -> Path:
    return config.chemin("base").parent / f"{nom}.verrou"


def prendre(config: Config, nom: str) -> IO:
    """Prend le verrou, ou lève DejaEnCours. Le garder ouvert tant qu'on en a besoin."""
    chemin = chemin_verrou(config, nom)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fichier = open(chemin, "a+")
    try:
        _verrouiller(fichier)
    except OSError:
        fichier.close()
        raise DejaEnCours(nom) from None
    return fichier


def liberer(fichier: IO) -> None:
    fichier.close()  # fermer le fichier libère le verrou


def _verrouiller(fichier: IO) -> None:
    if os.name == "nt":
        import msvcrt
        fichier.seek(0)
        msvcrt.locking(fichier.fileno(), msvcrt.LK_NBLCK, 1)  # NB : échoue tout de suite si déjà pris
    else:
        import fcntl
        fcntl.flock(fichier.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
