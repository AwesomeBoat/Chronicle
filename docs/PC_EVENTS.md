# Événements PC — référence du schéma commun (v1)

> Le contrat que **toutes** les machines respectent : Windows aujourd'hui,
> Omarchy demain. Source de vérité : [`pc/schema.py`](../pc/schema.py).
> Ce document en est la traduction ; `tests/pc_tracker/test_docs.py`
> vérifie qu'il cite chaque type et chaque champ.
> Architecture et décisions : [`PC_TRACKING.md`](PC_TRACKING.md).

## L'enveloppe

Identique à celle de `PhoneTracker/docs/EVENT-SCHEMA.md`, avec `source = "pc"`.

| Champ | Type | Rôle |
|---|---|---|
| `event_id` | UUIDv7 | identité de l'événement, générée sur la machine. Un renvoi porte le même |
| `source` | `"pc"` | la famille |
| `producer` | texte | le chemin de collecte : `windows.foreground`, `windows.eventlog`, `git.reflog`, `shell.powershell`, `browser_ext`… Seul champ, avec `device_id`, qui diffère d'un système à l'autre |
| `event_type` | texte | un des 21 types ci-dessous |
| `ts` | ISO-8601 UTC, microsecondes, `Z` | l'instant (le **début** pour un intervalle) |
| `ts_local` | ISO-8601 avec décalage | l'heure vécue, heure d'été comprise |
| `tz` | IANA ou `null` | le fuseau, pour information |
| `device_id` | `[a-z0-9-]`, 2 à 40 | la machine : `windows-main`, `omarchy-desktop`. Fixé à l'installation |
| `dedup_key` | 32 hexa | même fait vu deux fois → même clé |
| `schema_v` | `1` | version du format |
| `payload` | objet | les champs du type |

Règles communes :

- **Intervalle** : `payload.ended_at` (UTC) et `payload.duration_s`
  (`= ended_at − ts`, à 1 s près). Émis quand l'intervalle **se ferme**.
- **Inconnu = `null`**, jamais `0` : un portable sans capteur de
  température n'est pas à 0 °C.
- **Champs inconnus tolérés** : ajouter un champ ne change pas la version.
  Changer le sens d'un champ, si.
- **Horloge plausible** : `ts` entre le 1er janvier 2020 et maintenant + 2 j.
- **Unicité** : deux événements du même type n'ont jamais le même `ts` sur
  une machine (le second avance d'une microseconde). Les instants lus dans
  le système (commit git, journal Windows, création d'un processus) restent
  exacts ; un commit git reçoit des microsecondes tirées de son hash.

### `end_reason`

| Valeur | Sens |
|---|---|
| `switch` | une autre application est passée au premier plan |
| `title_change` | même application, nouveau titre tenu 2 s |
| `navigate`, `tab_switch`, `blur` | page : navigation, changement d'onglet, navigateur quitté |
| `lock`, `suspend`, `logoff`, `shutdown` | la session ou la machine s'est arrêtée |
| `unlock`, `input` | fin d'un verrou, fin d'une inactivité |
| `track_change`, `pause` | média |
| `tracker_stop` | arrêt normal du tracker |
| `recovered` | arrêt brutal : fermé au dernier battement de cœur (30 s au pire) |

## Le lot (envoi à Chronicle)

```http
POST /api/v1/events
X-API-Key: <clé de Chronicle>
Content-Type: application/json

{
  "batch_id": "01927c3e-...",          // UUIDv7, FIGÉ : un renvoi garde le même
  "device_id": "windows-main",
  "sent_at": "2026-09-22T15:35:11.599017Z",
  "events": [ ...500 au plus... ]
}
```

| Réponse | Sens | Le tracker… |
|---|---|---|
| `200` + `"replayed": false` | écrit en base, brut compris | marque le lot envoyé |
| `200` + `"replayed": true` | ce lot était déjà écrit | marque le lot envoyé |
| `200` + `"rejected": [...]` | ces événements sont invalides ; leur brut est archivé | journalise, marque envoyé |
| `401` | mauvaise clé | garde tout, réessaie plus tard |
| `413`, `422` | lot refusé tel quel | coupe le lot en deux jusqu'à isoler le fautif (→ `dead`) |
| `503`, réseau | Chronicle ou sa base indisponible | garde tout, réessaie (10 s → 30 min) |

Un lot vide est un **ping** : il vérifie l'adresse et la clé sans rien écrire.

Autres routes : `GET /health` (sans clé), `GET /api/v1/devices` (machines
connues et fraîcheur), `GET /docs` (documentation interactive de FastAPI).

## Les 21 types

Formes : **intervalle** → `episode` ; **point** → `episode` avec
`ended_at = started_at` ; **échantillon** → `observation` ; **état** →
`profile_snapshot`. Dans `episode.payload`, Chronicle ajoute `event_id`,
`producer`, `device_id`, `ts_local`, `tz`, `schema_v`.

Métriques créées par les échantillons :

| Champ de `system_metrics` | Métrique | Champ de `input_activity` | Métrique |
|---|---|---|---|
| `cpu_pct` | `pc.cpu_pct` (%) | `keys` | `pc.input.keys` |
| `ram_pct`, `ram_used_mb` | `pc.ram_pct`, `pc.ram_used_mb` | `clicks` | `pc.input.clicks` |
| `swap_pct` | `pc.swap_pct` | `scroll` | `pc.input.scroll` |
| `gpu_pct`, `vram_used_mb` | `pc.gpu_pct`, `pc.vram_used_mb` | `mouse_distance` | `pc.input.mouse_distance` |
| `cpu_temp_c` | `pc.cpu_temp_c` | `active_s` | `pc.input.active_s` |
| `disk_used_pct` | `pc.disk_used_pct` | | |
| `disk_read_bytes`, `disk_write_bytes` | `pc.disk_read_bytes`, `pc.disk_write_bytes` | | |
| `net_up_bytes`, `net_down_bytes` | `pc.net_up_bytes`, `pc.net_down_bytes` | | |
| `battery_pct` | `pc.battery_pct` | | |

`on_ac` (sur secteur) reste dans le brut : un booléen constant serait pris
pour un capteur mort par `python main.py quality`.

### `app_focus` — intervalle

Une fenetre au premier plan, de son activation a sa perte.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `app` | texte | oui |  |
| `app_name` | texte | non |  |
| `process` | texte | non |  |
| `exe_path` | texte | non |  |
| `pid` | entier | non |  |
| `end_reason` | texte | oui |  |
| `window_title` | texte | non |  |
| `window_class` | texte | non |  |
| `input_active_s` | nombre | non |  |
| `private` | booléen | non |  |

### `app_process` — intervalle

Vie d'un processus fenetre : de sa creation a sa sortie.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `app` | texte | oui |  |
| `app_name` | texte | non |  |
| `process` | texte | non |  |
| `exe_path` | texte | non |  |
| `pid` | entier | non |  |

### `browser_page` — intervalle

Un onglet actif pendant que son navigateur est au premier plan.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `end_reason` | texte | oui |  |
| `browser` | texte | oui |  |
| `domain` | texte | non |  |
| `url` | texte | non |  |
| `page_title` | texte | non |  |
| `input_active_s` | nombre | non |  |
| `private` | booléen | non |  |

### `idle` — intervalle

Aucune entree clavier/souris pendant au moins threshold_s. Commence a la derniere entree, pas au franchissement du seuil.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `end_reason` | texte | oui |  |
| `threshold_s` | nombre | oui |  |

### `session_locked` — intervalle

Session verrouillee, du verrouillage au deverrouillage.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `end_reason` | texte | oui |  |

### `system_sleep` — intervalle

Veille ou hibernation, de l'endormissement au reveil.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `state` | texte | oui | `hibernate`, `sleep`, `unknown` |
| `origin` | texte | oui | `event_log`, `live` |
| `wake_source` | texte | non |  |

### `media_playback` — intervalle

Lecture d'un media (piste, video), tant qu'il joue.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `end_reason` | texte | oui |  |
| `player` | texte | oui |  |
| `title` | texte | non |  |
| `artist` | texte | non |  |
| `album` | texte | non |  |
| `media_type` | texte | non | `image`, `music`, `unknown`, `video` |

### `terminal_command` — intervalle

Une commande de terminal, secrets rediges AVANT le stockage.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `shell` | texte | oui |  |
| `command` | texte | oui (null permis) |  |
| `redacted` | booléen | oui |  |
| `program` | texte | non |  |
| `terminal` | texte | non |  |
| `cwd` | texte | non |  |
| `exit_code` | entier | non |  |

### `tracker_run` — intervalle

Le tracker lui-meme : de son demarrage a son arret. Dit quand une absence de donnees est une absence de mesure.

Chronicle : `episode`

| champ | type | requis | valeurs |
|---|---|---|---|
| `ended_at` | texte | oui |  |
| `duration_s` | nombre | oui |  |
| `end_reason` | texte | oui |  |
| `version` | texte | oui |  |
| `platform` | texte | non |  |
| `collectors` | liste | non |  |

### `app_launch` — point

Un processus fenetre apparait. ts = sa creation, lue dans le systeme.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `app` | texte | oui |  |
| `app_name` | texte | non |  |
| `process` | texte | non |  |
| `exe_path` | texte | non |  |
| `pid` | entier | non |  |

### `system_boot` — point

Demarrage du systeme.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `origin` | texte | oui | `event_log`, `live` |

### `system_shutdown` — point

Arret du systeme, planifie ou brutal.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `kind` | texte | oui | `power_off`, `restart`, `unexpected`, `unknown` |
| `origin` | texte | oui | `event_log`, `live` |
| `initiator` | texte | non |  |

### `network_change` — point

Changement de connectivite. Le SSID est hache, jamais stocke.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `connected` | booléen | oui |  |
| `interfaces` | liste | oui |  |
| `wifi_ssid_hash` | texte | non |  |

### `file_change` — point

Creation, modification, suppression, renommage. Jamais le contenu.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `action` | texte | oui | `created`, `deleted`, `modified`, `renamed` |
| `path` | texte | oui |  |
| `is_dir` | booléen | oui (null permis) |  |
| `dest_path` | texte | non |  |
| `extension` | texte | non |  |
| `root` | texte | non |  |

### `git_commit` — point

Un commit fait sur cette machine (lu dans le reflog).

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `repo` | texte | oui |  |
| `commit` | texte | oui |  |
| `repo_path` | texte | non |  |
| `branch` | texte | non |  |
| `message` | texte | non |  |
| `files_changed` | entier | non |  |
| `insertions` | entier | non |  |
| `deletions` | entier | non |  |
| `amend` | booléen | non |  |
| `merge` | booléen | non |  |

### `git_checkout` — point

Un changement de branche.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `repo` | texte | oui |  |
| `repo_path` | texte | non |  |
| `from_ref` | texte | non |  |
| `to_ref` | texte | non |  |
| `commit` | texte | non |  |

### `notification` — point

Une notification recue. Jamais son contenu.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `app` | texte | oui |  |
| `notification_type` | texte | oui |  |
| `app_name` | texte | non |  |

### `collector_status` — point

La source s'observe elle-meme : un trou de collecte est enregistre, jamais confondu avec une absence d'activite.

Chronicle : `episode` (fin = début)

| champ | type | requis | valeurs |
|---|---|---|---|
| `collector` | texte | oui |  |
| `status` | texte | oui | `disabled`, `error`, `rate_limited`, `started`, `stopped`, `unavailable` |
| `detail` | texte | non |  |

### `system_metrics` — échantillon

Mesures systeme. Une valeur inconnue vaut null, jamais 0.

Chronicle : `observation`

| champ | type | requis | valeurs |
|---|---|---|---|
| `interval_s` | nombre | oui |  |
| `cpu_pct` | nombre | non |  |
| `ram_pct` | nombre | non |  |
| `ram_used_mb` | nombre | non |  |
| `swap_pct` | nombre | non |  |
| `gpu_pct` | nombre | non |  |
| `vram_used_mb` | nombre | non |  |
| `cpu_temp_c` | nombre | non |  |
| `disk_used_pct` | nombre | non |  |
| `disk_read_bytes` | nombre | non |  |
| `disk_write_bytes` | nombre | non |  |
| `net_up_bytes` | nombre | non |  |
| `net_down_bytes` | nombre | non |  |
| `battery_pct` | nombre | non |  |
| `on_ac` | booléen | non |  |

### `input_activity` — échantillon

Comptes d'entrees sur une minute. PAS DE KEYLOGGER : aucune touche, aucun texte, seulement des nombres.

Chronicle : `observation`

| champ | type | requis | valeurs |
|---|---|---|---|
| `interval_s` | nombre | oui |  |
| `keys` | entier | oui |  |
| `clicks` | entier | oui |  |
| `scroll` | nombre | oui |  |
| `active_s` | entier | oui |  |
| `mouse_distance` | nombre | non |  |

### `device_info` — état

La machine : materiel, systeme, version du tracker.

Chronicle : `profile_snapshot` (`pc_device`)

| champ | type | requis | valeurs |
|---|---|---|---|
| `hostname` | texte | oui |  |
| `os` | texte | oui | `linux`, `macos`, `windows` |
| `os_version` | texte | non |  |
| `os_build` | texte | non |  |
| `arch` | texte | non |  |
| `cpu_model` | texte | non |  |
| `cpu_cores` | entier | non |  |
| `ram_total_mb` | nombre | non |  |
| `gpus` | liste | non |  |
| `tracker_version` | texte | non |  |
| `python_version` | texte | non |  |
| `timezone` | texte | non |  |
