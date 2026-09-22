# Le PC comme source de Chronicle

> Proposition d'architecture, écrite **après** l'audit et **avant** le code.
> Le code du dossier `pc/` n'est que la traduction de ce document.
> Référence du format : [`PC_EVENTS.md`](PC_EVENTS.md).
> Installation et exploitation : [`pc/tracker/README.md`](../pc/tracker/README.md).

## 1. Le point de départ

Chronicle n'est pas un tracker d'activité PC. C'est un historien de données
personnelles, et le PC n'est qu'une source de plus, à côté de la montre, du
journal alimentaire, du capteur de chambre et de la liseuse.

La règle de Chronicle tient donc pour le PC comme pour les autres :

> **Une source de données n'est pas une table.**
> Le PC entre dans les tables existantes, rangé par la *forme temporelle*
> de chaque donnée : point, intervalle ou état.

## 2. Ce que l'audit a trouvé

### Chronicle (ce dépôt)

| Élément | État |
|---|---|
| Stockage | PostgreSQL 16 (Docker), 7 tables : `source`, `metric`, `observation`, `episode`, `profile_snapshot`, `raw_payload`, `sync_state` |
| Frontière connecteur ↔ base | `database/records.py` : un connecteur produit des `Batch`, il n'importe jamais SQLAlchemy (vérifié par `python main.py sources`) |
| Idempotence | clés naturelles + `ON CONFLICT` : `(source, metric, observed_at)`, `(source, kind, started_at)`, empreinte de contenu pour les snapshots et le brut |
| Traçabilité | `raw_payload` garde le brut, le parsing est rejouable |
| Curseurs | `sync_state` par (source, flux) |
| Sources | Polar (montre), journal alimentaire, capteur ESP32, Kindle |
| Ingestion HTTP | **aucune API** — seul le récepteur Kindle écoute, et il n'écrit que des fichiers |
| `device_id` | pas de table `device`. Existe déjà comme convention dans les payloads (`payload->>'device_id'` de la montre, lu par `v_sleep`) |

### Les collecteurs extérieurs

| Projet | Ce qu'il apporte au PC tracker |
|---|---|
| `screenTwin` (Android) | pousse vers « l'API digitaltwin » — l'ancien nom de Chronicle — **qui n'existe pas encore** |
| `PhoneTracker` + son serveur « Personal Data Lake » | l'**enveloppe d'événement commune** (`event_id` UUIDv7, `source`, `producer`, `ts` UTC, `device_id`, `dedup_key`…) et un protocole de synchro éprouvé (lots, `batch_id`, rejeu sans doublon). Son `EVENT-SCHEMA.md` prévoit déjà une source « PC Linux » |

### Le tracker Linux (Omarchy)

Il vit sur le poste Omarchy, pas sur ce portable : il n'a pas pu être audité
d'ici. La phase 6 (§ 11) commence donc par cet audit, avec une liste de
contrôle.

## 3. Deux décisions

### Décision 1 — les données du PC vont dans Chronicle

Pas dans un nouveau stockage, ni dans la base SQLite du Data Lake.
Chronicle possède déjà tout ce qu'il faut :

| Besoin du PC | Abstraction Chronicle réutilisée |
|---|---|
| une machine | une ligne `source` **par machine** : `pc:windows-main`, `pc:omarchy-desktop` |
| une mesure (CPU, clics par minute) | `observation` |
| une durée (fenêtre au premier plan, inactivité, veille) | `episode` |
| un état (matériel, OS) | `profile_snapshot` |
| le brut reçu | `raw_payload` |
| où en est chaque machine | `sync_state` |

Pourquoi **une source par machine** : la clé d'unicité d'`observation` est
`(source, metric, observed_at)`. Deux PC qui mesurent `pc.cpu_pct` à la même
seconde entreraient en collision dans une source unique. Avec une source par
machine, ils ne se touchent jamais — et supprimer toutes les données d'une
machine reste un seul `DELETE` (`ON DELETE CASCADE`).

**Aucune table n'est ajoutée. `database/models.py` et
`database/repository.py` ne changent pas.** Le critère de la V4 tient une
cinquième fois.

### Décision 2 — le PC parle l'enveloppe commune

Le tracker n'invente pas de format. Il émet l'enveloppe de
`PhoneTracker/docs/EVENT-SCHEMA.md`, avec `source = "pc"`. Deux conséquences :

- Chronicle gagne **l'API d'ingestion qui lui manquait** (`POST /api/v1/events`),
  avec le même protocole que le Data Lake. Le PC en est le premier client ;
  screenTwin et PhoneTracker pourront la viser plus tard sans nouveau format.
- Le tracker reste compatible avec le Data Lake : pointer son URL dessus
  suffirait. Rien n'est fermé.

## 4. Architecture

```
  ┌──────────────── chaque PC (Windows, Omarchy…) ────────────────┐
  │                                                               │
  │  collecteurs de la plateforme        collecteurs communs      │
  │  (fenêtre active, entrées, veille…)  (git, terminal, réseau,  │
  │              │                         navigateur, métriques) │
  │              └───────────────┬──────────────┘                 │
  │                              ▼                                │
  │          normalisation : schéma PC commun (pc/schema.py)      │
  │          vie privée : URL, secrets, listes de blocage         │
  │                              ▼                                │
  │          file locale SQLite (outbox) — survit au reboot       │
  │                              ▼                                │
  │          lots de 500, retry + backoff, rejouables             │
  └──────────────────────────────┬────────────────────────────────┘
                                 │ HTTP, X-API-Key
                                 ▼
  ┌──────────────────────────── Chronicle ────────────────────────┐
  │  api/ : POST /api/v1/events                                   │
  │    1. valide l'enveloppe et chaque payload (pc/schema.py)     │
  │    2. archive le lot dans raw_payload (brut, rejouable)       │
  │    3. pc/mapper.py : événements → Batch                       │
  │    4. store_batch (repository existant, ON CONFLICT)          │
  │    5. sync_state : curseur par machine                        │
  │                                                               │
  │  observation · episode · profile_snapshot   ← les données     │
  │  v_pc_daily · v_pc_apps_daily · v_pc_context_switches …       │
  │                                              ← les analyses   │
  └───────────────────────────────────────────────────────────────┘
       ▲            ▲             ▲              ▲
     Polar     alimentation    capteur        Kindle      (inchangés)
```

### Le code

```
pc/
  schema.py         LE contrat : enveloppe, types d'événements, validation
  apps.py           noms canoniques des applications (Code.exe ≡ code → vscode)
  mapper.py         connecteur Chronicle : événements → Batch
  tracker/          l'agent qui tourne sur chaque PC
    core/           file, synchro, vie privée, machines à états (communs)
    collectors/     collecteurs communs (psutil, git, terminal, navigateur)
    platforms/
      windows/      collecteurs Windows (ctypes, sans dépendance compilée)
      linux/        phase 6
    shell/          hooks PowerShell / bash
    browser_extension/  extension Chrome / Edge / Firefox
api/                API d'ingestion de Chronicle (FastAPI)
```

`pc/tracker/` n'importe **jamais** `database`, `config` ni SQLAlchemy : il
tourne sur des machines où Chronicle n'est pas installé.

## 5. Le modèle commun des événements PC

L'enveloppe est la même pour tous les événements, sur toutes les machines :

```json
{
  "event_id":   "01927c3e-5b6a-7c1d-9e2f-3a4b5c6d7e8f",
  "source":     "pc",
  "producer":   "windows.foreground",
  "event_type": "app_focus",
  "ts":         "2026-09-22T12:02:11.123456Z",
  "ts_local":   "2026-09-22T14:02:11.123456+02:00",
  "tz":         "Europe/Brussels",
  "device_id":  "windows-main",
  "dedup_key":  "8b1f0c…",
  "schema_v":   1,
  "payload": {
    "ended_at":       "2026-09-22T12:07:23.456789Z",
    "duration_s":     312.333,
    "app":            "vscode",
    "app_name":       "Visual Studio Code",
    "process":        "Code.exe",
    "window_title":   "chronicles.py - Chronicle - Visual Studio Code",
    "input_active_s": 187,
    "end_reason":     "switch"
  }
}
```

Ce qui **change** entre Windows et Linux : `device_id` (la machine) et
`producer` (le chemin de collecte, utile pour la qualité). Ce qui **ne change
pas** : `event_type` et les champs du `payload`. Il n'y a ni `WindowsData` ni
`LinuxData`.

Les 21 types, par forme :

| Forme | Types |
|---|---|
| **intervalle** (`ts` = début, `payload.ended_at` = fin) | `app_focus`, `app_process`, `browser_page`, `idle`, `session_locked`, `system_sleep`, `media_playback`, `terminal_command`, `tracker_run` |
| **point** (`ts` = l'instant) | `app_launch`, `system_boot`, `system_shutdown`, `network_change`, `file_change`, `git_commit`, `git_checkout`, `notification`, `collector_status` |
| **échantillon** (des nombres) | `system_metrics`, `input_activity` |
| **état** | `device_info` |

Le détail de chaque champ : [`PC_EVENTS.md`](PC_EVENTS.md).

### Temps

- `ts` est **toujours UTC, à la microseconde**. C'est le seul axe de jointure
  avec la montre et le reste.
- `ts_local` garde l'heure vécue : « 23 h » est un fait que l'UTC détruit.
- Deux événements du même type ne partagent jamais le même `ts` sur une
  machine : si l'horloge renvoie deux fois la même microseconde, le second
  avance d'une microseconde. Sans ça, la clé `(source, kind, started_at)`
  écraserait le premier en silence.

### Identité et doublons

| Barrière | Rôle |
|---|---|
| `event_id` (UUIDv7) | généré sur le PC, jamais par le serveur. Un renvoi porte le même identifiant |
| `dedup_key` | le même fait vu par deux chemins (ex. un démarrage lu en direct puis dans le journal Windows) |
| clé naturelle Chronicle | `(source, kind, started_at)` : rejouer un lot réécrit les mêmes lignes |
| empreinte du lot | un lot déjà archivé est reconnu et n'est pas retraité |

## 6. Correspondance avec Chronicle

| Événement | Table | Clé |
|---|---|---|
| intervalles et points | `episode` | `kind` = `event_type`, `started_at` = `ts`, `ended_at` = `payload.ended_at` (= `ts` pour un point) |
| `system_metrics` | `observation` | `pc.cpu_pct`, `pc.ram_pct`, `pc.gpu_pct`, `pc.vram_used_mb`, `pc.net_down_bytes`… |
| `input_activity` | `observation` | `pc.input.keys`, `pc.input.clicks`, `pc.input.scroll`, `pc.input.mouse_distance`, `pc.input.active_s` |
| `device_info` | `profile_snapshot` | `kind = pc_device`, dédoublonné par contenu |
| le lot reçu | `raw_payload` | `endpoint = pc/events` |
| la machine | `source` | `pc:<device_id>` |
| la progression | `sync_state` | `pc/<device_id>` |

Seules les grandeurs **numériques et comparables** deviennent des
observations. Un booléen comme « sur secteur » reste dans le payload : une
observation constante pendant des semaines serait signalée à tort comme un
capteur mort par `python main.py quality`.

Les noms d'épisodes évitent ceux des autres sources : `system_sleep` et non
`sleep`, que `v_sleep` et `v_chambre` lisent sans filtrer la source.

## 7. Brut et dérivé

| Niveau | Où | Exemple |
|---|---|---|
| **brut** | le tracker | `14:02 → 14:31 app_focus vscode`, `14:31 → 14:34 browser_page youtube.com`, `14:20 → 14:24 idle` |
| **dérivé** | vues Chronicle | `Code = 43 min`, `YouTube = 7 min`, `3 changements de contexte` |
| **analyse** | Chronicle, plus tard | `session de concentration 14:02 → 14:31` |

Le tracker **mesure**. Il ne classe pas (« coding », « distraction ») et ne
calcule aucun total : un total stocké deviendrait une seconde vérité, qui
divergerait de la première à la première correction — la leçon de
`v_nutrition`.

Deux mesures restent côté tracker parce qu'elles sont impossibles à
reconstruire après coup :

- `input_active_s` d'une fenêtre : les secondes où il y a eu une entrée
  clavier ou souris pendant qu'elle était au premier plan. C'est ce qui
  distingue « VS Code ouvert 3 h » de « VS Code utilisé 1 h 45 ».
- `idle` commence à la **dernière entrée**, pas au moment où le seuil est
  franchi. Sinon chaque pause serait amputée du seuil (2 min).

### Lire : le dashboard

`api/dashboard.py` + `api/static/dashboard/` : la couche de lecture, servie
par la même API (`/dashboard/`). Elle suit la règle d'`analytics/` : elle
lit, n'écrit jamais, ne stocke aucun total — tout est recalculé depuis
`episode` et `observation` à chaque requête (< 1 s sur 90 jours de données
denses).

Deux choix qui la distinguent des vues `v_pc_*` :

- **le temps est découpé aux bornes du jour** local (ou de l'heure) : une
  session 23 h 30 → 0 h 30 compte 30 min pour chacun des deux jours. Un
  jour de changement d'heure dure 23 ou 25 h, découpé en instants absolus ;
- **les catégories** (Code & IA, Navigation, Communication, Notes &
  documents, Médias & loisirs, Système, Autre) sont une interprétation :
  elles vivent dans `analytics/pc_categories.py`, jamais dans les données.
  L'ordre des catégories est l'ordre des couleurs : une catégorie garde sa
  couleur quelle que soit la période.

Accès sans clé depuis la machine seulement (bouclage + en-tête `Host`
local, contre le « DNS rebinding ») ; depuis le réseau, la clé d'API.

## 8. Vie privée

Jamais collecté, par construction (pas une option) :

| | Comment c'est garanti |
|---|---|
| touches individuelles, texte tapé | l'entrée clavier est comptée, le code de touche n'est jamais lu |
| presse-papiers, captures d'écran, webcam, micro | aucun appel à ces API dans le code |
| contenu des fichiers | seuls chemin, extension et action sont lus |
| mots de passe, jetons, clés d'API | rédaction des commandes, messages de commit et URL (`pc/tracker/core/privacy.py`) |
| URL complètes | par défaut : domaine + titre de page. Requête et fragment jamais stockés |
| navigation privée | ni URL ni titre, seulement « privé » |
| contenu des notifications | seulement l'application et le type |
| identifiants de réseaux Wi-Fi | hachés avec un sel qui ne quitte jamais la machine |

Configurable : applications, domaines et chemins exclus
(`[privacy]` dans la configuration). Par défaut, les gestionnaires de mots de
passe et les principales banques sont exclus.

## 9. Performance

| Principe | Application |
|---|---|
| événements plutôt que sondage | premier plan (`SetWinEventHook`), verrouillage (WTS), veille (`WM_POWERBROADCAST`), fichiers (`ReadDirectoryChangesW`), entrées (Raw Input) |
| sondage lent quand il n'y a pas d'événement | métriques 60 s, réseau 30 s, processus 10 s, git : `stat` avant tout `git` |
| aucune dépendance compilée côté Windows | `ctypes` + `psutil` ; le média (WinRT) est optionnel |
| écritures groupées | une seule écriture SQLite par événement, envoi par lots de 500 |
| aucun crochet clavier bas niveau | Raw Input est asynchrone : un tracker lent ne ralentit jamais la souris |

Mesuré sur ce portable (Ryzen 5 4500U, 6 cœurs), tracker complet en marche :
**0,10 % de CPU en moyenne (pic 0,27 %), 65 Mo de mémoire, 15 fils**. La
mesure a été faite machine peu sollicitée (lecture vidéo) : des mouvements
de souris rapides ajoutent quelques dixièmes de pour cent (Raw Input). Le
hook bash coûte ~20 ms par commande sous Git Bash (une seule lecture de
l'historique, aucun autre processus lancé).

Piège de mesure noté en passant : sous Windows, le `python.exe` d'un venv
n'est qu'un lanceur (1 fil, 5 Mo) qui démarre l'interpréteur réel en
processus enfant. Mesurer le lanceur donne 0 % et 5 Mo — faux.

## 10. Choix propres à Windows

**Pas de service Windows.** Un service tourne dans la session 0, isolée du
bureau : il ne voit ni la fenêtre au premier plan, ni le clavier, ni la
souris. Le tracker est donc un processus utilisateur sans fenêtre
(`pythonw`), lancé par une **tâche planifiée à l'ouverture de session**, avec
une répétition toutes les 15 min qui le relance s'il est tombé.

**Arrêt propre.** Fin de session, `stop`, Ctrl+C : les intervalles ouverts sont
fermés, la file est écrite sur disque. Après un arrêt brutal, le démarrage
suivant ferme les intervalles orphelins au dernier battement de cœur (30 s),
marqués `end_reason = recovered`.

**Le journal système.** Démarrage, arrêt (planifié ou brutal) et veille sont
relus dans le journal Windows, avec leurs heures exactes — y compris quand le
tracker ne tournait pas.

## 11. Phase Linux (Omarchy)

Rien n'est modifié sur Omarchy avant la validation du modèle commun sous
Windows. Ensuite :

1. **Auditer** le tracker existant, sur le poste lui-même :
   - quelles données, quel format, quelle fréquence ;
   - où il stocke (fichiers, SQLite, Data Lake ?) et comment il envoie ;
   - ce qui marche bien et doit être gardé (souvent : l'accès à Hyprland).
2. Réutiliser **tel quel** tout ce qui est commun : `pc/schema.py`,
   `pc/apps.py`, `pc/tracker/core/`, `pc/tracker/collectors/`.
3. Écrire seulement `pc/tracker/platforms/linux/` : fenêtre active
   (socket IPC de Hyprland), inactivité, veille et verrouillage (logind sur
   D-Bus), entrées (libinput/evdev, compteurs seulement), média (MPRIS).
4. `device_id = "omarchy-desktop"`, puis vérifier dans `v_pc_daily` que les
   deux machines se lisent avec les mêmes requêtes.

Le détail est dans `pc/tracker/platforms/linux/README.md`.

## 12. Limites connues

- **Navigateur** : le domaine et l'URL demandent l'extension (Chrome, Edge,
  Firefox). Sans elle, seul le titre de la fenêtre est connu.
- **Terminal** : PowerShell et bash via un hook de prompt. `cmd.exe` n'a pas
  de hook : ses commandes ne sont pas vues.
- **Température CPU** : Windows ne l'expose pas sans pilote tiers ; la valeur
  reste vide plutôt que fausse.
- **Notifications** : lues dans la base locale de Windows, non documentée.
  Une notification ignorée avant la lecture suivante (15 s) peut manquer.
- **Horloge** : un PC dont l'horloge est fausse produit des données fausses.
  L'API refuse les instants avant 2020 ou plus de 2 jours dans le futur.
