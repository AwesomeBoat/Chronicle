# Chronicle PC tracker

L'agent qui observe l'activité d'un PC et l'envoie à Chronicle. Une
installation par machine ; toutes parlent le même format
([`docs/PC_EVENTS.md`](../../docs/PC_EVENTS.md)) et ne diffèrent que par
leur `device_id`. Le pourquoi : [`docs/PC_TRACKING.md`](../../docs/PC_TRACKING.md).

```
collecteurs ─► événements normalisés ─► file locale (SQLite) ─► lots ─► API Chronicle
```

---

## 1. Installer (Windows)

Deux morceaux : **l'API** sur la machine de Chronicle (ce portable), et **le
tracker** sur chaque PC suivi (ce portable aussi).

### 1.1 L'API d'ingestion (machine de Chronicle, une fois)

```powershell
cd C:\Users\everv\Desktop\CodeMastery\PROJECTS\Chronicle
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py initdb       # cree les vues v_pc_* (idempotent)
.venv\Scripts\python.exe main.py serve        # essai au premier plan, Ctrl+C
```

Au premier lancement, la clé d'API est générée dans `data\api_key.txt`
(hors git). C'est elle que chaque tracker présente.

Démarrage automatique de l'API, sans fenêtre (à enregistrer soi-même — c'est
une modification du système, la règle de Chronicle). Dans PowerShell :

```powershell
$chronicle = "C:\Users\everv\Desktop\CodeMastery\PROJECTS\Chronicle"
$action = New-ScheduledTaskAction -Execute "$chronicle\.venv\Scripts\pythonw.exe" -Argument "main.py serve --ensure-db" -WorkingDirectory $chronicle
Register-ScheduledTask -TaskName "Chronicle API" -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
```

`--ensure-db` démarre le conteneur PostgreSQL s'il est arrêté (Docker Desktop
doit lui-même démarrer à l'ouverture de session). Si la base manque, l'API
répond `503` et les trackers gardent leurs événements : rien n'est perdu.

Pour accepter d'autres PC du réseau local (le poste Omarchy) :
`serve --host 0.0.0.0`, puis autoriser Python dans le pare-feu Windows, **réseau
privé seulement**.

### 1.2 Le tracker (chaque PC)

```powershell
cd C:\Users\everv\Desktop\CodeMastery\PROJECTS\Chronicle
.venv\Scripts\python.exe -m pc.tracker init --device-id windows-main --timezone Europe/Brussels --api-key (Get-Content data\api_key.txt)
.venv\Scripts\python.exe -m pc.tracker check     # adresse + cle
.venv\Scripts\python.exe -m pc.tracker run       # essai au premier plan, Ctrl+C
.venv\Scripts\python.exe -m pc.tracker install   # demarrage automatique
.venv\Scripts\python.exe -m pc.tracker start     # le lancer tout de suite
```

Sur une machine **sans** Chronicle, un venv léger suffit :

```powershell
python -m venv .venv-tracker
.venv-tracker\Scripts\python.exe -m pip install -r pc\tracker\requirements.txt
```

`install` crée la tâche planifiée « Chronicle PC Tracker » :

| Réglage | Valeur | Pourquoi |
|---|---|---|
| déclencheur | ouverture de session + 30 s | le bureau doit être prêt |
| répétition | toutes les 15 min, « ne pas lancer de 2ᵉ instance » | chien de garde : relance le tracker s'il est tombé |
| programme | `pythonw.exe -m pc.tracker run` | aucune fenêtre |
| droits | ceux de l'utilisateur, session interactive | aucun droit administrateur |

**Pourquoi pas un service Windows** : un service tourne dans la session 0,
sans bureau. Il ne verrait ni la fenêtre active, ni le clavier, ni la souris.

### 1.3 Options (facultatif)

| Quoi | Commande |
|---|---|
| commandes de terminal | `python -m pc.tracker shell-hook install` (PowerShell ; `--bash` pour Git Bash) |
| pages web | `python -m pc.tracker extension` : chargement dans Chrome/Edge + jeton |
| Firefox | `python -m pc.tracker extension --firefox <dossier>` |

---

## 2. Le `device_id`

L'identité **stable** de la machine : `windows-main`, `omarchy-desktop`.
Minuscules, chiffres, tirets, 2 à 40 caractères.

- Chaque machine devient une **source** Chronicle : `pc:windows-main`.
- Il est figé par `init`. En changer crée une nouvelle source ; l'ancienne
  garde son historique. (`init --force` pour réécrire la configuration.)
- Deux machines, deux `device_id` différents — jamais le même.

---

## 3. Configuration

Fichier : `%LOCALAPPDATA%\ChroniclePC\config.toml` (Linux :
`~/.config/chronicle-pc/config.toml`). Chaque réglage y est commenté.
`python -m pc.tracker paths` affiche tous les emplacements.

| Section | Réglages principaux | Défaut |
|---|---|---|
| racine | `device_id`, `timezone` | — |
| `[chronicle]` | `url`, `api_key`, `batch_size`, `sync_interval_s`, `keep_sent_days` | `http://127.0.0.1:8780`, 500, 60 s, 7 j |
| `[collectors]` | un interrupteur par collecteur | tous actifs |
| `[idle]` | `threshold_s` | 120 s |
| `[focus]` | `title_settle_s`, `min_span_s` | 2 s, 1 s |
| `[metrics]` | `interval_s` | 60 s |
| `[files]` | `roots`, `ignore`, `max_per_minute` | Bureau, Documents, Téléchargements ; 120/min |
| `[git]` | `roots` | Bureau, Documents |
| `[browser]` | `port`, `token`, `url_mode` | 8781, généré, `domain` |
| `[terminal]` | `command_mode` | `redacted` |
| `[privacy]` | `mask_mode`, `exclude_apps`, `redact_title_apps`, `exclude_domains`, `exclude_paths` | voir § 5 |

Une modification est prise en compte au redémarrage du tracker
(`stop` puis `start`).

---

## 4. Les collecteurs

| Collecteur | Événements | Comment (Windows) | Rythme |
|---|---|---|---|
| `focus` | `app_focus` | `SetWinEventHook` (premier plan, titre) | événements + filet 5 s |
| `processes` | `app_launch`, `app_process` | fenêtres visibles ; instants lus dans le noyau | 10 s |
| `idle` | `idle` | `GetLastInputInfo` + première entrée Raw Input | 2 s |
| `input` | `input_activity` | Raw Input : **comptes** par minute | événements |
| `session` | `session_locked` (+ pauses) | WTS, `WM_POWERBROADCAST`, `WM_ENDSESSION` | événements |
| `system_events` | `system_boot`, `system_shutdown`, `system_sleep` | journal Windows (`wevtutil`) | 10 min, 20 s après un réveil |
| `metrics` | `system_metrics` | psutil + compteurs PDH (GPU) | 60 s |
| `network` | `network_change` | psutil + type de carte + SSID haché | 30 s |
| `files` | `file_change` | `ReadDirectoryChangesW` | événements |
| `git` | `git_commit`, `git_checkout` | reflog lu directement (`stat` d'abord) | 60 s |
| `terminal` | `terminal_command` | hook de prompt → dépôt → rédaction | 5 s |
| `browser` | `browser_page` | extension → 127.0.0.1 | événements |
| `media` | `media_playback` | contrôles multimédias Windows (WinRT, optionnel) | 5 s |
| `notifications` | `notification` | base locale des notifications (lecture seule) | 15 s |
| — | `device_info`, `tracker_run`, `collector_status` | le tracker lui-même | démarrage, 6 h |

Consommation mesurée sur ce portable (Ryzen 5 4500U) : **0,1 % de CPU en
moyenne, 65 Mo de mémoire**, 15 fils — mesure faite machine peu sollicitée ;
les mouvements de souris rapides ajoutent quelques dixièmes de pour cent.

---

## 5. Vie privée

### Jamais collecté

| | Garantie |
|---|---|
| touches, texte tapé | Raw Input est compté : le code de touche n'est jamais lu (`desktop.py`, `_entree`) |
| presse-papiers, écran, webcam, micro | aucune de ces API n'est appelée |
| contenu des fichiers | chemin, extension, action — rien d'autre |
| secrets dans les commandes et commits | rédigés **avant** la file locale (`core/privacy.py`) : jetons connus, JWT, `--password`, `KEY=valeur`, `user:pass@`, chaînes à haute entropie |
| URL complètes | domaine seul par défaut ; requête et `#` jamais |
| navigation privée | l'extension ne peut pas y tourner (`"incognito": "not_allowed"`) ; la fenêtre est marquée `private`, sans titre |
| contenu des notifications | application et type seulement |
| nom du Wi-Fi | haché avec un sel propre à la machine |

### Exclusions configurables

`mask_mode = "mask"` (défaut) : l'intervalle existe, marqué `private`, sans
nom ni titre — la chronologie ne montre pas de trou trompeur.
`mask_mode = "drop"` : rien n'est émis.

Exclus par défaut : gestionnaires de mots de passe (KeePass, Bitwarden,
1Password…), banques belges et services de paiement, pages de connexion
Google/Microsoft, `~/.ssh`, `~/AppData`, fichiers `*.kdbx`, `*.pem`, `*.key`.

---

## 6. La synchronisation

1. Chaque événement est écrit dans `tracker.db` (SQLite) dès sa création.
2. Toutes les 60 s (ou dès 500 en attente), un lot part vers
   `POST /api/v1/events`.
3. Le lot est **figé** : un renvoi porte le même `batch_id` et les mêmes
   événements. Chronicle reconnaît un lot déjà écrit et ne refait rien.
4. Seule une réponse 2xx marque les événements « envoyés ».
5. Chronicle éteint, réseau coupé, Docker arrêté : les événements
   attendent. Délai entre deux essais : 10 s, 20 s, 40 s… jusqu'à 30 min.
6. Un lot refusé (`422`) est coupé en deux jusqu'à isoler l'événement
   fautif, mis de côté (`dead`) — jamais supprimé en silence.
7. Les événements envoyés restent 7 jours dans la file : si la base de
   Chronicle devait être restaurée, `python -m pc.tracker resend --days 7`
   les renvoie (sans doublon : clés naturelles de Chronicle).

Un arrêt brutal (plantage, coupure de courant) : au redémarrage, les
intervalles restés ouverts sont fermés au dernier battement de cœur (30 s),
marqués `recovered`.

---

## 7. Consulter les données : le dashboard

L'API sert aussi un dashboard, sur la machine de Chronicle :

```powershell
.venv\Scripts\python.exe main.py serve      # puis http://127.0.0.1:8780/dashboard/
```

Avec la tâche planifiée « Chronicle API » (§ 1.1), il est toujours
disponible : un favori sur `http://127.0.0.1:8780/` suffit.

| Bloc | Ce qu'il montre | Calculé à partir de |
|---|---|---|
| Chiffres clés | temps réellement actif, au premier plan, changements de contexte, inactivité, commits, veille, commandes — comparés à la période précédente | épisodes + `pc.input.*` |
| Par jour (par heure si un seul jour) | minutes au premier plan par catégorie ; le point = minutes actives | `app_focus`, `pc.input.active_s` |
| Chronologie | une journée, intervalle par intervalle (fenêtres, inactivité, verrou, veille, média, collecte) | `episode` brut |
| Applications, sites | top 12 / top 10 ; premier plan contre réellement actif | `app_focus`, `browser_page` |
| Rythme de la semaine | minutes actives par jour de semaine × heure | `pc.input.active_s` |
| Changements de contexte | les passages A → B les plus fréquents | `v_pc_context_switches` |
| Git, terminal, fichiers | commits, programmes lancés, types de fichiers modifiés | `git_commit`, `terminal_command`, `file_change` |
| Machine, réseau | CPU et RAM moyens, Mo reçus / envoyés | `pc.cpu_pct`, `pc.ram_pct`, `pc.net_*` |
| Santé de la collecte | part du temps couverte par le tracker, erreurs des collecteurs | `tracker_run`, `collector_status` |

- **Filtres** : machine, période (aujourd'hui, 7, 30, 90 jours ou dates
  libres). Ils restent dans l'adresse : un rechargement ou un favori rouvre
  la même vue.
- Chaque graphique a sa vue **Tableau** (les valeurs exactes). Cliquer une
  colonne ouvre la chronologie de ce jour ; `‹ ›` passe au jour voisin.
- Thème clair, sombre ou automatique. Rafraîchi chaque minute tant que la
  période inclut aujourd'hui.
- Le temps est **découpé** aux bornes du jour (heure locale) : une session
  de 23 h 30 à 0 h 30 compte 30 min pour chaque jour. Les vues `v_pc_*`,
  elles, rangent un intervalle au jour de son début.
- Les **catégories** (Code & IA, Navigation…) sont une décision d'analyse :
  `analytics/pc_categories.py`. Une application absente tombe dans
  « Autre » ; l'ajouter = une ligne, aucune donnée à réécrire.

Le dashboard ne montre que ce qui est **arrivé dans Chronicle** : un
tracker hors ligne garde ses événements dans sa file (§ 6), ils
apparaîtront à la synchronisation suivante.

**Accès.** Sans clé depuis cette machine seulement (adresse de bouclage et
en-tête `Host` local : une page web ne peut pas se faire passer pour
localhost). Depuis une autre machine (API lancée avec `--host 0.0.0.0`), la
page demande la clé de `data\api_key.txt` et la garde dans ce navigateur.
Routes de lecture, documentées sur `/docs` :

```
GET /api/v1/pc/meta                                machines, catégories, fuseau
GET /api/v1/pc/summary?device=all&start=AAAA-MM-JJ&end=AAAA-MM-JJ
GET /api/v1/pc/day?device=all&day=AAAA-MM-JJ       la chronologie d'un jour
```

---

## 8. Dépannage

| Symptôme | Vérifier |
|---|---|
| `status` : en attente qui grandit | `check` ; l'API tourne-t-elle (`python main.py serve`) ? Docker ? |
| `check` : Chronicle injoignable | adresse dans `[chronicle] url` ; pare-feu si autre machine |
| `check` : clé refusée | `[chronicle] api_key` = contenu de `data\api_key.txt` |
| rien dans `v_pc_*` | `python main.py initdb` (crée les vues), puis `python main.py dbstats` |
| dashboard vide | la période ? la machine choisie ? « Santé de la collecte » dit si le tracker a tourné |
| « Un tracker tourne déjà » | `python -m pc.tracker stop`, ou la tâche planifiée l'a déjà lancé |
| pas de pages web | extension chargée ? jeton collé ? `options` → « Enregistrer et tester » |
| pas de commandes | nouveaux terminaux seulement ; politique PowerShell `RemoteSigned` requise |
| pas de média | `pip install -r pc\tracker\requirements.txt` (paquets winrt) |
| erreurs d'un collecteur | journal : `%LOCALAPPDATA%\ChroniclePC\logs\tracker.log` ; en base : `episode` de `kind = 'collector_status'` |

Voir ce qui est arrivé dans Chronicle :

```sql
SELECT * FROM v_pc_daily ORDER BY jour DESC LIMIT 7;
SELECT * FROM v_pc_apps_daily WHERE jour = current_date;
SELECT * FROM v_pc_context_switches WHERE jour = current_date;
```

---

## 9. Ajouter un collecteur

1. **Le type** : s'il n'existe pas, l'ajouter dans `pc/schema.py`
   (`EVENT_TYPES` : forme, champs, description), puis sa section dans
   `docs/PC_EVENTS.md` (`test_docs.py` le vérifie).
2. **Commun ou propre au système ?** Commun (psutil, fichiers, git) →
   `pc/tracker/collectors/`. Propre au système → `platforms/<os>/`.
3. **La classe** : `PollingCollector` (méthode `poll(now)`, un intervalle)
   ou `Collector` (ses propres fils : `start`, `stop`). N'émettre que par
   `self.ctx.factory.point(...)` / `.interval(...)` : c'est là que se fait la
   validation.
4. **Instants** : `historique=True` si l'instant vient du système (il doit
   rester exact d'une lecture à l'autre), sinon laisser la fabrique garantir
   l'unicité.
5. **Vie privée** : passer tout texte libre par `redact_secrets`, tout
   chemin par `normalize_path` et `privacy.path_excluded`.
6. **Brancher** : une ligne dans `COLLECTEURS` (`config.py`) et dans la liste
   de `platforms/<os>/__init__.py` (ou `core/runtime.py` s'il est commun).
7. **Chronicle** : un intervalle ou un point est pris tel quel ; une nouvelle
   mesure numérique demande une ligne dans `pc/mapper.py`
   (`SYSTEM_METRICS`, `INPUT_METRICS`). Aucune table, aucune migration.
8. **Tester** : `python -m pytest tests/pc_tracker`.

---

## 10. Désinstaller

```powershell
python -m pc.tracker uninstall            # tache + hooks de terminal
python -m pc.tracker uninstall --purge    # + configuration, file, journaux
```

L'extension se retire depuis `chrome://extensions`. Les données déjà
envoyées restent dans Chronicle ; pour les effacer :
`DELETE FROM source WHERE code = 'pc:windows-main';` (cascade sur toutes ses
lignes).

## 11. Linux

Phase 6 : [`platforms/linux/README.md`](platforms/linux/README.md).
