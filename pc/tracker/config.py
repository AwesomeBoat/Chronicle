"""Configuration du tracker : un fichier TOML par machine.

Emplacements :

    Windows   %LOCALAPPDATA%\\ChroniclePC\\config.toml   (+ tracker.db, logs\\)
    Linux     ~/.config/chronicle-pc/config.toml
              ~/.local/share/chronicle-pc/             (tracker.db, logs/)

    CHRONICLE_PC_HOME=<dossier> regroupe tout dans un seul dossier
    (tests, installation portable).

`python -m pc.tracker init --device-id windows-main ...` ecrit le fichier.
Il est ensuite edite a la main : chaque reglage y est commente.

Le DEVICE_ID EST FIGE a l'initialisation. Le changer apres coup cree une
nouvelle source dans Chronicle ; l'ancienne garde son historique.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pc import schema
from pc.tracker.core.privacy import (APPLICATIONS_EXCLUES, CHEMINS_EXCLUS,
                                     DOMAINES_EXCLUS, PrivacyRules)

IS_WINDOWS = sys.platform == "win32"

COLLECTEURS = {
    "focus": "fenetre au premier plan (app_focus)",
    "processes": "lancement et fermeture des applications",
    "idle": "inactivite (idle)",
    "input": "activite clavier/souris, comptes par minute",
    "session": "verrouillage, veille, fin de session",
    "system_events": "demarrage, arret, veille lus dans le journal systeme",
    "metrics": "CPU, RAM, GPU, disque, reseau, batterie",
    "network": "changements de connexion",
    "files": "creation / modification / suppression de fichiers",
    "git": "commits et changements de branche",
    "terminal": "commandes (hook de shell a installer)",
    "browser": "pages web (extension a installer)",
    "media": "musique et videos en cours de lecture",
    "notifications": "notifications recues (application et type)",
}

# Dossiers et fichiers ignores par le collecteur de fichiers, a toute
# profondeur. Ils bougent a chaque compilation ou installation et
# noieraient le reste.
FICHIERS_IGNORES = [
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vs",
    ".gradle", "build", "dist", ".next", ".nuxt", ".cache", ".expo",
    "target", "obj", "bin", ".terraform", "site-packages",
    "~$*", "*.tmp", "*.temp", "*.swp", "*.swo", "*~", ".~lock*",
    "*.crdownload", "*.part", "*.download", "*.lock", "*.log",
    "*.pyc", "Thumbs.db", "desktop.ini", ".DS_Store",
]


def base_dirs() -> tuple[Path, Path]:
    """(dossier de configuration, dossier de donnees)."""
    racine = os.environ.get("CHRONICLE_PC_HOME")

    if racine:
        chemin = Path(racine).expanduser()
        return chemin, chemin

    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA",
                                    Path.home() / "AppData" / "Local"))
        return local / "ChroniclePC", local / "ChroniclePC"

    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    donnees = Path(os.environ.get("XDG_DATA_HOME",
                                  Path.home() / ".local" / "share"))
    return config / "chronicle-pc", donnees / "chronicle-pc"


def config_path() -> Path:
    return base_dirs()[0] / "config.toml"


@dataclass
class Config:
    device_id: str
    chronicle_url: str = "http://127.0.0.1:8780"
    api_key: str = ""
    timezone: str | None = None

    batch_size: int = 500
    sync_interval_s: float = 60.0
    keep_sent_days: float = 7.0
    keep_dead_days: float = 30.0

    collectors: dict[str, bool] = field(default_factory=lambda: {
        nom: True for nom in COLLECTEURS})

    idle_threshold_s: float = 120.0
    focus_settle_s: float = 2.0
    focus_min_span_s: float = 1.0
    metrics_interval_s: float = 60.0
    network_interval_s: float = 30.0
    processes_interval_s: float = 10.0
    media_interval_s: float = 5.0
    notifications_interval_s: float = 15.0

    files_roots: list[str] = field(default_factory=lambda: [
        "~/Desktop", "~/Documents", "~/Downloads"])
    files_ignore: list[str] = field(default_factory=lambda: list(
        FICHIERS_IGNORES))
    files_max_per_minute: int = 120

    git_roots: list[str] = field(default_factory=lambda: [
        "~/Desktop", "~/Documents"])
    git_check_interval_s: float = 60.0

    browser_port: int = 8781
    browser_token: str = ""

    terminal_command_mode: str = "redacted"     # redacted | program_only

    privacy: PrivacyRules = field(default_factory=PrivacyRules)

    log_level: str = "INFO"

    config_dir: Path = field(default_factory=lambda: base_dirs()[0])
    data_dir: Path = field(default_factory=lambda: base_dirs()[1])

    # --------------------------------------------------------- chemins

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracker.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def spool_dir(self) -> Path:
        """Ou les hooks de shell deposent les commandes a lire."""
        return self.data_dir / "spool"

    def enabled(self, nom: str) -> bool:
        return bool(self.collectors.get(nom, False))

    def validate(self) -> list[str]:
        erreurs = []

        if not schema.valid_device_id(self.device_id):
            erreurs.append(f"device_id invalide : {self.device_id!r} "
                           "(minuscules, chiffres, tirets, 2 a 40 caracteres)")

        if not self.chronicle_url.startswith(("http://", "https://")):
            erreurs.append("chronicle.url doit commencer par http(s)://")

        if self.terminal_command_mode not in ("redacted", "program_only"):
            erreurs.append("terminal.command_mode : redacted ou program_only")

        if not 1 <= self.batch_size <= 5000:
            erreurs.append("chronicle.batch_size entre 1 et 5000")

        return erreurs


def _section(donnees: dict, nom: str) -> dict:
    valeur = donnees.get(nom, {})
    return valeur if isinstance(valeur, dict) else {}


def load(chemin: Path | None = None) -> Config:
    """Lit le fichier. Leve FileNotFoundError s'il n'existe pas."""
    chemin = chemin or config_path()

    with open(chemin, "rb") as fichier:
        d = tomllib.load(fichier)

    chronicle = _section(d, "chronicle")
    idle = _section(d, "idle")
    focus = _section(d, "focus")
    metrics = _section(d, "metrics")
    files = _section(d, "files")
    git = _section(d, "git")
    browser = _section(d, "browser")
    terminal = _section(d, "terminal")
    privacy = _section(d, "privacy")
    intervals = _section(d, "intervals")

    defaut = Config(device_id=d.get("device_id", ""))
    collecteurs = dict(defaut.collectors)
    collecteurs.update({k: bool(v) for k, v in
                        _section(d, "collectors").items()})

    return Config(
        device_id=str(d.get("device_id", "")),
        timezone=d.get("timezone") or None,
        chronicle_url=chronicle.get("url", defaut.chronicle_url),
        api_key=chronicle.get("api_key", ""),
        batch_size=int(chronicle.get("batch_size", defaut.batch_size)),
        sync_interval_s=float(chronicle.get("sync_interval_s",
                                            defaut.sync_interval_s)),
        keep_sent_days=float(chronicle.get("keep_sent_days",
                                           defaut.keep_sent_days)),
        keep_dead_days=float(chronicle.get("keep_dead_days",
                                           defaut.keep_dead_days)),
        collectors=collecteurs,
        idle_threshold_s=float(idle.get("threshold_s",
                                        defaut.idle_threshold_s)),
        focus_settle_s=float(focus.get("title_settle_s",
                                       defaut.focus_settle_s)),
        focus_min_span_s=float(focus.get("min_span_s",
                                         defaut.focus_min_span_s)),
        metrics_interval_s=float(metrics.get("interval_s",
                                             defaut.metrics_interval_s)),
        network_interval_s=float(intervals.get("network_s",
                                               defaut.network_interval_s)),
        processes_interval_s=float(intervals.get(
            "processes_s", defaut.processes_interval_s)),
        media_interval_s=float(intervals.get("media_s",
                                             defaut.media_interval_s)),
        notifications_interval_s=float(intervals.get(
            "notifications_s", defaut.notifications_interval_s)),
        files_roots=list(files.get("roots", defaut.files_roots)),
        files_ignore=list(files.get("ignore", defaut.files_ignore)),
        files_max_per_minute=int(files.get("max_per_minute",
                                           defaut.files_max_per_minute)),
        git_roots=list(git.get("roots", defaut.git_roots)),
        git_check_interval_s=float(git.get("check_interval_s",
                                           defaut.git_check_interval_s)),
        browser_port=int(browser.get("port", defaut.browser_port)),
        browser_token=str(browser.get("token", "")),
        terminal_command_mode=terminal.get("command_mode",
                                           defaut.terminal_command_mode),
        privacy=PrivacyRules(
            exclude_apps=list(privacy.get("exclude_apps",
                                          APPLICATIONS_EXCLUES)),
            redact_title_apps=list(privacy.get("redact_title_apps", [])),
            exclude_domains=list(privacy.get("exclude_domains",
                                             DOMAINES_EXCLUS)),
            exclude_paths=list(privacy.get("exclude_paths", CHEMINS_EXCLUS)),
            url_mode=browser.get("url_mode", "domain"),
            mask_mode=privacy.get("mask_mode", "mask"),
        ),
        log_level=str(d.get("log_level", "INFO")).upper(),
        config_dir=(chemin.parent),
        data_dir=base_dirs()[1] if not os.environ.get("CHRONICLE_PC_HOME")
        else chemin.parent,
    )


def _toml_str(valeur: str) -> str:
    return '"' + valeur.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(valeurs: list[str], indent: str = "    ") -> str:
    if not valeurs:
        return "[]"

    lignes = ",\n".join(indent + _toml_str(v) for v in valeurs)
    return "[\n" + lignes + ",\n]"


def render(cfg: Config) -> str:
    """Le fichier de configuration initial, commente."""
    collecteurs = "\n".join(
        f"{nom:<14} = {'true' if cfg.collectors.get(nom) else 'false'}"
        f"   # {description}"
        for nom, description in COLLECTEURS.items())

    return f"""\
# Chronicle PC tracker - configuration de cette machine.
# Documentation : pc/tracker/README.md du depot Chronicle.

# Identifiant STABLE de la machine. Ne pas le changer apres coup : un
# nouveau device_id cree une nouvelle source dans Chronicle.
device_id = {_toml_str(cfg.device_id)}

# Fuseau IANA, pour information (l'heure locale exacte est deja dans
# chaque evenement). Vide = detection automatique.
timezone = {_toml_str(cfg.timezone or "")}

log_level = "INFO"

[chronicle]
url = {_toml_str(cfg.chronicle_url)}
api_key = {_toml_str(cfg.api_key)}
batch_size = {cfg.batch_size}
sync_interval_s = {cfg.sync_interval_s:g}
# Les evenements envoyes restent quelques jours dans la file locale : si
# la base de Chronicle devait etre restauree, "resend" les renverrait.
keep_sent_days = {cfg.keep_sent_days:g}
keep_dead_days = {cfg.keep_dead_days:g}

[collectors]
{collecteurs}

[idle]
# Sans entree clavier/souris pendant ce temps, l'intervalle "idle" demarre
# (a la derniere entree, pas au franchissement du seuil).
threshold_s = {cfg.idle_threshold_s:g}

[focus]
# Un nouveau titre de fenetre doit tenir ce temps pour ouvrir un nouvel
# intervalle (evite de couper sur un titre qui clignote).
title_settle_s = {cfg.focus_settle_s:g}
# Les fenetres vues moins longtemps (Alt+Tab) ne sont pas emises.
min_span_s = {cfg.focus_min_span_s:g}

[metrics]
interval_s = {cfg.metrics_interval_s:g}

[intervals]
network_s = {cfg.network_interval_s:g}
processes_s = {cfg.processes_interval_s:g}
media_s = {cfg.media_interval_s:g}
notifications_s = {cfg.notifications_interval_s:g}

[files]
# Dossiers surveilles (recursivement). Jamais le contenu des fichiers.
roots = {_toml_list(cfg.files_roots)}
# Noms de dossiers/fichiers ignores a toute profondeur (motifs acceptes).
ignore = {_toml_list(cfg.files_ignore)}
max_per_minute = {cfg.files_max_per_minute}

[git]
# Dossiers ou chercher les depots git (profondeur 5).
roots = {_toml_list(cfg.git_roots)}
check_interval_s = {cfg.git_check_interval_s:g}

[browser]
# Port local (127.0.0.1 seulement) ou l'extension envoie l'onglet actif.
port = {cfg.browser_port}
# A recopier dans les options de l'extension.
token = {_toml_str(cfg.browser_token)}
# domain : domaine seul (recommande) | path : + chemin nettoye |
# full : + parametres, valeurs masquees sauf liste blanche
url_mode = {_toml_str(cfg.privacy.url_mode)}

[terminal]
# redacted : commande complete, secrets masques | program_only : "git", "npm"
command_mode = {_toml_str(cfg.terminal_command_mode)}

[privacy]
# mask : l'intervalle existe mais sans nom ni titre ("private")
# drop : rien n'est emis (un trou dans la chronologie)
mask_mode = {_toml_str(cfg.privacy.mask_mode)}
# Applications jamais nommees (identifiant canonique ou nom de processus).
exclude_apps = {_toml_list(cfg.privacy.exclude_apps)}
# Applications dont le titre de fenetre n'est jamais garde.
redact_title_apps = {_toml_list(cfg.privacy.redact_title_apps)}
# Domaines (et sous-domaines) jamais enregistres.
exclude_domains = {_toml_list(cfg.privacy.exclude_domains)}
# Chemins (prefixes ou motifs) jamais enregistres.
exclude_paths = {_toml_list(cfg.privacy.exclude_paths)}
"""
