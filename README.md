# Chronicle

**A personal data historian. Collect now, understand later.**

Most quantified-self apps show you a dashboard of yesterday and quietly discard
the rest. This is the opposite: an infrastructure that collects patiently now, so
that in two years I can ask questions I can't even formulate today.

Sleep, heart rate, activity, food, room temperature, reading, screen time, computer
activity. Not to
look at a number this evening, but to have enough history to find out what
actually relates to what.

---

## Why this has to be built now, not later

**The data is perishable.** That single fact drives every design decision in this
repository.

| Source | Retention if nobody is listening |
|---|---|
| Polar | erased after ~28 days |
| Kindle | reading sessions go to a rotating buffer, uploaded to Amazon, then deleted |
| Android usage stats | detailed events aggregated away after a short window |
| Bedroom sensor | 24 h of onboard memory |
| Food journal | **never recoverable** — nobody remembers what they ate on March 12th |

You can always run a better model on old data. You can never run any model on
data you didn't keep. So the priority is capture, not analysis, and the analysis
layer stays deliberately simple until the history is deep enough to deserve
better.

> ⏰ Every day without collection is lost permanently.

---

## The design constraint: a source is not a table

The schema is organised by the **temporal shape** of the data, not by where it
came from. This is what lets a new source arrive without a migration.

| Table | Shape | Examples |
|---|---|---|
| `observation` | a **point** in time | a heartbeat, a meal's calories, the temperature at 3am |
| `episode` | an **interval** | a night of sleep, a meal, a workout |
| `profile_snapshot` | a **slowly changing state** | weight, VO₂max, height |
| `source` · `metric` | registries | one row per source, one per quantity |
| `raw_payload` | traceability | the raw response, exactly as received |
| `sync_state` | collection cursor | where to resume, per stream |

A bedroom temperature reading and a heartbeat are both points: same table,
different `source_id`.

**It has been verified twice.** Adding the food journal and the bedroom sensor
required **zero changes** to `database/models.py` or `database/repository.py`.

**Connectors don't know the database exists.** They produce `Batch` objects (see
`database/records.py`); the repository consumes them. No connector imports
SQLAlchemy, and `python main.py sources` checks that mechanically rather than
trusting it.

Views (`v_daily`, `v_sleep`, `v_nutrition`, `v_chambre`) denormalise at read
time. A view stores nothing, so it cannot drift out of sync.

### Why this shape is not a personal quirk

This is the same problem an **industrial data historian** solves: heterogeneous
sources, perishable time series, idempotent ingestion that survives a network
outage, and a schema that has to accept equipment nobody has specified yet. The
scale differs. The design does not.

---

## Collectors

Each source is an independent project. None of them knows anything about this
database beyond the `Batch` contract.

| Collector | Where | What it recovers |
|---|---|---|
| **Polar AccessLink** | `polar/`, in this repo | sleep stages, HRV, continuous heart rate, activity, training sessions |
| **Food journal** | `food/`, in this repo | one text file per day, resolved against the CIQUAL nutritional table |
| **Bedroom sensor** | `sensors/`, in this repo | ESP32 + BME280: temperature, humidity, pressure |
| **kindle-tracker** | separate repo | reading sessions, highlights and vocabulary lookups, read straight from the Kindle's own SQLite databases before the firmware deletes them |
| **screenTwin** | separate repo | Android per-app screen time, via a native Kotlin module |
| **PC tracker** | `pc/`, in this repo | focused window, real input activity, idle, sleep, apps, web pages, git, terminal, files, media, system metrics — one source per machine (`pc:windows-main`, later `pc:omarchy-desktop`), same event model on every OS. **No keylogger** |

The PC is the first source that **pushes**, from several machines: it
talks to Chronicle's ingestion API (`python main.py serve`) with the common
event envelope shared with PhoneTracker. See
[`docs/PC_TRACKING.md`](docs/PC_TRACKING.md).

---

## State

| Version | Scope | Status |
|---|---|---|
| **V0** | Polar OAuth2 + first API calls | ✅ |
| **V1** | PostgreSQL (Docker) + idempotent temporal schema | ✅ |
| **V2** | Automated collection, cursors, diagnostics | ✅ except the scheduled task |
| **V3** | Analytics: quality, descriptive stats, figures, correlations | ✅ |
| **V4** | New sources: food journal + bedroom sensor | ✅ except hardware |
| V5 | Machine learning, once the history justifies it | upcoming |

**5 connectors** · **88 metrics** · **7 tables** (unchanged since V1) · **11 views**

---

## Install

### Requirements

- **Python 3.11+** (tested on 3.14)
- **Docker Desktop** (for PostgreSQL 16)
- A [Polar AccessLink](https://admin.polaraccesslink.com) developer account

### Setup

```powershell
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Configuration — copy and fill in
copy .env.example .env

# 3. Database
docker compose up -d
.venv\Scripts\python.exe main.py initdb

# 4. Polar authorisation (opens the browser)
.venv\Scripts\python.exe main.py auth

# 5. CIQUAL nutritional table (~20 s, once)
.venv\Scripts\python.exe main.py ciqual
```

> Commands below are written `python main.py <command>`, assuming the venv is
> activated. Otherwise prefix with `.venv\Scripts\python.exe`.

### Environment variables

| Variable | Role | Default |
|---|---|---|
| `POLAR_CLIENT_ID` / `POLAR_CLIENT_SECRET` | Polar app credentials | — (required) |
| `POLAR_REDIRECT_URI` | must be **identical** on admin.polaraccesslink.com | — (required) |
| `CALLBACK_PORT` | OAuth2 callback server port | `8000` |
| `POSTGRES_PASSWORD` | database password | — (required) |
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` | PostgreSQL connection | `localhost` / `5432` / `digitaltwin` / `twin` |
| `LOCAL_TZ` | timezone applied to naive timestamps | `Europe/Paris` |
| `BEDROOM_SENSOR_URL` | ESP32 module address | — (optional) |
| `SQL_ECHO` | `1` to print every SQL statement | `0` |
| `CHRONICLE_API_HOST` / `PORT` | ingestion API (`serve`) | `127.0.0.1` / `8780` |
| `CHRONICLE_API_KEY` | API key for the trackers | generated in `data/api_key.txt` |

`.env` and `tokens.json` are **never** versioned.

---

## Usage

### 1. Collection

```bash
python main.py collect          # fetch → map → store, in one pass
python main.py health           # is everything fine? (exit code = number of problems)
python main.py status           # token state
```

`collect` is **idempotent**: running it again immediately inserts nothing. It
keeps one cursor per stream with an overlap window, so it can catch up after
several days offline.

<details>
<summary>Automating collection (Windows scheduled task)</summary>

```powershell
schtasks /Create /TN "Chronicle Collect" /SC HOURLY /MO 4 ^
  /TR "C:\Users\everv\Desktop\CodeMastery\PROJECTS\Chronicle\run_collect.bat" ^
  /ST 07:00 /RL LIMITED
```

Two conditions for this to work: the PC must be **on with the session open**
(the task does not wake the machine), and **Docker Desktop must start on
login** — `run_collect.bat` starts the container, not the Docker engine.
</details>

Low-level commands, useful for diagnosis:

```bash
python main.py fetch            # call the API, archive JSON into data/raw/
python main.py store            # replay data/raw/ into the database
```

### 2. Food

One text file per day in `data/food/`, one line per intake.

```bash
python main.py eat "12h30, 100g de riz cuit, 55g de poulet, 50g de brocoli"
python main.py food             # replay every journal into the database
```

**Line format** — a time, then the foods:

```
12h30, 100g de riz cuit, 55g de poulet, 50g de brocoli
16h. 1 banane 1/2 pomme
8h05 2 oeufs, 30g flocons avoine, 200ml lait
```

| Element | Accepted forms |
|---|---|
| Time | `12h30` · `16h` · `8:05` · `12h 30`, followed by `,` `.` `;` `-` or nothing |
| Weight / volume | `100g` `1kg` `200ml` `2cl` |
| Kitchen units | `1 cs` (15 g) · `1 cc` (5 g) · `1 bol` · `1 verre` |
| Counts | `2 oeufs` · `1 banane` · `1/2 pomme` · `½ pomme` |
| Filler words | `de`, `du`, `la`… are ignored |

The file is edited by hand (Obsidian, notepad) and replayed with `food` — **the
file is the source of truth, the database is only a reading of it.**

> **Parser rule: anything not understood with certainty is rejected, never
> guessed.** A partially understood line does not enter the database, not even
> for its valid parts. The error message says what to fix.

Two tables are maintained by hand, and that is the project's upkeep gesture:

- `food/portions.py` — how much "1 banana" weighs (120 g). Quantities derived
  from it are flagged **estimated** in the database.
- `food/aliases.py` — what "poulet" means. CIQUAL distinguishes 40 chickens;
  this table decides once and for all. `python main.py quality` checks it.

```bash
python main.py ciqual "riz blanc cuit"   # look up a food and its code
```

### 3. Bedroom sensor

An ESP32 + BME280 (~€15) measures temperature, humidity and pressure, keeps 24 h
in memory, and **waits to be polled** — which lets it survive a PC that has been
off for days.

```bash
python main.py bedroom              # query the module
python main.py bedroom --simulate   # FABRICATED data, for development
```

<details>
<summary>Building the module</summary>

**Wiring**: `3V3→VIN`, `GND→GND`, `GPIO22→SCL`, `GPIO21→SDA`

1. Flash `sensors/firmware/bedroom_esp32.ino` (libraries: *Adafruit BME280*,
   *Adafruit Unified Sensor*, *ArduinoJson v7*)
2. Set `WIFI_SSID` / `WIFI_PASS` in the `.ino` file
3. Read the IP printed on the serial monitor
4. `.env` → `BEDROOM_SENSOR_URL=http://192.168.1.42`

**Placement**: away from the radiator, the window and the bed itself — a human
body heats and humidifies its immediate surroundings. At bed height, one or two
metres away, against an interior wall.
</details>

### 4. Analysis

```bash
python main.py quality       # is this data usable?
python main.py describe      # means, medians, distributions, outliers
python main.py plots         # PNG figures
python main.py correlate     # correlations, with n, p and Bonferroni
python main.py report        # all of the above in a dated folder: reports/
```

The order is not decorative. `quality` comes first: a flawless collection can
still produce a column of 30 zeros, and nothing else would say so.

`correlate` always prints `r`, `n` and `p` **together**, and refuses to conclude
when the sample is too small. It also discards tautologies (`steps` and
`distance` are derived from each other by Polar) and collection metadata.

For free exploration: `notebooks/exploration.ipynb`.

### 5. Database

```bash
python main.py initdb        # create tables + views (idempotent)
python main.py dbstats       # row count per table
python main.py sources       # declared connectors + contract check
python main.py resetdb       # DESTRUCTIVE, asks for confirmation
```

Views available in SQL:

| View | Grain | Contents |
|---|---|---|
| `v_daily` | one day | every daily quantity + aggregated HR |
| `v_sleep` | one night | bedtime, wake time, stages, HRV, HR |
| `v_nutrition` | one day | nutritional totals, meal count, eating window |
| `v_chambre` | one night | temperature and humidity **during** sleep |
| `v_lecture` / `v_livres` | one day / one book | reading sessions, highlights, progress |
| `v_pc_daily` | machine × day | time in front / really active / idle / locked / asleep, context switches, keys, commits |
| `v_pc_apps_daily` | machine × day × app | foreground minutes vs active minutes |
| `v_pc_context_switches` | one switch | from app → to app, gap |
| `v_pc_domains_daily` | machine × day × domain | minutes per website |
| `v_pc_focus` | one window span | the flattened `app_focus` episodes |

### 6. PC activity (the source that pushes)

```bash
python main.py serve         # ingestion API + dashboard: http://127.0.0.1:8780/dashboard/
python main.py pc            # replay archived PC batches (after a mapper fix)
python -m pc.tracker --help  # the agent itself: init, run, install, status...
```

Each tracker keeps a local queue and sends batches; if Chronicle or Docker
is down, nothing is lost. The dashboard (active vs foreground time, apps,
websites, day timeline, weekly rhythm, Git, collection health) reads the
episodes and observations directly and stores nothing; it answers without a
key from this machine only. Install, configuration, privacy, troubleshooting:
[`pc/tracker/README.md`](pc/tracker/README.md). Event reference:
[`docs/PC_EVENTS.md`](docs/PC_EVENTS.md).

Tests: `python -m pytest tests/` (the database tests need a **dedicated**
test database, see `tests/conftest.py` — they never touch `digitaltwin`).

---

## Project structure

```
main.py                CLI — every command
config.py              environment variables, DSN
collector.py           Polar pipeline: fetch → map → store
health.py              collection diagnostics
logging_setup.py       rotating logs

auth/                  Polar OAuth2 (authorisation, callback, tokens)
polar/                 HTTP client + one module per endpoint + mapper
food/                  CIQUAL, portions, aliases, parser, journal, mapper
sensors/               bedroom sensor + Arduino firmware
kindle/                Kindle reader, receiver, Amazon export importer
connectors/            the contract shared by every source

pc/                    the PC as a source
  schema.py            the common event model (Windows = Linux)
  apps.py              canonical app names across OSes
  mapper.py            connector: events → Batch
  tracker/             the agent that runs on each PC (never imports database/)
api/                   ingestion API for sources that push (FastAPI)
  dashboard.py         read routes of the PC dashboard (/api/v1/pc/*)
  static/dashboard/    the dashboard page (plain HTML/CSS/JS, no build)

database/
  models.py            the 7 tables (SQLAlchemy 2.0)
  records.py           Batch — the connector ↔ database boundary
  repository.py        idempotent insertion (ON CONFLICT)
  views.py             the 11 read views
  connection.py        engine, sessions, pool

analytics/             load, quality, describe, plots, correlate, report,
                       pc_categories (app → category, for the dashboard)
notebooks/             Jupyter exploration
tests/                 pytest: tracker, connector, API, end-to-end
docs/SCHEMA.md         why the data model looks like this
docs/PC_TRACKING.md    the PC source: architecture and decisions
docs/PC_EVENTS.md      the PC event reference
```

---

## Known traps

These cost real time. They are written down so they only cost it once.

**Polar AccessLink**

- Three distinct hosts: authorisation `flow.polar.com`, token `polarremote.com`,
  data `www.polaraccesslink.com/v3`
- `POST /v3/users` is **mandatory** after the token exchange. Without it, every
  `/v3/users/*` call fails despite a perfectly valid token.
- No `refresh_token`: the access token lasts ~10 years
- `204 No Content` means no new data. That is normal, not an error.
- Today's `sleep.score` only arrives if the watch has been synced

**Data**

- Continuous HR is sampled **in bursts** (1 Hz under effort, one reading every
  5 min at rest). Aggregating raw readings massively overweights exercise: raw
  median 108 bpm against 75.5 when weighted by time. So the views aggregate
  **per minute first**.
- 28% of CIQUAL foods have **no energy value at all** — "Broccoli, cooked" is one
  of them. The resolver discards them and says so.
- The Pacer's `cardio.*` metrics return nothing but zeros.

**Environment**

- `data/food/` is the project's **only non-regenerable data**. Polar gives back
  its last 28 days, the sensor holds 24 h — but nobody remembers what they ate on
  March 12th. Back it up elsewhere.
- Windows console: avoid non-ASCII characters in `print()`
- `scipy` is blocked by application control policy on the development machine, so
  p-values are computed by hand in `analytics/correlate.py` (validated against
  Student's table and by simulation)

---

## Further reading

| Document | Contents |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | version breakdown, decisions, bugs found and why |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | justification of the data model |
| [`docs/PC_TRACKING.md`](docs/PC_TRACKING.md) | the PC source: audit, architecture, raw vs derived, privacy |
| [`docs/PC_EVENTS.md`](docs/PC_EVENTS.md) | the 21 PC event types, field by field |
| [`pc/tracker/README.md`](pc/tracker/README.md) | installing and running the tracker |

**This is a learning project as much as a working one.** The goal was never a
finished tool, it was understanding every line it contains. `ROADMAP.md`
documents the mistakes as carefully as the solutions — that is deliberate, and
it is the part worth rereading.
