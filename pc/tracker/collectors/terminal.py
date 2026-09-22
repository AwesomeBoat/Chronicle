"""terminal_command : les commandes tapees dans PowerShell et bash.

COMMENT ELLES ARRIVENT
----------------------
Un hook de prompt (pc/tracker/shell/) ajoute une ligne par commande dans
un fichier de depot, un par shell :

    spool/ps-<pid>.jsonl      PowerShell : une ligne JSON
    spool/bash-<pid>.tsv      bash : champs separes par des tabulations,
                              \\t \\n \\r \\\\ echappes (format "b2")

Ce module lit les lignes nouvelles toutes les 5 s, REDIGE les secrets, et
emet les evenements. Le fichier d'un shell ferme est supprime une fois lu.

Le hook ecrit la commande brute sur le disque, quelques secondes, dans le
profil de l'utilisateur. C'est l'exposition que PSReadLine (et
~/.bash_history) imposent deja en permanence ; la base de Chronicle, elle,
ne voit jamais que la version redigee.

cmd.exe n'a pas de hook de prompt : ses commandes ne sont pas vues.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import psutil

from pc.tracker.collectors.base import PollingCollector
from pc.tracker.core.privacy import normalize_path, redact_secrets

COMMANDE_MAX = 2000
_PID = re.compile(r"^(?:ps|pwsh|bash|zsh)-(\d+)\.(?:jsonl|tsv)$")


def program_name(commande: str) -> str | None:
    """"& 'C:/x/python.exe' -m pip" -> "python" ; "sudo git push" -> "git"."""
    texte = commande.strip().lstrip("&.").strip()
    jetons = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", texte)

    for jeton in jetons:
        jeton = jeton.strip("\"'")

        if jeton in ("sudo", "doas", "time", "nohup", "env", "command",
                     "exec", "builtin") or "=" in jeton:
            continue

        nom = jeton.replace("\\", "/").rsplit("/", 1)[-1].lower()

        for suffixe in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
            nom = nom.removesuffix(suffixe)

        return nom[:64] or None

    return None


def _instant(valeur) -> datetime:
    if isinstance(valeur, (int, float)):
        return datetime.fromtimestamp(float(valeur), tz=timezone.utc)

    texte = str(valeur).strip()

    # PowerShell sort 7 chiffres apres la virgule : on en garde 6.
    texte = re.sub(r"(\.\d{6})\d+", r"\1", texte)
    moment = datetime.fromisoformat(texte.replace("Z", "+00:00"))

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc)


_ECHAPPEMENTS = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def _desechapper(texte: str) -> str:
    return re.sub(r"\\([\\tnr])", lambda m: _ECHAPPEMENTS[m.group(1)], texte)


def parse_line(nom_fichier: str, ligne: str) -> dict | None:
    """Une ligne de depot -> dict normalise, ou None si illisible."""
    ligne = ligne.rstrip("\r\n")

    if not ligne:
        return None

    try:
        if nom_fichier.endswith(".jsonl"):
            d = json.loads(ligne)
            return {"shell": str(d.get("shell") or "powershell"),
                    "pid": d.get("pid"),
                    "terminal": d.get("terminal"),
                    "cwd": d.get("cwd"),
                    "command": d.get("command") or "",
                    "start": _instant(d["start"]),
                    "end": _instant(d.get("end") or d["start"]),
                    "exit_code": d.get("exit_code")}

        champs = ligne.split("\t")

        if len(champs) < 8 or champs[0] not in ("b1", "b2"):
            return None

        if champs[0] == "b2":
            # Echappement natif bash (\t \n \r \\) : voir le hook.
            cwd, commande = _desechapper(champs[5]), _desechapper(champs[6])
        else:
            # b1 : premiere version du hook, en base64.
            cwd = base64.b64decode(champs[5]).decode("utf-8", "replace")
            commande = base64.b64decode(champs[6]).decode("utf-8", "replace")

        return {"shell": champs[7] or "bash",
                "pid": int(champs[4]) if champs[4].isdigit() else None,
                "terminal": champs[8] if len(champs) > 8 and champs[8]
                else None,
                "cwd": cwd,
                "command": commande,
                "start": _instant(float(champs[1].replace(",", "."))),
                "end": _instant(float(champs[2].replace(",", "."))),
                "exit_code": int(champs[3]) if champs[3].lstrip("-").isdigit()
                else None}

    except (ValueError, KeyError, TypeError, binascii.Error):
        return None


class TerminalCollector(PollingCollector):
    name = "terminal"
    interval_s = 5.0

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.spool: Path = ctx.config.spool_dir
        self.spool.mkdir(parents=True, exist_ok=True)

    def poll(self, now: datetime) -> None:
        for fichier in sorted(self.spool.iterdir()):
            if fichier.suffix in (".jsonl", ".tsv"):
                self._lire(fichier)

    def _lire(self, fichier: Path) -> None:
        cle = f"spool:{fichier.name}"
        position = self.ctx.outbox.get_state(cle, 0)

        try:
            taille = fichier.stat().st_size
        except OSError:
            return

        if taille < position:
            position = 0                          # fichier recree

        if taille > position:
            with open(fichier, "rb") as flux:
                flux.seek(position)
                brut = flux.read(taille - position)

            # Seulement les lignes COMPLETES : le shell peut etre en train
            # d'ecrire la suivante.
            fin = brut.rfind(b"\n")

            if fin >= 0:
                for ligne in brut[:fin + 1].decode("utf-8", "replace") \
                        .splitlines():
                    commande = parse_line(fichier.name, ligne)

                    if commande is not None:
                        self.emettre(commande)

                position += fin + 1
                self.ctx.outbox.set_state(cle, position)

        # Shell ferme et fichier lu en entier : on nettoie.
        trouve = _PID.match(fichier.name)

        if trouve and position >= taille \
                and not psutil.pid_exists(int(trouve.group(1))):
            try:
                fichier.unlink()
                self.ctx.outbox.delete_state(cle)
            except OSError:
                pass

    def emettre(self, c: dict) -> dict | None:
        brute = (c["command"] or "").strip()

        if not brute:
            return None

        cwd = normalize_path(c.get("cwd")) if c.get("cwd") else None
        programme = program_name(brute)
        regles = self.ctx.config.privacy

        if cwd and regles.path_excluded(cwd):
            if regles.mask_mode == "drop":
                return None
            commande, redige, cwd = None, True, None
        elif self.ctx.config.terminal_command_mode == "program_only":
            commande, redige = programme, brute != programme
        else:
            commande, redige = redact_secrets(brute[:COMMANDE_MAX])

        shell = str(c["shell"]).lower()[:16]

        return self.ctx.factory.interval(
            "terminal_command", f"shell.{shell}", c["start"], c["end"],
            {"shell": shell, "terminal": c.get("terminal"), "cwd": cwd,
             "command": commande, "program": programme,
             "exit_code": c.get("exit_code"), "redacted": redige},
            naturelle=f"{shell}|{c.get('pid')}|{c['start'].isoformat()}",
            historique=True)
