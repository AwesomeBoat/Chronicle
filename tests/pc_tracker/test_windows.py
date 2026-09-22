"""Collecteurs Windows sur la vraie machine (sautes ailleurs)."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from pc import schema

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows uniquement")


def test_disposition_raw_input_64_bits():
    """Les decalages lus dans WM_INPUT supposent l'en-tete 64 bits."""
    from pc.tracker.platforms.windows import win32 as w
    assert ctypes.sizeof(w.RAWINPUTHEADER) == (24 if struct.calcsize("P") == 8
                                              else 16)


def test_fenetre_au_premier_plan_decrite():
    from pc.tracker.platforms.windows import win32 as w
    from pc.tracker.platforms.windows.procinfo import describe_window

    hwnd = w.user32.GetForegroundWindow()

    if not hwnd:
        pytest.skip("aucune fenetre au premier plan (session sans bureau)")

    fenetre = describe_window(hwnd)
    assert fenetre is not None and fenetre.app and fenetre.pid


def test_processus_courant():
    from pc.tracker.platforms.windows.procinfo import process

    moi = process(os.getpid())
    assert moi.exe_path and moi.exe_path.lower().endswith(("python.exe",
                                                           "pythonw.exe"))
    assert moi.creation and moi.creation > 0


def test_gpu_via_pdh():
    from pc.tracker.platforms.windows.gpu import GpuSampler

    gpu = GpuSampler()
    time.sleep(0.5)
    utilisation, vram = gpu.sample()
    gpu.close()

    if not gpu.disponible and utilisation is None:
        pytest.skip("compteurs GPU indisponibles sur cette machine")

    assert utilisation is None or 0 <= utilisation <= 100


def test_notifications_de_fichiers_decodees():
    from pc.tracker.platforms.windows.files import decode_notifications

    nom1, nom2 = "a.txt".encode("utf-16-le"), "bé.txt".encode("utf-16-le")
    premier = struct.pack("<III", 12 + len(nom1) + 2, 1, len(nom1)) + nom1 \
        + b"\x00\x00"
    second = struct.pack("<III", 0, 4, len(nom2)) + nom2
    assert decode_notifications(premier + second) == [
        ("created", "a.txt"), ("renamed_old", "bé.txt")]


def test_surveillance_de_dossier_reelle(tmp_path):
    """ReadDirectoryChangesW sur un vrai dossier : creation, renommage."""
    from pc.tracker.collectors.base import Bus, Context, PlatformHooks
    from pc.tracker.config import Config
    from pc.tracker.core.events import EventFactory
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.core.privacy import PrivacyRules
    from pc.tracker.platforms.windows.files import WindowsFileWatcher

    racine = tmp_path / "surveille"
    racine.mkdir()
    emis: list[dict] = []
    cfg = Config(device_id="windows-main", files_roots=[str(racine)],
                 data_dir=tmp_path, config_dir=tmp_path,
                 privacy=PrivacyRules(exclude_paths=[]))
    boite = Outbox(tmp_path / "t.db")
    ctx = Context(cfg, EventFactory("windows-main", emis.append), boite,
                  threading.Event(), Bus(), "sel", "windows", PlatformHooks())
    surveillant = WindowsFileWatcher(ctx)
    surveillant.start()
    time.sleep(0.3)

    (racine / "note.md").write_text("x")
    time.sleep(0.3)
    (racine / "note.md").rename(racine / "journal.md")
    time.sleep(0.5)
    surveillant.stop()
    boite.close()

    actions = [(e["payload"]["action"], e["payload"]["path"].rsplit("/", 1)[-1])
               for e in emis]
    assert ("created", "note.md") in actions
    assert ("renamed", "note.md") in actions
    renomme = next(e for e in emis if e["payload"]["action"] == "renamed")
    assert renomme["payload"]["dest_path"].endswith("journal.md")
    assert all(schema.validate_event(e) == [] for e in emis)


def test_bureau_demarre_et_s_arrete(tmp_path):
    """La boucle de messages demarre, observe, et ferme ses intervalles."""
    from pc.tracker.collectors.base import Bus, Context
    from pc.tracker.config import Config
    from pc.tracker.core.events import EventFactory
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.platforms.windows.desktop import WindowsDesktop
    from pc.tracker.platforms.windows.hooks import WindowsHooks

    class FauxTracker:
        stopped = threading.Event()

        def request_stop(self, raison):
            pass

    emis: list[dict] = []
    cfg = Config(device_id="windows-main", data_dir=tmp_path,
                 config_dir=tmp_path, focus_min_span_s=0.0)
    boite = Outbox(tmp_path / "t.db")
    ctx = Context(cfg, EventFactory("windows-main", emis.append), boite,
                  threading.Event(), Bus(), "sel", "windows", WindowsHooks())
    bureau = WindowsDesktop(ctx, FauxTracker())
    bureau.start()
    assert bureau.alive()
    time.sleep(1.5)
    bureau.stop("tracker_stop")
    boite.close()

    assert not bureau.alive()
    assert all(schema.validate_event(e) == [] for e in emis)

    focus = [e for e in emis if e["event_type"] == "app_focus"]

    if focus:
        assert focus[-1]["payload"]["end_reason"] == "tracker_stop"


def test_bureau_reprend_une_fenetre_restee_ouverte(tmp_path):
    """Plantage pendant une fenetre ouverte : fermee au dernier battement."""
    from datetime import timedelta

    from pc.tracker.collectors.base import Bus, Context
    from pc.tracker.config import Config
    from pc.tracker.core.events import EventFactory
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.core.spans import Window
    from pc.tracker.platforms.windows.desktop import WindowsDesktop
    from pc.tracker.platforms.windows.hooks import WindowsHooks

    debut = datetime.now(timezone.utc) - timedelta(minutes=30)
    battement = debut + timedelta(minutes=12)
    emis: list[dict] = []
    boite = Outbox(tmp_path / "t.db")
    boite.set_state("open:desktop", {
        "focus": {"window": Window("vscode", "Visual Studio Code", "Code.exe",
                                   pid=7, title="main.py").to_state(),
                  "start": debut.isoformat()},
        "idle_since": None, "locked_since": None})
    ctx = Context(Config(device_id="windows-main", data_dir=tmp_path,
                         config_dir=tmp_path),
                  EventFactory("windows-main", emis.append), boite,
                  threading.Event(), Bus(), "sel", "windows", WindowsHooks(),
                  previous_heartbeat=battement)

    WindowsDesktop(ctx, tracker=None)._reprendre()
    assert boite.get_state("open:desktop") is None      # consomme
    boite.close()

    (focus,) = emis
    assert focus["event_type"] == "app_focus"
    assert focus["payload"]["end_reason"] == "recovered"
    assert focus["payload"]["ended_at"] == schema.format_ts(battement)
    assert focus["payload"]["app"] == "vscode"


def test_reprise_n_ecrase_pas_un_intervalle_deja_ferme(tmp_path):
    """L'inactivite s'est fermee normalement 10 s avant le plantage : l'etat
    (en retard de 30 s) la croit ouverte. La reprise ne doit rien emettre."""
    from datetime import timedelta

    from pc.tracker.collectors.base import Bus, Context
    from pc.tracker.config import Config
    from pc.tracker.core.events import EventFactory
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.platforms.windows.desktop import WindowsDesktop
    from pc.tracker.platforms.windows.hooks import WindowsHooks

    debut = datetime.now(timezone.utc) - timedelta(minutes=30)
    boite = Outbox(tmp_path / "t.db")
    factory = EventFactory("windows-main", lambda e: boite.enqueue([e]))
    factory.interval("idle", "windows.idle", debut,
                     debut + timedelta(minutes=9),
                     {"threshold_s": 120.0, "end_reason": "input"})
    boite.set_state("open:desktop", {"focus": None, "locked_since": None,
                                     "idle_since": debut.isoformat()})
    ctx = Context(Config(device_id="windows-main", data_dir=tmp_path,
                         config_dir=tmp_path), factory, boite,
                  threading.Event(), Bus(), "sel", "windows", WindowsHooks(),
                  previous_heartbeat=debut + timedelta(minutes=5))

    WindowsDesktop(ctx, tracker=None)._reprendre()
    assert boite.counts()["pending"] == 1                 # rien de plus
    boite.close()


def test_type_de_media_tolerant():
    """Paquet winrt-Windows.Media absent : "unknown", pas une panne."""
    from pc.tracker.platforms.windows.media import _genre, player_id

    class Casse:
        @property
        def playback_type(self):
            raise AttributeError("module has no attribute MediaPlaybackType")

    class Video:
        playback_type = 2

    assert _genre(Casse()) == "unknown"
    assert _genre(Video()) == "video"
    assert player_id("Spotify.exe") == "spotify"
    assert player_id("MSEdge") == "edge"
    assert player_id("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == \
        "spotify"


def test_journal_systeme_reel():
    from datetime import timedelta
    from pc.tracker.platforms.windows.eventlog import query, to_events

    faits = query(datetime.now(timezone.utc) - timedelta(days=2))
    for type_, debut, fin, payload, _ in to_events(faits):
        assert type_ in ("system_boot", "system_shutdown", "system_sleep")
        assert fin is None or fin > debut
