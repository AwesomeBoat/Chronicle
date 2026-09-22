"""git_commit / git_checkout : ce qui est fait dans git SUR CETTE MACHINE.

LA SOURCE : LE REFLOG, PAS `git log`
------------------------------------
`git log` montre aussi les commits des autres (apres un pull) et les
commits reecrits par un rebase. Le reflog (.git/logs/HEAD) ne contient que
les gestes faits ici, dans l'ordre, horodates :

    <ancien> <nouveau> Nom <email> 1758542531 +0200<TAB>commit: Ajoute X
    <ancien> <nouveau> Nom <email> 1758542600 +0200<TAB>checkout: moving from main to dev

C'est un fichier texte : le lire ne coute rien. Un `git show` n'est lance
que pour compter les lignes d'un commit nouveau.

COUT
----
Chaque minute : un `stat` par depot. Rien d'autre tant que le fichier ne
bouge pas. La liste des depots est reconstruite toutes les 6 heures
(profondeur 5 sous les dossiers configures).

UNICITE
-------
git horodate a la seconde. Deux commits dans la meme seconde (script,
cherry-pick en serie) partageraient la cle (source, kind, started_at) de
Chronicle et le second ecraserait le premier. Les microsecondes sont donc
tirees du hash du commit : deterministe (relire le meme reflog donne le
meme instant), unique en pratique, et la seconde reste exacte.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path

from pc.tracker.collectors.base import PollingCollector
from pc.tracker.core.privacy import normalize_path, redact_secrets

PROFONDEUR = 5
REDECOUVERTE_S = 6 * 3600
# Premier passage sur un depot : on remonte au plus 30 jours de reflog.
RATTRAPAGE = timedelta(days=30)

_LIGNE = re.compile(r"^(?P<ancien>[0-9a-f]{40,64}) (?P<nouveau>[0-9a-f]{40,64}) "
                    r"(?P<qui>.*?) (?P<ts>\d+) (?P<tz>[+-]\d{4})\t(?P<msg>.*)$")

_ACTIONS_COMMIT = ("commit", "commit (initial)", "commit (amend)",
                   "commit (merge)", "cherry-pick", "revert")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(slots=True)
class Entree:
    """Une ligne du reflog."""

    ancien: str
    nouveau: str
    moment: datetime
    action: str
    message: str


def parse_reflog(texte: str) -> list[Entree]:
    entrees = []

    for ligne in texte.splitlines():
        trouve = _LIGNE.match(ligne)

        if not trouve:
            continue

        action, _, message = trouve["msg"].partition(": ")
        entrees.append(Entree(
            ancien=trouve["ancien"], nouveau=trouve["nouveau"],
            moment=datetime.fromtimestamp(int(trouve["ts"]), tz=timezone.utc),
            action=action.strip(), message=message.strip()))

    return entrees


def instant_unique(moment: datetime, sha: str) -> datetime:
    """La seconde exacte de git + des microsecondes tirees du hash."""
    return moment.replace(microsecond=int(sha[:8], 16) % 1_000_000)


def git_dir(depot: Path) -> Path | None:
    """Le vrai dossier git : .git, ou la cible d'un fichier .git (worktree)."""
    point_git = depot / ".git"

    if point_git.is_dir():
        return point_git

    if point_git.is_file():
        try:
            contenu = point_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None

        if contenu.startswith("gitdir:"):
            cible = Path(contenu[7:].strip())
            return cible if cible.is_absolute() else (depot / cible).resolve()

    return None


def trouver_depots(racines: list[str], ignores: list[str]) -> list[Path]:
    """Les depots git sous les racines, sans descendre dans les depots."""
    trouves: list[Path] = []
    pile = [(Path(os.path.expanduser(r)), 0) for r in racines]

    while pile:
        dossier, profondeur = pile.pop()

        try:
            entrees = list(os.scandir(dossier))
        except OSError:
            continue

        if any(e.name == ".git" for e in entrees):
            trouves.append(dossier)
            continue

        if profondeur >= PROFONDEUR:
            continue

        for entree in entrees:
            try:
                if not entree.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            if entree.name.startswith(".") or \
                    any(fnmatch(entree.name, m) for m in ignores):
                continue

            pile.append((Path(entree.path), profondeur + 1))

    return sorted(set(trouves))


def branche_courante(dossier_git: Path) -> str | None:
    try:
        tete = (dossier_git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if tete.startswith("ref: refs/heads/"):
        return tete[len("ref: refs/heads/"):]

    return None                     # HEAD detachee


def stats_commit(depot: Path, sha: str) -> tuple[int, int, int] | None:
    """(fichiers, insertions, suppressions), ou None si git ne sait pas."""
    try:
        resultat = subprocess.run(
            ["git", "-C", str(depot), "show", "--numstat", "--format=",
             "--diff-merges=first-parent", "--no-renames", sha],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None

    if resultat.returncode != 0:
        return None

    fichiers = insertions = suppressions = 0

    for ligne in resultat.stdout.splitlines():
        morceaux = ligne.split("\t")

        if len(morceaux) < 3:
            continue

        fichiers += 1
        # Binaire : "-\t-\tchemin" -> fichier compte, pas les lignes.
        insertions += int(morceaux[0]) if morceaux[0].isdigit() else 0
        suppressions += int(morceaux[1]) if morceaux[1].isdigit() else 0

    return fichiers, insertions, suppressions


class GitCollector(PollingCollector):
    name = "git"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.git_check_interval_s
        self.depots: list[Path] = []
        self._decouverte = 0.0
        self._mtimes: dict[Path, float] = {}

    def _cle(self, depot: Path) -> str:
        return f"git:{normalize_path(str(depot))}"

    def poll(self, now: datetime) -> None:
        if time.monotonic() - self._decouverte > REDECOUVERTE_S \
                or not self._decouverte:
            debut = time.monotonic()
            self.depots = [d for d in trouver_depots(
                self.ctx.config.git_roots, self.ctx.config.files_ignore)
                if not self.ctx.config.privacy.path_excluded(
                    normalize_path(str(d)))]
            self._decouverte = time.monotonic()
            self.log.info("%d depot(s) git surveille(s) (%.1f s)",
                          len(self.depots), time.monotonic() - debut)

        for depot in self.depots:
            dossier_git = git_dir(depot)

            if dossier_git is None:
                continue

            reflog = dossier_git / "logs" / "HEAD"

            try:
                mtime = reflog.stat().st_mtime
            except OSError:
                continue

            if self._mtimes.get(depot) == mtime:
                continue

            self._mtimes[depot] = mtime
            self.traiter(depot, dossier_git, reflog, now)

    def traiter(self, depot: Path, dossier_git: Path, reflog: Path,
                now: datetime) -> int:
        """Emet les entrees nouvelles du reflog. Renvoie leur nombre."""
        try:
            entrees = parse_reflog(reflog.read_text(encoding="utf-8",
                                                    errors="replace"))
        except OSError:
            return 0

        curseur = self.ctx.outbox.get_state(self._cle(depot)) or {}
        dernier_ts = curseur.get("ts")
        dernier_sha = curseur.get("sha")
        plancher = now - RATTRAPAGE

        # Ou reprendre : juste apres la derniere entree traitee. Si elle a
        # disparu (reflog nettoye par git gc), a la premiere entree plus
        # recente qu'elle.
        depart = 0

        if dernier_ts is not None:
            depart = len(entrees)

            for indice, e in enumerate(entrees):
                t = e.moment.timestamp()

                if e.nouveau == dernier_sha and t == dernier_ts:
                    depart = indice + 1
                    break

                if t > dernier_ts:
                    depart = indice
                    break

        nom = depot.name
        chemin = normalize_path(str(depot))
        branche = curseur.get("branch")

        if branche is None:
            # Premier passage : la branche avant le premier checkout du
            # reflog est son "from". Sans checkout, c'est la branche
            # actuelle (le cas de la plupart des depots).
            premier = next((e for e in entrees[depart:]
                            if e.action == "checkout"), None)
            trouve = premier and re.match(r"moving from (.+) to (.+)$",
                                          premier.message)
            branche = trouve.group(1) if trouve \
                else branche_courante(dossier_git)

        emis = 0

        for entree in entrees[depart:]:
            if entree.action == "checkout":
                trouve = re.match(r"moving from (.+) to (.+)$", entree.message)
                ancienne, nouvelle = (trouve.groups() if trouve
                                      else (None, None))

                if entree.moment >= plancher and ancienne != nouvelle:
                    self.ctx.factory.point(
                        "git_checkout", "git.reflog",
                        instant_unique(entree.moment, entree.nouveau),
                        {"repo": nom, "repo_path": chemin,
                         "from_ref": ancienne, "to_ref": nouvelle,
                         "commit": entree.nouveau},
                        naturelle=f"{chemin}|{entree.nouveau}|"
                                  f"{int(entree.moment.timestamp())}",
                        historique=True)
                    emis += 1

                branche = nouvelle or branche
                continue

            if entree.action not in _ACTIONS_COMMIT or entree.moment < plancher:
                continue

            message, _ = redact_secrets(entree.message[:500])
            stats = stats_commit(depot, entree.nouveau)

            self.ctx.factory.point(
                "git_commit", "git.reflog",
                instant_unique(entree.moment, entree.nouveau),
                {"repo": nom, "repo_path": chemin, "branch": branche,
                 "commit": entree.nouveau, "message": message,
                 "files_changed": stats[0] if stats else None,
                 "insertions": stats[1] if stats else None,
                 "deletions": stats[2] if stats else None,
                 "amend": entree.action == "commit (amend)",
                 "merge": entree.action == "commit (merge)"},
                naturelle=entree.nouveau, historique=True)
            emis += 1

        if entrees:
            derniere = entrees[-1]
            self.ctx.outbox.set_state(self._cle(depot), {
                "ts": derniere.moment.timestamp(), "sha": derniere.nouveau,
                "branch": branche})

        return emis
