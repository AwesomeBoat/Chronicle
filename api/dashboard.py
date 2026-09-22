"""Dashboard de l'activite PC : les routes de LECTURE.

    GET /dashboard/              la page (fichiers de api/static/dashboard)
    GET /api/v1/pc/meta          machines, categories, fuseau
    GET /api/v1/pc/summary       une periode : chiffres, jours, apps, sites...
    GET /api/v1/pc/day           une journee : la chronologie detaillee

Cette couche LIT, elle n'ecrit jamais (regle de analytics/). Elle ne
stocke aucun total : tout est recalcule a partir des episodes et des
observations, comme les vues v_pc_*.

LE TEMPS EST DECOUPE, PAS ATTRIBUE
----------------------------------
Les vues v_pc_* rangent un intervalle au jour de son DEBUT. Ici, chaque
intervalle est coupe aux bornes du jour (ou de l'heure) : une session de
23 h 30 a 00 h 30 compte 30 min pour chaque jour. Les bornes sont celles
de l'heure LOCALE (un jour de changement d'heure dure 23 ou 25 h).

QUI PEUT LIRE
-------------
Sans cle : seulement depuis cette machine (adresse de bouclage ET en-tete
Host local - ce second controle bloque le "DNS rebinding", ou une page
web se ferait passer pour localhost). Depuis le reseau (serve --host
0.0.0.0) : la cle d'API, comme pour l'ingestion.
"""

from __future__ import annotations

import ipaddress
import secrets
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import text

from analytics.pc_categories import as_payload, category_of
from api import settings
from config import LOCAL_TZ
from database.connection import engine
from pc import schema

PERIODE_MAX_JOURS = 366

# "testserver" : l'hote du client de test de FastAPI.
HOTES_LOCAUX = {"localhost", "127.0.0.1", "::1", "testserver"}


# ================================================================ acces

def _local(request: Request) -> bool:
    """Requete venue de cette machine, adressee a cette machine."""
    client = request.client.host if request.client else ""

    if client == "testclient":                  # client de test FastAPI
        boucle = True
    else:
        try:
            boucle = ipaddress.ip_address(client).is_loopback
        except ValueError:
            boucle = False

    # urlsplit sait lire "127.0.0.1:8780", "localhost" et "[::1]:8780".
    hote = urlsplit("//" + (request.headers.get("host") or "")).hostname

    return boucle and (hote or "") in HOTES_LOCAUX


def lecture_autorisee(request: Request,
                      x_api_key: str | None = Header(default=None)) -> None:
    if _local(request):
        return

    if x_api_key and secrets.compare_digest(x_api_key, settings.api_key()):
        return

    raise HTTPException(401, "X-API-Key requise hors de cette machine")


router = APIRouter(prefix="/api/v1/pc", tags=["dashboard"],
                   dependencies=[Depends(lecture_autorisee)])


# ============================================================ utilitaires

def _requete(sql: str, **params) -> list[dict]:
    """Execute une lecture ; les Decimal de PostgreSQL deviennent des float.

    extract(epoch ...) renvoie un numeric depuis PostgreSQL 14 : sans cette
    conversion, la moindre division par un float leverait TypeError.

    jit = off, pour cette transaction seulement : generate_series fait
    croire au planificateur a 1 000 lignes, il compile alors la requete
    (~600 ms) pour un travail reel de quelques millisecondes.
    """
    with engine.connect() as connexion:
        connexion.execute(text("SET LOCAL jit = off"))
        return [{cle: float(v) if isinstance(v, Decimal) else v
                 for cle, v in ligne._mapping.items()}
                for ligne in connexion.execute(text(sql), params)]


def _metriques(*codes: str) -> dict[str, int]:
    """Identifiants des metriques : filtrer observation par metric_id
    laisse PostgreSQL utiliser l'index (source, metrique, instant)."""
    lignes = _requete("SELECT code, id FROM metric WHERE code = ANY(:codes)",
                      codes=list(codes))
    return {l["code"]: l["id"] for l in lignes}


def _sources(device: str | None) -> tuple[list[int], list[str]]:
    """Les sources PC visees (ids, noms de machines) : une, ou toutes."""
    if device and device != "all":
        if not schema.valid_device_id(device):
            raise HTTPException(422, f"device invalide : {device!r}")

        lignes = _requete("SELECT id, code FROM source WHERE code = :code",
                          code=f"pc:{device}")
    else:
        lignes = _requete("SELECT id, code FROM source "
                          "WHERE code LIKE 'pc:%'")

    return ([l["id"] for l in lignes],
            [l["code"].split(":", 1)[1] for l in lignes])


def _aujourd_hui() -> date:
    return datetime.now(ZoneInfo(LOCAL_TZ)).date()


def _minutes_du_jour(jour: date) -> int:
    """1440, ou 1380 / 1500 un jour de changement d'heure.

    Calcul en UTC : deux datetime du meme fuseau se soustraient "a
    l'horloge murale" en Python, sans voir le changement d'heure.
    """
    tz = ZoneInfo(LOCAL_TZ)
    debut = datetime.combine(jour, time.min, tzinfo=tz)
    fin = datetime.combine(jour + timedelta(days=1), time.min, tzinfo=tz)
    return round((fin.astimezone(timezone.utc)
                  - debut.astimezone(timezone.utc)).total_seconds() / 60)


def _periode(start: str | None, end: str | None) -> tuple[date, date]:
    try:
        fin = date.fromisoformat(end) if end else _aujourd_hui()
        debut = date.fromisoformat(start) if start else fin - timedelta(days=6)
    except ValueError:
        raise HTTPException(422, "dates au format AAAA-MM-JJ")

    if debut > fin:
        raise HTTPException(422, "start apres end")

    if (fin - debut).days >= PERIODE_MAX_JOURS:
        raise HTTPException(422, f"periode limitee a {PERIODE_MAX_JOURS} jours")

    return debut, fin


# La periode entiere comme un seul bucket (chiffres cles, top apps...).
PERIODE_SQL = """
    b AS (
        SELECT 'total' AS cle,
               CAST(:debut AS date)::timestamp AT TIME ZONE :tz AS debut,
               (CAST(:fin AS date) + 1)::timestamp AT TIME ZONE :tz AS fin
    )"""


def _buckets_sql(par_heure: bool) -> str:
    """CTE `b` : une ligne par jour (ou par heure) LOCAL, bornes en UTC."""
    if par_heure:
        # generate_series en instants absolus : un jour de changement
        # d'heure donne 23 ou 25 buckets, chacun avec sa vraie heure locale.
        return """
        b AS (
            SELECT to_char(h AT TIME ZONE :tz, 'HH24') AS cle,
                   h AS debut, h + interval '1 hour' AS fin
            FROM generate_series(
                CAST(:debut AS date)::timestamp AT TIME ZONE :tz,
                (CAST(:debut AS date) + 1)::timestamp AT TIME ZONE :tz
                    - interval '1 hour',
                interval '1 hour') AS h
        )"""

    return """
    b AS (
        SELECT to_char(d, 'YYYY-MM-DD') AS cle,
               d::timestamp AT TIME ZONE :tz AS debut,
               (d + interval '1 day')::timestamp AT TIME ZONE :tz AS fin
        FROM generate_series(CAST(:debut AS date), CAST(:fin AS date),
                             interval '1 day') AS d
    )"""


def _chevauchement(kinds: str) -> str:
    """Secondes des episodes `kinds`, coupees aux bornes de chaque bucket."""
    return f"""
    SELECT b.cle, e.kind, e.payload ->> 'app' AS app,
           sum(extract(epoch FROM least(e.ended_at, b.fin)
                                - greatest(e.started_at, b.debut))) AS secondes
    FROM b
    JOIN episode e ON e.source_id = ANY(:sources)
                  AND e.kind IN ({kinds})
                  AND e.started_at < b.fin AND e.ended_at > b.debut
    GROUP BY 1, 2, 3"""


def _minutes(secondes: float | None) -> float:
    return round(float(secondes or 0.0) / 60.0, 1)


def _arrondi(valeur: float | None, chiffres: int = 1) -> float | None:
    return None if valeur is None else round(float(valeur), chiffres)


# ================================================================ routes

@router.get("/meta")
def meta() -> dict:
    machines = _requete("""
        SELECT split_part(s.code, ':', 2) AS device,
               max(st.last_success_at) AS last_batch_at,
               max(st.cursor_at) AS data_until,
               (SELECT min(e.started_at) FROM episode e
                WHERE e.source_id = s.id) AS first_data
        FROM source s
        LEFT JOIN sync_state st ON st.source_id = s.id
        WHERE s.code LIKE 'pc:%'
        GROUP BY s.id, s.code
        ORDER BY 1""")

    return {"timezone": LOCAL_TZ, "categories": as_payload(),
            "devices": machines, "today": _aujourd_hui()}


@router.get("/summary")
def summary(device: str = Query("all"), start: str | None = None,
            end: str | None = None) -> dict[str, Any]:
    debut, fin = _periode(start, end)
    sources, machines = _sources(device)
    par_heure = debut == fin
    nb_jours = (fin - debut).days + 1
    reponse: dict[str, Any] = {
        "range": {"start": debut, "end": fin, "days": nb_jours,
                  "bucket": "hour" if par_heure else "day"},
        "device": device, "empty": True}

    # Sans source (machine inconnue, rien recu), les requetes tournent quand
    # meme sur des listes vides : la reponse garde toujours la meme forme.
    p = {"sources": sources, "machines": machines, "tz": LOCAL_TZ,
         "debut": debut, "fin": fin}
    fin_avant = debut - timedelta(days=1)
    avant = {**p, "debut": fin_avant - timedelta(days=nb_jours - 1),
             "fin": fin_avant}

    reponse.update(
        kpis=_kpis(p), previous=_kpis(avant),
        buckets=_buckets(p, par_heure), apps=_apps(p),
        domains=_domaines(p), heatmap=_heatmap(p),
        transitions=_transitions(p), commits=_commits(p),
        programs=_programmes(p), extensions=_extensions(p),
        machine=_machine(p, par_heure), health=_sante(p))
    k = reponse["kpis"]
    reponse["empty"] = not (k["premier_plan_min"] or k["actif_min"]
                            or k["commits"] or k["veille_min"])
    return reponse


@router.get("/day")
def day(device: str = Query("all"), day: str | None = None) -> dict[str, Any]:
    jour = _periode(day, day)[0] if day else _aujourd_hui()
    sources, _machines = _sources(device)
    reponse: dict[str, Any] = {"day": jour, "device": device, "lanes": {},
                               "minutes": _minutes_du_jour(jour)}

    p = {"sources": sources, "tz": LOCAL_TZ, "debut": jour, "fin": jour}
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT e.kind,
               extract(epoch FROM greatest(e.started_at, b.debut) - b.debut)
                   / 60 AS s,
               extract(epoch FROM least(e.ended_at, b.fin) - b.debut) / 60 AS e,
               e.payload ->> 'app' AS app,
               e.payload ->> 'app_name' AS app_name,
               e.payload ->> 'window_title' AS titre,
               (e.payload ->> 'input_active_s')::float8 AS actif_s,
               e.payload ->> 'player' AS lecteur,
               e.payload ->> 'title' AS media_titre,
               e.payload ->> 'artist' AS artiste,
               e.payload ->> 'state' AS etat,
               e.payload ->> 'end_reason' AS raison
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources)
                      AND e.kind IN ('app_focus', 'idle', 'session_locked',
                                     'system_sleep', 'media_playback',
                                     'tracker_run')
                      AND e.started_at < b.fin AND e.ended_at > b.debut
        ORDER BY e.started_at""", **p)

    lanes: dict[str, list] = {}

    for l in lignes:
        segment = {"s": round(l["s"], 2), "e": round(l["e"], 2)}

        if l["kind"] == "app_focus":
            segment.update(app=l["app"], app_name=l["app_name"] or l["app"],
                           title=l["titre"], active_s=l["actif_s"])
            lanes.setdefault(category_of(l["app"]), []).append(segment)
        elif l["kind"] == "media_playback":
            segment.update(player=l["lecteur"], title=l["media_titre"],
                           artist=l["artiste"])
            lanes.setdefault("media", []).append(segment)
        elif l["kind"] == "system_sleep":
            segment.update(state=l["etat"])
            lanes.setdefault("sleep", []).append(segment)
        else:
            nom = {"idle": "idle", "session_locked": "locked",
                   "tracker_run": "tracker"}[l["kind"]]
            segment.update(reason=l["raison"])
            lanes.setdefault(nom, []).append(segment)

    reponse["lanes"] = lanes
    return reponse


# ============================================================ requetes

def _kpis(p: dict) -> dict:
    durees: dict[str, float] = {}

    for l in _requete(f"""
            WITH {PERIODE_SQL}
            {_chevauchement("'app_focus', 'idle', 'session_locked', "
                            "'system_sleep', 'media_playback'")}""", **p):
        durees[l["kind"]] = durees.get(l["kind"], 0.0) + (l["secondes"] or 0)

    ids = _metriques("pc.input.active_s", "pc.input.keys")
    entrees = {l["code"]: l["total"] for l in _requete(f"""
        WITH {PERIODE_SQL}
        SELECT m.code, sum(o.value) AS total
        FROM b
        JOIN observation o ON o.source_id = ANY(:sources)
                          AND o.metric_id = ANY(:ids)
                          AND o.observed_at >= b.debut
                          AND o.observed_at < b.fin
        JOIN metric m ON m.id = o.metric_id
        GROUP BY m.code""", **p, ids=list(ids.values()))}

    (comptes,) = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT count(*) FILTER (WHERE e.kind = 'git_commit') AS commits,
               sum((e.payload ->> 'insertions')::int)
                   FILTER (WHERE e.kind = 'git_commit') AS lignes_plus,
               sum((e.payload ->> 'deletions')::int)
                   FILTER (WHERE e.kind = 'git_commit') AS lignes_moins,
               count(*) FILTER (WHERE e.kind = 'terminal_command') AS commandes,
               count(*) FILTER (WHERE e.kind = 'file_change') AS fichiers,
               count(*) FILTER (WHERE e.kind = 'notification') AS notifications,
               count(*) FILTER (WHERE e.kind = 'app_launch') AS lancements
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources)
                      AND e.kind IN ('git_commit', 'terminal_command',
                                     'file_change', 'notification',
                                     'app_launch')
                      AND e.started_at >= b.debut AND e.started_at < b.fin
        """, **p)

    (changements,) = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT count(*) AS n
        FROM b
        JOIN v_pc_context_switches v ON v.device = ANY(:machines)
                                    AND v.instant >= b.debut
                                    AND v.instant < b.fin""", **p)

    return {
        "premier_plan_min": _minutes(durees.get("app_focus")),
        "actif_min": _minutes(entrees.get("pc.input.active_s")),
        "inactif_min": _minutes(durees.get("idle")),
        "verrouille_min": _minutes(durees.get("session_locked")),
        "veille_min": _minutes(durees.get("system_sleep")),
        "media_min": _minutes(durees.get("media_playback")),
        "changements": changements["n"] or 0,
        "touches": int(entrees.get("pc.input.keys") or 0),
        "commits": comptes["commits"] or 0,
        "lignes_plus": comptes["lignes_plus"] or 0,
        "lignes_moins": comptes["lignes_moins"] or 0,
        "commandes": comptes["commandes"] or 0,
        "fichiers": comptes["fichiers"] or 0,
        "notifications": comptes["notifications"] or 0,
        "lancements": comptes["lancements"] or 0,
    }


def _buckets(p: dict, par_heure: bool) -> list[dict]:
    """Par jour (ou par heure) : minutes au premier plan par categorie, et
    minutes reellement actives (entrees clavier/souris)."""
    bucket_sql = _buckets_sql(par_heure)
    par_cle = {c["cle"]: {"key": c["cle"], "categories": {}, "actif_min": 0.0}
               for c in _requete(f"WITH {bucket_sql} SELECT cle FROM b "
                                 f"ORDER BY debut", **p)}

    for l in _requete(f"WITH {bucket_sql} {_chevauchement(repr('app_focus'))}",
                      **p):
        categories = par_cle[l["cle"]]["categories"]
        categorie = category_of(l["app"])
        categories[categorie] = round(categories.get(categorie, 0.0)
                                      + (l["secondes"] or 0) / 60, 2)

    ids = _metriques("pc.input.active_s")

    for l in _requete(f"""
            WITH {bucket_sql}
            SELECT b.cle, sum(o.value) AS secondes
            FROM b
            JOIN observation o ON o.source_id = ANY(:sources)
                              AND o.metric_id = ANY(:ids)
                              AND o.observed_at >= b.debut
                              AND o.observed_at < b.fin
            GROUP BY 1""", **p, ids=list(ids.values())):
        par_cle[l["cle"]]["actif_min"] = _minutes(l["secondes"])

    return list(par_cle.values())


def _apps(p: dict, limite: int = 12) -> list[dict]:
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT e.payload ->> 'app' AS app,
               max(e.payload ->> 'app_name') AS app_name,
               count(*) AS fenetres,
               sum(extract(epoch FROM least(e.ended_at, b.fin)
                                    - greatest(e.started_at, b.debut)))
                   AS secondes,
               -- Secondes actives au prorata de la part de l'intervalle
               -- qui tombe dans la periode.
               sum((e.payload ->> 'input_active_s')::float8
                   * extract(epoch FROM least(e.ended_at, b.fin)
                                      - greatest(e.started_at, b.debut))::float8
                   / NULLIF(extract(epoch FROM e.ended_at
                                             - e.started_at)::float8, 0))
                   AS actif_s
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources) AND e.kind = 'app_focus'
                      AND e.started_at < b.fin AND e.ended_at > b.debut
        GROUP BY 1
        ORDER BY secondes DESC
        LIMIT :limite""", **p, limite=limite)

    return [{"app": l["app"], "app_name": l["app_name"] or l["app"],
             "category": category_of(l["app"]),
             "minutes": _minutes(l["secondes"]),
             "actif_min": _minutes(l["actif_s"]),
             "fenetres": l["fenetres"]} for l in lignes]


def _domaines(p: dict, limite: int = 10) -> list[dict]:
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT COALESCE(e.payload ->> 'domain', '(prive)') AS domaine,
               count(*) AS pages,
               sum(extract(epoch FROM least(e.ended_at, b.fin)
                                    - greatest(e.started_at, b.debut)))
                   AS secondes
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources)
                      AND e.kind = 'browser_page'
                      AND e.started_at < b.fin AND e.ended_at > b.debut
        GROUP BY 1
        ORDER BY secondes DESC
        LIMIT :limite""", **p, limite=limite)

    return [{"domain": l["domaine"], "pages": l["pages"],
             "minutes": _minutes(l["secondes"])} for l in lignes]


def _heatmap(p: dict) -> list[dict]:
    """Minutes actives par jour de semaine (1 = lundi) et heure locale."""
    ids = _metriques("pc.input.active_s")
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT extract(isodow FROM o.observed_at AT TIME ZONE :tz)::int AS jour,
               extract(hour FROM o.observed_at AT TIME ZONE :tz)::int AS heure,
               sum(o.value) AS secondes
        FROM b
        JOIN observation o ON o.source_id = ANY(:sources)
                          AND o.metric_id = ANY(:ids)
                          AND o.observed_at >= b.debut
                          AND o.observed_at < b.fin
        GROUP BY 1, 2""", **p, ids=list(ids.values()))

    return [{"dow": l["jour"], "hour": l["heure"],
             "minutes": _minutes(l["secondes"])} for l in lignes]


def _transitions(p: dict, limite: int = 10) -> list[dict]:
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT v.de, v.vers, count(*) AS n
        FROM b
        JOIN v_pc_context_switches v ON v.device = ANY(:machines)
                                    AND v.instant >= b.debut
                                    AND v.instant < b.fin
        GROUP BY 1, 2
        ORDER BY n DESC, 1, 2
        LIMIT :limite""", **p, limite=limite)

    return [{"from": l["de"], "from_category": category_of(l["de"]),
             "to": l["vers"], "to_category": category_of(l["vers"]),
             "n": l["n"]} for l in lignes]


def _commits(p: dict, limite: int = 20) -> list[dict]:
    lignes = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT e.started_at AT TIME ZONE :tz AS quand,
               e.payload ->> 'repo' AS depot,
               e.payload ->> 'branch' AS branche,
               e.payload ->> 'message' AS message,
               (e.payload ->> 'insertions')::int AS plus,
               (e.payload ->> 'deletions')::int AS moins,
               (e.payload ->> 'files_changed')::int AS fichiers,
               left(e.payload ->> 'commit', 8) AS hash
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources) AND e.kind = 'git_commit'
                      AND e.started_at >= b.debut AND e.started_at < b.fin
        ORDER BY e.started_at DESC
        LIMIT :limite""", **p, limite=limite)

    return [{"when": l["quand"].isoformat(timespec="minutes"),
             "repo": l["depot"], "branch": l["branche"],
             "message": l["message"], "insertions": l["plus"],
             "deletions": l["moins"], "files": l["fichiers"],
             "hash": l["hash"]} for l in lignes]


def _compte_par(p: dict, kind: str, champ: str, defaut: str,
                limite: int = 8) -> list[dict]:
    return _requete(f"""
        WITH {PERIODE_SQL}
        SELECT COALESCE(e.payload ->> '{champ}', '{defaut}') AS label,
               count(*) AS n
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources) AND e.kind = '{kind}'
                      AND e.started_at >= b.debut AND e.started_at < b.fin
        GROUP BY 1 ORDER BY n DESC, 1 LIMIT :limite""", **p, limite=limite)


def _programmes(p: dict) -> list[dict]:
    return _compte_par(p, "terminal_command", "program", "(inconnu)")


def _extensions(p: dict) -> list[dict]:
    return _compte_par(p, "file_change", "extension", "(sans)")


def _machine(p: dict, par_heure: bool) -> list[dict]:
    """CPU / RAM moyens et trafic reseau par bucket."""
    ids = _metriques("pc.cpu_pct", "pc.ram_pct", "pc.net_down_bytes",
                     "pc.net_up_bytes")
    lignes = _requete(f"""
        WITH {_buckets_sql(par_heure)}
        SELECT b.cle,
               avg(o.value) FILTER (WHERE m.code = 'pc.cpu_pct') AS cpu,
               avg(o.value) FILTER (WHERE m.code = 'pc.ram_pct') AS ram,
               sum(o.value) FILTER (WHERE m.code = 'pc.net_down_bytes') / 1e6
                   AS recu_mo,
               sum(o.value) FILTER (WHERE m.code = 'pc.net_up_bytes') / 1e6
                   AS envoye_mo
        FROM b
        LEFT JOIN observation o ON o.source_id = ANY(:sources)
                               AND o.metric_id = ANY(:ids)
                               AND o.observed_at >= b.debut
                               AND o.observed_at < b.fin
        LEFT JOIN metric m ON m.id = o.metric_id
        GROUP BY b.cle, b.debut
        ORDER BY b.debut""", **p, ids=list(ids.values()))

    return [{"key": l["cle"], "cpu": _arrondi(l["cpu"]),
             "ram": _arrondi(l["ram"]), "down_mb": _arrondi(l["recu_mo"]),
             "up_mb": _arrondi(l["envoye_mo"])} for l in lignes]


def _sante(p: dict) -> dict:
    """La collecte elle-meme : couverture, erreurs, derniere donnee."""
    (couverture,) = _requete("""
        WITH b AS (
            SELECT CAST(:debut AS date)::timestamp AT TIME ZONE :tz AS debut,
                   least((CAST(:fin AS date) + 1)::timestamp AT TIME ZONE :tz,
                         now()) AS fin
        )
        SELECT sum(extract(epoch FROM least(e.ended_at, b.fin)
                                    - greatest(e.started_at, b.debut)))
                   AS suivi_s,
               max(extract(epoch FROM b.fin - b.debut)) AS periode_s,
               count(e.id) AS lancements
        FROM b
        LEFT JOIN episode e ON e.source_id = ANY(:sources)
                           AND e.kind = 'tracker_run'
                           AND e.started_at < b.fin AND e.ended_at > b.debut
        """, **p)

    erreurs = _requete(f"""
        WITH {PERIODE_SQL}
        SELECT e.started_at AT TIME ZONE :tz AS quand,
               e.payload ->> 'collector' AS collecteur,
               e.payload ->> 'status' AS statut,
               e.payload ->> 'detail' AS detail
        FROM b
        JOIN episode e ON e.source_id = ANY(:sources)
                      AND e.kind = 'collector_status'
                      AND e.payload ->> 'status' <> 'started'
                      AND e.started_at >= b.debut AND e.started_at < b.fin
        ORDER BY e.started_at DESC LIMIT 8""", **p)

    (derniere,) = _requete("""
        SELECT max(COALESCE(ended_at, started_at)) AS derniere
        FROM episode WHERE source_id = ANY(:sources)""", **p)

    periode = couverture["periode_s"] or 0

    return {
        "tracked_min": _minutes(couverture["suivi_s"]),
        "coverage_pct": round(100 * (couverture["suivi_s"] or 0) / periode, 1)
        if periode > 0 else None,
        "runs": couverture["lancements"],
        "last_event": derniere["derniere"],
        "errors": [{"when": e["quand"].isoformat(timespec="minutes"),
                    "collector": e["collecteur"], "status": e["statut"],
                    "detail": e["detail"]} for e in erreurs],
    }
