# Phase 6 — le tracker Linux (Omarchy) sur le modèle commun

> Rien ici ne touche au tracker Omarchy existant. Ce document prépare la
> phase 6 : **auditer d'abord**, puis brancher ce qui existe sur le
> schéma commun (`pc/schema.py`).

## Ce qui marche déjà sous Linux

Le cœur et les collecteurs communs ne dépendent pas du système. Sur
Omarchy, `python -m pc.tracker run` fait déjà tourner, au même format que
Windows :

| Collecteur | Événements |
|---|---|
| métriques (psutil, `/sys` pour le GPU AMD, capteurs de température) | `system_metrics` |
| réseau | `network_change` |
| git (reflog) | `git_commit`, `git_checkout` |
| terminal (hook bash, déjà testé sous Git Bash) | `terminal_command` |
| extension navigateur | `browser_page` (dès que la fenêtre active est connue) |
| machine | `device_info` |

Il manque les collecteurs **de bureau**. Le module `__init__.py` le dit
au démarrage (`collector_status: unavailable`), sans planter.

## Étape 1 — auditer le tracker Omarchy (sur le poste lui-même)

À remplir avant d'écrire une ligne :

- [ ] Où est le code ? Dépôt git, langage, dépendances.
- [ ] Comment démarre-t-il ? (unité systemd, `exec-once` de Hyprland…)
- [ ] Quelles données : fenêtre active ? titre ? inactivité ? entrées ?
      veille ? média ? fichiers ?
- [ ] Quel format et quelle fréquence ? (sondage ou événements IPC)
- [ ] Où stocke-t-il ? (fichiers, SQLite, Data Lake de PhoneTracker ?)
- [ ] Envoie-t-il quelque chose ? À qui, avec quel protocole ?
- [ ] Qu'est-ce qui marche bien et doit être gardé tel quel ?
      (souvent : l'abonnement à l'IPC de Hyprland)
- [ ] Qu'est-ce qui manque ou est fragile ?
- [ ] Quel historique existe déjà, et vaut-il d'être converti puis envoyé
      à Chronicle ? (l'API accepte des instants passés)

## Étape 2 — correspondance Hyprland / Linux → schéma commun

| Événement | Source Linux proposée | Remarques |
|---|---|---|
| `app_focus` | socket IPC `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock` : `activewindowv2`, `windowtitlev2` ; détails par `hyprctl -j activewindow` (pid, `class`, titre) | nourrir `core/spans.FocusTracker` **tel quel** : même règles de titre, même seuil |
| `app_launch`, `app_process` | `hyprctl -j clients` + `/proc/<pid>/stat` (instant de création exact) | processus avec fenêtre seulement |
| `idle` | `ext-idle-notify-v1` (hypridle) ou `IdleHint` de logind | commencer à la **dernière** entrée, comme sous Windows |
| `input_activity` | evdev en lecture (groupe `input`) : compter `EV_KEY` valeur 1, jamais le code | **pas de keylogger** : le code de touche ne doit jamais être lu |
| `session_locked` | logind, signaux `Lock` / `Unlock` (D-Bus) | |
| `system_sleep` | logind `PrepareForSleep(true/false)` | |
| `system_boot`, `system_shutdown` | `journalctl --list-boots -o json` | instants exacts, même après coup |
| `media_playback` | MPRIS sur D-Bus (`playerctl metadata`) | |
| `notification` | écoute D-Bus de `org.freedesktop.Notifications.Notify` | application et type, jamais le texte |
| `file_change` | inotify (ou `watchdog`) | réutiliser `collectors/files.FileWatcherBase.notify()` |

Les noms canoniques d'applications sont partagés : `pc/apps.py` connaît
déjà `code`, `google-chrome`, `microsoft-edge`, `kitty`, `alacritty`,
`ghostty`, `foot`, `nautilus`… À compléter avec les classes Hyprland vues
pendant l'audit.

## Étape 3 — écrire `platforms/linux/`

Même découpage que `platforms/windows/` :

```
linux/
  __init__.py      collectors() renvoie les collecteurs ci-dessous
  desktop.py       IPC Hyprland + logind -> FocusTracker, IdleTracker, LockTracker
  inputs.py        evdev -> InputAggregator (comptes seulement)
  media.py         MPRIS
  notifications.py D-Bus
  files.py         inotify
  install.py       unité systemd --user
```

Démarrage automatique :

```ini
# ~/.config/systemd/user/chronicle-pc.service
[Unit]
Description=Chronicle PC tracker
After=graphical-session.target

[Service]
WorkingDirectory=%h/Chronicle
ExecStart=%h/Chronicle/.venv/bin/python -m pc.tracker run --quiet
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

## Étape 4 — valider le modèle commun

- `python -m pc.tracker init --device-id omarchy-desktop --url http://<IP du portable>:8780 --api-key <clé>`
- Sur le portable : `python main.py serve --host 0.0.0.0` (autoriser Python
  dans le pare-feu Windows, réseau privé uniquement).
- Les mêmes requêtes doivent marcher pour les deux machines :

```sql
SELECT device, jour, minutes_premier_plan, minutes_actives, changements_contexte
FROM v_pc_daily ORDER BY jour DESC, device;
```

- `python -m pytest tests/pc_tracker` doit passer sous Linux (les tests
  propres à Windows se sautent seuls).
