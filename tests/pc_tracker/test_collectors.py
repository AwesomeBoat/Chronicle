"""Collecteurs communs : git (vrai depot), terminal, fichiers, reseau,
applications, metriques, journal Windows (XML reel, anonymise)."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc import schema
from pc.apps import canonical_app, is_browser
from pc.tracker.collectors.base import Bus, Context, PlatformHooks
from pc.tracker.collectors.files import FileEventFilter
from pc.tracker.collectors.network import type_interface
from pc.tracker.config import Config
from pc.tracker.core.events import EventFactory
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.privacy import PrivacyRules


@pytest.fixture
def ctx(tmp_path):
    """Un contexte de collecteur complet, file jetable."""
    emis: list[dict] = []
    # Les dossiers temporaires de pytest sont sous ~/AppData, exclu par
    # defaut : sans ce reglage, git et fichiers seraient (a juste titre)
    # filtres.
    cfg = Config(device_id="windows-main", data_dir=tmp_path,
                 config_dir=tmp_path,
                 privacy=PrivacyRules(exclude_paths=[]))
    boite = Outbox(tmp_path / "tracker.db")
    contexte = Context(config=cfg,
                       factory=EventFactory("windows-main", emis.append),
                       outbox=boite, stop=threading.Event(), bus=Bus(),
                       salt="sel", platform="windows", hooks=PlatformHooks())
    contexte.emis = emis
    yield contexte
    boite.close()


# ============================================================ applications

@pytest.mark.parametrize("process, classe, attendu", [
    ("Code.exe", "Chrome_WidgetWin_1", "vscode"),
    ("code", None, "vscode"),                       # Linux, meme app
    (None, "Code", "vscode"),                       # classe Hyprland
    ("msedge.exe", None, "edge"),
    ("microsoft-edge", None, "edge"),
    ("explorer.exe", "Shell_TrayWnd", "taskbar"),
    ("explorer.exe", "CabinetWClass", "file-explorer"),
    ("SystemSettings.exe", "Windows.UI.Core.CoreWindow", "settings"),
    ("Mon Appli Maison.exe", None, "mon-appli-maison.exe")])
def test_noms_canoniques_communs_windows_linux(process, classe, attendu):
    app, _nom = canonical_app(process, classe)
    assert app == attendu.removesuffix(".exe")


def test_nom_lisible_de_repli():
    assert canonical_app("outil.exe", None, "Mon Outil") == ("outil",
                                                             "Mon Outil")
    assert is_browser("firefox") and not is_browser("vscode")


# ===================================================================== git

def _git(depot: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(depot), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def depot(tmp_path):
    racine = tmp_path / "projets" / "demo"
    racine.mkdir(parents=True)
    _git(racine, "init", "-q", "-b", "main")
    _git(racine, "config", "user.email", "test@example.com")
    _git(racine, "config", "user.name", "Test")
    (racine / "a.py").write_text("print(1)\nprint(2)\n")
    _git(racine, "add", ".")
    _git(racine, "commit", "-q", "-m", "Premier commit")
    _git(racine, "checkout", "-q", "-b", "dev")
    (racine / "b.py").write_text("x = 1\n")
    _git(racine, "add", ".")
    _git(racine, "commit", "-q", "-m",
         "Ajoute b avec token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    return racine


def test_git_reflog_commits_et_checkout(ctx, depot):
    from pc.tracker.collectors.git import GitCollector

    ctx.config.git_roots = [str(depot.parent)]
    git = GitCollector(ctx)
    git.poll(datetime.now(timezone.utc))

    commits = [e for e in ctx.emis if e["event_type"] == "git_commit"]
    checkouts = [e for e in ctx.emis if e["event_type"] == "git_checkout"]
    assert len(commits) == 2 and len(checkouts) == 1

    premier, second = (c["payload"] for c in commits)
    assert premier["branch"] == "main" and premier["insertions"] == 2
    assert second["branch"] == "dev" and second["files_changed"] == 1
    assert "ghp_" not in second["message"]            # secret redige
    assert checkouts[0]["payload"]["to_ref"] == "dev"
    assert all(schema.validate_event(e) == [] for e in ctx.emis)


def test_git_reprise_sans_doublon(ctx, depot):
    from pc.tracker.collectors.git import GitCollector

    ctx.config.git_roots = [str(depot.parent)]
    GitCollector(ctx).poll(datetime.now(timezone.utc))
    avant = len(ctx.emis)

    # Nouveau collecteur (= redemarrage du tracker) : rien de neuf.
    GitCollector(ctx).poll(datetime.now(timezone.utc))
    assert len(ctx.emis) == avant

    (depot / "c.py").write_text("y = 2\n")
    _git(depot, "add", ".")
    _git(depot, "commit", "-q", "-m", "Troisieme")
    GitCollector(ctx).poll(datetime.now(timezone.utc))
    nouveaux = ctx.emis[avant:]
    assert [e["payload"]["message"] for e in nouveaux] == ["Troisieme"]


def test_git_instant_deterministe():
    from pc.tracker.collectors.git import instant_unique

    moment = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    a = instant_unique(moment, "a" * 40)
    assert a == instant_unique(moment, "a" * 40)
    assert a != instant_unique(moment, "b" * 40)
    assert a.replace(microsecond=0) == moment


# ================================================================ terminal

def test_terminal_powershell_et_bash(ctx):
    from pc.tracker.collectors.terminal import TerminalCollector

    spool = ctx.config.spool_dir
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "ps-999999.jsonl").write_text(json.dumps({
        "v": 1, "shell": "powershell", "pid": 999999, "terminal": "vscode",
        "cwd": str(Path.home() / "Desktop"),
        "command": "git push https://user:hunter2@github.com/x",
        "start": "2026-09-22T12:00:00.1234567Z",
        "end": "2026-09-22T12:00:02.5000000Z", "exit_code": 0}) + "\n")
    # Format b2 du hook bash : \t \n \\ echappes. Virgule decimale (locale
    # francaise de $EPOCHREALTIME) acceptee.
    (spool / "bash-999998.tsv").write_text("\t".join([
        "b2", "1758542400,5", "1758542401.0", "1", "999998", "/tmp",
        "export API_TOKEN=abc123secret\\necho a\\tb", "bash", "kitty"])
        + "\n")

    TerminalCollector(ctx).poll(datetime.now(timezone.utc))
    ps, bash = sorted((e for e in ctx.emis), key=lambda e: e["producer"],
                      reverse=True)

    assert ps["payload"]["cwd"] == "~/Desktop"
    assert "hunter2" not in ps["payload"]["command"]
    assert ps["payload"]["redacted"] and ps["payload"]["program"] == "git"
    assert ps["payload"]["duration_s"] == pytest.approx(2.376544)
    assert bash["payload"]["exit_code"] == 1
    assert "abc123secret" not in bash["payload"]["command"]
    assert "\necho a\tb" in bash["payload"]["command"]     # desechappe
    assert bash["payload"]["terminal"] == "kitty"
    assert bash["payload"]["duration_s"] == pytest.approx(0.5)

    # Shells morts (pid inexistant) et fichiers lus : nettoyes.
    assert list(spool.iterdir()) == []


def test_terminal_ancien_format_base64():
    from pc.tracker.collectors.terminal import parse_line

    ligne = "\t".join(["b1", "1758542400.5", "1758542401", "0", "12",
                       base64.b64encode(b"/tmp").decode(),
                       base64.b64encode(b"ls -la").decode(), "bash"])
    assert parse_line("bash-12.tsv", ligne)["command"] == "ls -la"


def test_terminal_mode_programme_seul(ctx):
    from pc.tracker.collectors.terminal import TerminalCollector

    ctx.config.terminal_command_mode = "program_only"
    e = TerminalCollector(ctx).emettre({
        "shell": "pwsh", "pid": 1, "terminal": None, "cwd": None,
        "command": "& 'C:/Python/python.exe' -m pip install x",
        "start": datetime(2026, 9, 22, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 22, tzinfo=timezone.utc), "exit_code": 0})
    assert e["payload"]["command"] == "python"


def test_terminal_ligne_incomplete_attendue(ctx):
    """Le shell ecrit : une ligne sans retour a la ligne n'est pas lue."""
    from pc.tracker.collectors.terminal import TerminalCollector

    spool = ctx.config.spool_dir
    spool.mkdir(parents=True, exist_ok=True)
    fichier = spool / f"ps-{__import__('os').getpid()}.jsonl"
    fichier.write_text('{"shell": "powershell", "command": "ls", "start": '
                       '"2026-09-22T12:00:00Z"')
    TerminalCollector(ctx).poll(datetime.now(timezone.utc))
    assert ctx.emis == [] and fichier.exists()


# ================================================================ fichiers

def test_le_dossier_personnel_n_est_pas_une_sauvegarde_d_editeur():
    """Regression : le motif "*~" ignorait tout chemin commencant par ~."""
    filtre = FileEventFilter(["*~"], PrivacyRules(exclude_paths=[]), 100)
    assert not filtre.ignore("~/Desktop/notes.txt")
    assert not filtre.ignore("C:/Data/notes.txt")
    assert filtre.ignore("~/Desktop/notes.txt~")


def test_filtre_fichiers():
    filtre = FileEventFilter(["node_modules", ".git", "*.tmp", "~$*"],
                             PrivacyRules(), max_par_minute=3)
    assert filtre.ignore("~/Desktop/app/node_modules/x/index.js")
    assert filtre.ignore("~/Desktop/app/.git/index")
    assert filtre.ignore("~/Documents/~$rapport.docx")
    assert filtre.ignore("~/.ssh/id_rsa")                  # vie privee
    assert not filtre.ignore("~/Desktop/app/main.py")

    assert filtre.accept("modified", "~/a.py", 1000.0)
    assert not filtre.accept("modified", "~/a.py", 1001.0)  # anti-rebond
    assert filtre.accept("modified", "~/a.py", 1003.0)
    assert filtre.accept("created", "~/b.py", 1004.0)
    assert not filtre.accept("created", "~/c.py", 1005.0)   # plafond 3/min
    assert filtre.pop_dropped() == 1


def test_notify_fichier_emet_un_evenement_valide(ctx, tmp_path):
    from pc.tracker.collectors.files import FileWatcherBase

    ctx.config.files_roots = [str(tmp_path)]
    surveillant = FileWatcherBase(ctx)
    fichier = tmp_path / "rapport.PDF"
    fichier.write_text("x")
    surveillant.notify("created", str(fichier), str(tmp_path))
    surveillant.notify("modified", str(tmp_path), str(tmp_path))   # dossier
    (e,) = ctx.emis
    assert e["payload"]["extension"] == "pdf"
    assert e["payload"]["is_dir"] is False
    assert schema.validate_event(e) == []


# ================================================================== reseau

@pytest.mark.parametrize("nom, attendu", [
    ("Wi-Fi", "wifi"), ("wlan0", "wifi"), ("wlp2s0", "wifi"),
    ("Ethernet", "ethernet"), ("enp3s0", "ethernet"), ("eth0", "ethernet"),
    ("wg0", "vpn"), ("ProtonVPN", "vpn"), ("Mystere", "other")])
def test_type_interface(nom, attendu):
    assert type_interface(nom) == attendu


def test_reseau_emet_seulement_au_changement(ctx):
    from pc.tracker.collectors.network import NetworkCollector

    reseau = NetworkCollector(ctx)
    maintenant = datetime.now(timezone.utc)
    reseau.poll(maintenant)
    reseau.poll(maintenant)
    assert len(ctx.emis) == 1
    assert schema.validate_event(ctx.emis[0]) == []


# ============================================================== metriques

def test_metriques_reelles_valides(ctx):
    from pc.tracker.collectors.metrics import MetricsCollector

    metriques = MetricsCollector(ctx)
    echantillon = metriques.sample()
    assert 0 <= echantillon["ram_pct"] <= 100
    assert echantillon["cpu_temp_c"] is None      # jamais 0 invente
    metriques.poll(datetime.now(timezone.utc))
    assert schema.validate_event(ctx.emis[0]) == []


def test_device_info_valide(ctx):
    from pc.tracker.collectors.device import DeviceCollector

    DeviceCollector(ctx).poll(datetime.now(timezone.utc))
    (e,) = ctx.emis
    assert e["payload"]["os"] in ("windows", "linux")
    assert schema.validate_event(e) == []


# ======================================================== journal Windows

XML_JOURNAL = """<Events>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='User32'/><EventID Qualifiers='32768'>1074</EventID><TimeCreated SystemTime='2026-09-18T14:42:11.6670219Z'/></System><EventData><Data Name='param1'>C:\\Windows\\explorer.exe (PC)</Data><Data Name='param5'>red\u00e9marrer</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-Kernel-General'/><EventID>13</EventID><TimeCreated SystemTime='2026-09-18T14:42:30.1Z'/></System><EventData><Data Name='StopTime'>2026-09-18T14:42:30.0624733Z</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-Kernel-General'/><EventID>12</EventID><TimeCreated SystemTime='2026-09-18T14:43:04.7Z'/></System><EventData><Data Name='StartTime'>2026-09-18T14:43:04.5000000Z</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-Power-Troubleshooter'/><EventID>1</EventID><TimeCreated SystemTime='2026-09-19T16:43:51Z'/></System><EventData><Data Name='SleepTime'>2026-09-19T16:43:50.0368228Z</Data><Data Name='WakeTime'>2026-09-19T16:43:50.0368228Z</Data><Data Name='TargetState'>0</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-Power-Troubleshooter'/><EventID>1</EventID><TimeCreated SystemTime='2026-09-20T16:27:12Z'/></System><EventData><Data Name='SleepTime'>2026-09-19T16:43:50.0368228Z</Data><Data Name='WakeTime'>2026-09-20T16:27:09.4810097Z</Data><Data Name='TargetState'>5</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='EventLog'/><EventID Qualifiers='32768'>6008</EventID><TimeCreated SystemTime='2026-09-05T21:03:43.6Z'/></System><EventData><Data>22:38:59</Data><Binary>EA07090006000500160026003B008001EA07090006000500140026003B008001600900003C000000</Binary></EventData></Event>
</Events>"""


def test_journal_windows_parse():
    from pc.tracker.platforms.windows.eventlog import parse_events, to_events

    evenements = to_events(parse_events(XML_JOURNAL))
    par_type = {}

    for type_, debut, fin, payload, _cle in evenements:
        par_type.setdefault(type_, []).append((debut, fin, payload))

    (arret_brutal, arret), = [par_type["system_shutdown"]]
    assert arret_brutal[2]["kind"] == "unexpected"
    assert arret_brutal[0] == datetime(2026, 9, 5, 20, 38, 59, 384000,
                                       tzinfo=timezone.utc)
    assert arret[2] == {"kind": "restart", "origin": "event_log",
                        "initiator": "explorer.exe"}
    assert par_type["system_boot"][0][0] == datetime(
        2026, 9, 18, 14, 43, 4, 500000, tzinfo=timezone.utc)

    # L'evenement fantome (TargetState 0, duree nulle) est ecarte.
    (veille,) = par_type["system_sleep"]
    assert veille[2]["state"] == "hibernate"
    assert veille[1] - veille[0] > timedelta(hours=23)
