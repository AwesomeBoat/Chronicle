"""python -m pc.tracker <commande>

    init        ecrit la configuration de cette machine (device_id, URL, cle)
    run         lance le tracker au premier plan (Ctrl+C pour arreter)
    status      etat : en marche ?, file locale, derniere synchro
    check       verifie la configuration et la connexion a Chronicle
    sync        envoie maintenant ce qui attend (si le tracker est arrete)
    stop        arrete proprement le tracker en arriere-plan
    start       lance la tache planifiee (Windows)
    install     demarrage automatique a l'ouverture de session (Windows)
    uninstall   retire le demarrage automatique (--purge : donnees locales)
    shell-hook  install | uninstall : commandes de terminal (PowerShell/bash)
    extension   installer l'extension navigateur (Chrome, Edge, Firefox)
    resend      renvoie ce qui a ete envoye depuis N jours (base restauree)
    paths       ou sont la configuration, la file et les journaux
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path

from pc import schema
from pc.tracker import VERSION
from pc.tracker import config as configuration


def _charger(args) -> configuration.Config:
    chemin = Path(args.config) if args.config else configuration.config_path()

    try:
        cfg = configuration.load(chemin)
    except FileNotFoundError:
        print(f"Aucune configuration dans {chemin}")
        print("Commencer par : python -m pc.tracker init --device-id "
              "<nom-de-la-machine>")
        sys.exit(2)

    erreurs = cfg.validate()

    if erreurs:
        print("Configuration invalide :")
        for erreur in erreurs:
            print(f"  - {erreur}")
        sys.exit(2)

    return cfg


def _plateforme():
    from pc.tracker import platforms
    return platforms.load()


def _age(epoch: float | None) -> str:
    if not epoch:
        return "jamais"

    secondes = int(time.time() - epoch)

    if secondes < 120:
        return f"il y a {secondes} s"
    if secondes < 7200:
        return f"il y a {secondes // 60} min"
    if secondes < 172800:
        return f"il y a {secondes // 3600} h"
    return f"il y a {secondes // 86400} j"


# ============================================================ commandes

def cmd_init(args) -> int:
    chemin = Path(args.config) if args.config else configuration.config_path()

    if chemin.exists() and not args.force:
        print(f"{chemin} existe deja. --force pour le remplacer.")
        print("Attention : changer de device_id cree une nouvelle source "
              "dans Chronicle.")
        return 1

    if not schema.valid_device_id(args.device_id):
        print(f"device_id invalide : {args.device_id!r}")
        print("Minuscules, chiffres et tirets, 2 a 40 caracteres. "
              "Exemples : windows-main, omarchy-desktop")
        return 2

    cfg = configuration.Config(
        device_id=args.device_id,
        chronicle_url=args.url,
        api_key=args.api_key or "",
        timezone=args.timezone,
        browser_token=secrets.token_urlsafe(18),
        config_dir=chemin.parent)

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(configuration.render(cfg), encoding="utf-8")

    print(f"Configuration ecrite : {chemin}")
    print(f"  device_id  {cfg.device_id}")
    print(f"  Chronicle  {cfg.chronicle_url}")
    print(f"  cle d'API  {'renseignee' if cfg.api_key else 'A RENSEIGNER'}")
    print()
    print("Jeton de l'extension navigateur (a coller dans ses options) :")
    print(f"  {cfg.browser_token}")
    print()
    print("Ensuite :")
    print("  python -m pc.tracker check      verifier la connexion")
    print("  python -m pc.tracker run        essayer au premier plan")
    print("  python -m pc.tracker install    demarrage automatique")
    return 0


def cmd_run(args) -> int:
    from pc.tracker.core.runtime import Tracker, setup_logging

    cfg = _charger(args)
    plateforme = _plateforme()
    setup_logging(cfg, console=not args.quiet)

    verrou = plateforme.single_instance(cfg.device_id)

    if verrou is None:
        print(f"Un tracker tourne deja pour {cfg.device_id}. "
              "`python -m pc.tracker stop` pour l'arreter.")
        return 3

    tracker = Tracker(cfg, plateforme)
    plateforme.stop_listener(tracker)

    def interrompre(_signum, _frame):
        tracker.request_stop("tracker_stop")

    signal.signal(signal.SIGINT, interrompre)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, interrompre)

    if args.duration:
        # Pour les essais : s'arrete seul apres N secondes.
        import threading
        threading.Timer(args.duration, tracker.request_stop,
                        args=("tracker_stop",)).start()

    return tracker.run()


def cmd_status(args) -> int:
    from pc.tracker.core.outbox import Outbox

    cfg = _charger(args)
    plateforme = _plateforme()
    en_marche = plateforme.is_running(cfg.device_id)

    print(f"Chronicle PC tracker {VERSION} - device_id {cfg.device_id}")
    print(f"  en marche      {'oui' if en_marche else 'non'}")

    if sys.platform == "win32":
        from pc.tracker.platforms.windows.install import task_installed
        print(f"  demarrage auto {'oui' if task_installed() else 'non'}")

    print(f"  Chronicle      {cfg.chronicle_url}")

    if not cfg.db_path.exists():
        print("  file locale    (vide : le tracker n'a jamais tourne)")
        return 0

    boite = Outbox(cfg.db_path)
    comptes = boite.counts()
    print(f"  en attente     {comptes['pending']}")
    print(f"  envoyes        {comptes['sent']} (gardes "
          f"{cfg.keep_sent_days:g} j)")
    print(f"  refuses        {comptes['dead']}")
    print(f"  derniere synchro reussie : {_age(boite.get_state('sync:last_ok'))}")

    erreur = boite.get_state("sync:last_error")

    if erreur and (not boite.get_state("sync:last_ok")
                   or erreur["at"] > boite.get_state("sync:last_ok")):
        print(f"  derniere erreur ({_age(erreur['at'])}) : {erreur['error']}")

    battement = boite.get_state("heartbeat")

    if battement:
        print(f"  dernier battement : {battement}")

    boite.close()
    return 0


def cmd_check(args) -> int:
    from pc.tracker.core.sync import ChronicleClient

    cfg = _charger(args)
    client = ChronicleClient(cfg.chronicle_url, cfg.api_key, timeout=10)
    print(f"Configuration OK ({cfg.config_dir / 'config.toml'})")

    sante = client.health()

    if sante.statut is None:
        print(f"Chronicle injoignable a {cfg.chronicle_url} : {sante.erreur}")
        print("  Sur la machine de Chronicle : python main.py serve")
        return 1

    print(f"Chronicle repond : {json.dumps(sante.corps, ensure_ascii=False)}")

    ping = client.ping(cfg.device_id)

    if ping.statut in (401, 403):
        print("Cle d'API refusee : verifier [chronicle] api_key")
        return 1

    if not ping.ok:
        print(f"Envoi de test refuse : HTTP {ping.statut} {ping.corps}")
        return 1

    print("Cle d'API acceptee : la synchronisation fonctionnera.")
    return 0


def cmd_sync(args) -> int:
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.core.runtime import setup_logging
    from pc.tracker.core.sync import ChronicleClient, Syncer

    cfg = _charger(args)

    if _plateforme().is_running(cfg.device_id):
        print("Le tracker tourne : il synchronise tout seul "
              f"(toutes les {cfg.sync_interval_s:g} s).")
        return 0

    setup_logging(cfg)
    boite = Outbox(cfg.db_path)
    avant = boite.counts()["pending"]
    syncer = Syncer(boite, ChronicleClient(cfg.chronicle_url, cfg.api_key),
                    cfg.device_id, cfg.batch_size)
    syncer.sync_once(budget_s=120)
    apres = boite.counts()["pending"]
    boite.close()
    print(f"{avant - apres} evenement(s) envoye(s), {apres} en attente.")
    return 0 if apres == 0 else 1


def cmd_stop(args) -> int:
    cfg = _charger(args)

    if not _plateforme().signal_stop(cfg.device_id):
        print("Aucun tracker en marche.")
        return 1

    print("Arret demande. Le tracker ferme ses intervalles et ecrit sa file.")
    return 0


def cmd_start(args) -> int:
    if sys.platform != "win32":
        print("Sous Linux : systemctl --user start chronicle-pc")
        return 1

    from pc.tracker.platforms.windows.install import run_task

    ok, message = run_task()
    print(message)
    return 0 if ok else 1


def cmd_install(args) -> int:
    if sys.platform != "win32":
        print("Sous Linux : voir pc/tracker/platforms/linux/README.md")
        return 1

    from pc.tracker.platforms.windows.install import TASK_NAME, install_task

    cfg = _charger(args)
    ok, message = install_task(Path(args.config) if args.config else None)
    print(message)

    if ok:
        print(f"Tache \"{TASK_NAME}\" enregistree : le tracker demarrera a "
              f"chaque ouverture de session ({cfg.device_id}).")
        print("Le lancer maintenant : python -m pc.tracker start")

    return 0 if ok else 1


def cmd_uninstall(args) -> int:
    if sys.platform == "win32":
        from pc.tracker.platforms.windows.install import (
            uninstall_shell_hook, uninstall_task)

        cfg = None

        try:
            cfg = configuration.load(Path(args.config) if args.config
                                     else configuration.config_path())
            _plateforme().signal_stop(cfg.device_id)
        except FileNotFoundError:
            pass

        ok, message = uninstall_task()
        print(message)

        for ligne in uninstall_shell_hook():
            print(f"hook de terminal {ligne}")

    if args.purge:
        config_dir, data_dir = configuration.base_dirs()
        time.sleep(3)

        for dossier in {config_dir, data_dir}:
            if dossier.exists():
                shutil.rmtree(dossier, ignore_errors=True)
                print(f"supprime : {dossier}")

    print("Desinstalle. Les donnees deja envoyees restent dans Chronicle.")
    return 0


def cmd_shell_hook(args) -> int:
    if sys.platform != "win32":
        hook = Path(__file__).parent / "shell" / "chronicle_pc_hook.bash"
        print(f"Ajouter a ~/.bashrc :  source \"{hook}\"")
        return 0

    from pc.tracker.platforms.windows.install import (HOOK_BASH,
                                                      install_shell_hook,
                                                      uninstall_shell_hook)

    if args.action == "install":
        for ligne in install_shell_hook(bash=args.bash):
            print(ligne)
        print("Actif dans les NOUVEAUX terminaux.")
        if not args.bash:
            print(f"Git Bash : ajouter --bash, ou dans ~/.bashrc : "
                  f"source \"{str(HOOK_BASH).replace(chr(92), '/')}\"")
    else:
        for ligne in uninstall_shell_hook() or ["aucun hook installe"]:
            print(ligne)

    return 0


def cmd_resend(args) -> int:
    from pc.tracker.core.outbox import Outbox

    cfg = _charger(args)
    boite = Outbox(cfg.db_path)
    n = boite.requeue_sent(time.time() - args.days * 86400)
    boite.close()
    print(f"{n} evenement(s) remis en attente d'envoi.")
    return 0


def cmd_extension(args) -> int:
    """Ou est l'extension, comment l'installer ; copie Firefox a la demande."""
    dossier = Path(__file__).parent / "browser_extension"

    if args.firefox:
        cible = Path(args.firefox)
        cible.mkdir(parents=True, exist_ok=True)

        for nom in ("background.js", "options.html", "options.js"):
            shutil.copy2(dossier / nom, cible / nom)

        shutil.copy2(dossier / "manifest.firefox.json", cible / "manifest.json")
        print(f"Version Firefox ecrite dans {cible}")
        print("Firefox : about:debugging > Ce Firefox > Charger un module "
              "complementaire temporaire > manifest.json")
        return 0

    try:
        jeton = _charger(args).browser_token
    except SystemExit:
        jeton = "(python -m pc.tracker init d'abord)"

    print(f"Dossier de l'extension : {dossier}")
    print()
    print("Chrome / Edge :")
    print("  1. chrome://extensions  (edge://extensions)")
    print("  2. activer le Mode developpeur")
    print("  3. Charger l'extension non empaquetee -> le dossier ci-dessus")
    print("  4. Details > Options de l'extension : coller le jeton")
    print(f"     {jeton}")
    print()
    print("Firefox : python -m pc.tracker extension --firefox <dossier>")
    return 0


def cmd_paths(args) -> int:
    config_dir, data_dir = configuration.base_dirs()
    print(f"configuration  {Path(args.config) if args.config else configuration.config_path()}")
    print(f"file locale    {data_dir / 'tracker.db'}")
    print(f"journaux       {data_dir / 'logs'}")
    print(f"depot shell    {data_dir / 'spool'}")
    print(f"extension      {Path(__file__).parent / 'browser_extension'}")
    return 0


# ============================================================ analyse

def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(
        prog="python -m pc.tracker",
        description="Chronicle PC tracker - activite PC vers Chronicle.")
    parseur.add_argument("--config", help="chemin du fichier config.toml")
    sous = parseur.add_subparsers(dest="commande", required=True)

    p = sous.add_parser("init", help="configurer cette machine")
    p.add_argument("--device-id", required=True,
                   help="identifiant stable : windows-main, omarchy-desktop")
    p.add_argument("--url", default="http://127.0.0.1:8780",
                   help="adresse de l'API Chronicle")
    p.add_argument("--api-key", help="cle d'API de Chronicle")
    p.add_argument("--timezone", help="fuseau IANA (Europe/Brussels)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fonction=cmd_init)

    p = sous.add_parser("run", help="lancer au premier plan")
    p.add_argument("--quiet", action="store_true", help="journal seulement")
    p.add_argument("--duration", type=float, help="s'arreter apres N s")
    p.set_defaults(fonction=cmd_run)

    for nom, fonction, aide in (
            ("status", cmd_status, "etat du tracker"),
            ("check", cmd_check, "verifier la connexion a Chronicle"),
            ("sync", cmd_sync, "envoyer maintenant"),
            ("stop", cmd_stop, "arreter le tracker"),
            ("start", cmd_start, "lancer la tache planifiee"),
            ("install", cmd_install, "demarrage automatique"),
            ("paths", cmd_paths, "emplacements des fichiers")):
        sous.add_parser(nom, help=aide).set_defaults(fonction=fonction)

    p = sous.add_parser("uninstall", help="retirer le demarrage automatique")
    p.add_argument("--purge", action="store_true",
                   help="supprimer aussi configuration, file et journaux")
    p.set_defaults(fonction=cmd_uninstall)

    p = sous.add_parser("shell-hook", help="hook de terminal")
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--bash", action="store_true", help="aussi ~/.bashrc")
    p.set_defaults(fonction=cmd_shell_hook)

    p = sous.add_parser("extension", help="installer l'extension navigateur")
    p.add_argument("--firefox", metavar="DOSSIER",
                   help="ecrire une copie prete pour Firefox")
    p.set_defaults(fonction=cmd_extension)

    p = sous.add_parser("resend", help="renvoyer ce qui a ete envoye")
    p.add_argument("--days", type=float, default=7.0)
    p.set_defaults(fonction=cmd_resend)

    args = parseur.parse_args(argv)
    return args.fonction(args) or 0
