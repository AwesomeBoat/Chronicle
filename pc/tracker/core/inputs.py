"""Activite clavier/souris : des COMPTES, jamais des touches.

PAS DE KEYLOGGER. Ce module ne recoit que des notifications du type "une
touche a ete enfoncee a l'instant t". Il ne sait pas laquelle, et aucun
de ses appelants ne la lit : le collecteur Windows ne decode jamais le
champ VKey de Raw Input, le collecteur Linux ne lira jamais le code evdev.

Ce qui sort, par minute :

    keys            touches enfoncees (repetition automatique comprise)
    clicks          boutons de souris enfonces
    scroll          crans de molette (un cran = 120 unites Windows)
    mouse_distance  deplacement cumule, en unites du peripherique
    active_s        secondes contenant au moins une entree

`active_s` est la mesure qui compte : elle distingue une heure passee
devant une fenetre d'une heure passee a s'en servir.
"""

from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass


@dataclass(slots=True)
class Minute:
    keys: int = 0
    clicks: int = 0
    scroll: float = 0.0
    mouse_distance: float = 0.0
    seconds: set | None = None

    def payload(self) -> dict:
        return {"interval_s": 60, "keys": self.keys, "clicks": self.clicks,
                "scroll": round(self.scroll, 2),
                "mouse_distance": round(self.mouse_distance, 1),
                "active_s": len(self.seconds or ())}


class InputAggregator:
    """Accumule les entrees par minute et garde les secondes actives."""

    # Les secondes actives servent a calculer `input_active_s` d'une
    # fenetre ; au-dela de cette fenetre de temps, plus personne n'en a
    # besoin (une fenetre ouverte plus longtemps est rare, et sa mesure
    # serait simplement sous-estimee).
    RETENTION_S = 12 * 3600

    def __init__(self) -> None:
        self._minutes: dict[int, Minute] = {}
        # Pour chaque seconde active, l'instant EXACT de sa premiere entree.
        # Une fenetre compte les secondes dont la premiere entree tombe en
        # son sein : chaque seconde appartient a une seule fenetre, jamais a
        # deux voisines. Liste triee (et non deque) : bisect en O(log n).
        self._secondes: list[float] = []
        self._vues: set[int] = set()
        self._verrou = threading.Lock()

    # ------------------------------------------------------ evenements

    def _marquer(self, t: float) -> Minute:
        seconde = int(t)
        minute = self._minutes.get(seconde // 60)

        if minute is None:
            minute = Minute(seconds=set())
            self._minutes[seconde // 60] = minute

        minute.seconds.add(seconde)

        if seconde not in self._vues:
            self._vues.add(seconde)

            if not self._secondes or t >= self._secondes[-1]:
                self._secondes.append(t)
            else:
                # Horloge reculee (synchro NTP) : on insere a sa place.
                bisect.insort(self._secondes, t)

        return minute

    def key(self, t: float) -> None:
        with self._verrou:
            self._marquer(t).keys += 1

    def click(self, t: float) -> None:
        with self._verrou:
            self._marquer(t).clicks += 1

    def scroll(self, t: float, crans: float) -> None:
        with self._verrou:
            self._marquer(t).scroll += abs(crans)

    def move(self, t: float, distance: float) -> None:
        if distance <= 0:
            return

        with self._verrou:
            self._marquer(t).mouse_distance += distance

    # ------------------------------------------------------- lectures

    def pop_complete(self, now: float) -> list[tuple[int, Minute]]:
        """Les minutes terminees, retirees de l'accumulateur.

        Renvoie (debut de minute en secondes epoch, comptes). Une minute
        sans aucune entree n'existe pas : l'inactivite est deja decrite
        par les intervalles `idle`, inutile d'emettre 60 zeros par heure.
        """
        courante = int(now) // 60

        with self._verrou:
            finies = sorted(m for m in self._minutes if m < courante)
            return [(m * 60, self._minutes.pop(m)) for m in finies]

    def pop_all(self) -> list[tuple[int, Minute]]:
        """Tout, minute en cours comprise (arret du tracker)."""
        with self._verrou:
            tout = sorted(self._minutes.items())
            self._minutes.clear()
            return [(m * 60, minute) for m, minute in tout]

    def active_seconds(self, debut: float, fin: float) -> int:
        """Secondes actives dont la premiere entree tombe dans [debut, fin).

        Les fenetres successives se partagent les secondes sans recouvrement :
        la somme sur une journee ne compte jamais une seconde deux fois.
        """
        with self._verrou:
            gauche = bisect.bisect_left(self._secondes, debut)
            droite = bisect.bisect_left(self._secondes, fin)
            return max(0, droite - gauche)

    def last_active(self) -> float | None:
        with self._verrou:
            return self._secondes[-1] if self._secondes else None

    def prune(self, now: float) -> None:
        limite = now - self.RETENTION_S

        with self._verrou:
            coupe = bisect.bisect_left(self._secondes, limite)

            if coupe:
                del self._secondes[:coupe]
                self._vues = {s for s in self._vues if s >= limite}
