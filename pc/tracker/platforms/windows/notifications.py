"""notification : les notifications "toast" recues (application, instant).

Windows n'offre pas d'API d'ecoute des notifications aux programmes
classiques (UserNotificationListener exige une application empaquetee).
Il les garde en revanche dans une base SQLite locale :

    %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db

On n'y lit que trois colonnes : le type, l'instant d'arrivee et
l'application. JAMAIS la colonne Payload, qui contient le texte.

LIMITES, ASSUMEES
-----------------
- Base non documentee : son format peut changer avec une mise a jour.
  Le collecteur est donc desactive par defaut, et se declare
  "unavailable" au lieu de planter si la structure change.
- Une notification fermee est effacee de la base. Lue toutes les 15 s,
  une notification ignoree en moins de 15 s peut manquer.
- Seuls les toasts comptent : tuiles et badges sont des mises a jour
  d'affichage, pas des notifications.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from pc.apps import canonical_app
from pc.tracker.collectors.base import PollingCollector
from pc.tracker.core.clock import from_epoch
from pc.tracker.platforms.windows.win32 import filetime_to_epoch

BASE = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" \
    / "Notifications" / "wpndatabase.db"
CLE = "notifications:cursor"

REQUETE = """
SELECT n.Id, n.Type, n.ArrivalTime, h.PrimaryId
FROM Notification n JOIN NotificationHandler h ON h.RecordId = n.HandlerId
WHERE n.ArrivalTime > ? AND n.Type = 'toast'
ORDER BY n.ArrivalTime
"""


def app_from_aumid(aumid: str) -> str:
    """"MSTeams_8wekyb3d8bbwe!MSTeams" -> teams ; chemin -> nom de l'exe."""
    nom = aumid.split("!")[-1]
    nom = nom.replace("\\", "/").rsplit("/", 1)[-1]
    return canonical_app(nom)[0]


class NotificationCollector(PollingCollector):
    name = "notifications"

    def __init__(self, ctx, base: Path = BASE) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.notifications_interval_s
        self.base = base

        if not self.base.exists():
            raise FileNotFoundError(f"base de notifications absente : {base}")

    def _lire(self, depuis: int) -> list[tuple]:
        try:
            # Lecture seule, directement : la base est en WAL, un lecteur ne
            # gene pas Windows.
            connexion = sqlite3.connect(f"file:{self.base.as_posix()}?mode=ro",
                                        uri=True, timeout=5)
            try:
                return connexion.execute(REQUETE, (depuis,)).fetchall()
            finally:
                connexion.close()

        except sqlite3.OperationalError:
            # Verrouillee : on lit une copie (base + journal WAL).
            with tempfile.TemporaryDirectory() as dossier:
                for suffixe in ("", "-wal", "-shm"):
                    source = Path(str(self.base) + suffixe)

                    if source.exists():
                        shutil.copy2(source, Path(dossier) /
                                     f"wpn.db{suffixe}")

                connexion = sqlite3.connect(Path(dossier) / "wpn.db")
                try:
                    return connexion.execute(REQUETE, (depuis,)).fetchall()
                finally:
                    connexion.close()

    def poll(self, now: datetime) -> None:
        curseur = self.ctx.outbox.get_state(CLE)

        if curseur is None:
            # Premier passage : les dernieres 24 h seulement. La base garde
            # des notifications non lues vieilles de plusieurs semaines.
            limite = (now - timedelta(days=1)).timestamp()
            curseur = int(limite * 10_000_000) + 116444736000000000

        dernier = curseur

        for identifiant, genre, arrivee, aumid in self._lire(curseur):
            if not arrivee:
                continue

            instant = from_epoch(filetime_to_epoch(arrivee))
            app = app_from_aumid(aumid or "")

            if self.ctx.config.privacy.app_excluded(app, None):
                continue

            self.ctx.factory.point("notification", "windows.wpndatabase",
                                   instant, {"app": app,
                                             "notification_type": genre},
                                   naturelle=f"{identifiant}|{arrivee}",
                                   historique=True)
            dernier = max(dernier, arrivee)

        if dernier != curseur or self.ctx.outbox.get_state(CLE) is None:
            self.ctx.outbox.set_state(CLE, dernier)
