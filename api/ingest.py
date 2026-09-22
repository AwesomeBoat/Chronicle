"""Ingestion d'un lot : le seul chemin entre une source qui pousse et la base.

Tout se passe dans UNE transaction :

    1. registres   la source de la machine, le catalogue des metriques
    2. brut        le lot entier dans raw_payload (empreinte de contenu)
    3. traduction  pc/mapper.py : evenements -> Batch
    4. ecriture    store_batch, le meme que Polar, Kindle et les autres
    5. curseur     sync_state de la machine

Soit tout est ecrit, soit rien : une reponse 2xx veut dire "c'est en
base, brut compris", et le tracker peut oublier le lot.

REJEU
-----
Le brut archive porte (batch_id, device_id, events). Un lot renvoye a
l'identique - le cas "recu, mais la reponse s'est perdue" - a la meme
empreinte : raw_payload le reconnait, et rien n'est retraite
(`replayed: true`). Un meme evenement renvoye dans un autre lot tombe sur
les cles naturelles de Chronicle (ON CONFLICT) : aucun doublon non plus.

REFUS
-----
Un evenement invalide ne fait pas echouer le lot. Il est refuse, liste
dans la reponse, et son brut reste archive : corriger le connecteur puis
`python main.py pc` le rattrapera. Rien n'est perdu, rien n'est devine.

Ce module ne connait pas FastAPI : il se teste avec un dict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from database.connection import session_scope
from database.models import RawPayload
from database.repository import (get_or_create_source, get_sync_state,
                                 insert_raw_payload, mark_sync_success,
                                 store_batch, sync_metrics)
from pc import mapper as pc_mapper
from pc import schema

SOURCES = {"pc": pc_mapper}


class IngestError(Exception):
    """Lot refuse en entier (4xx). `status` est le code HTTP."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _source_du_lot(events: list) -> str:
    sources = {e.get("source") for e in events if isinstance(e, dict)}

    if len(sources) > 1:
        raise IngestError(422, f"un lot = une source ; recu {sorted(map(str, sources))}")

    source = sources.pop() if sources else "pc"

    if source not in SOURCES:
        raise IngestError(422, f"source non geree par cette API : {source!r} "
                               f"(geree : {sorted(SOURCES)})")

    return source


def ingest_batch(corps: dict[str, Any], max_events: int = 5000,
                 now: datetime | None = None) -> dict:
    """Ecrit un lot. Renvoie le compte rendu (voir la reponse de l'API)."""
    if not isinstance(corps, dict):
        raise IngestError(422, "objet JSON attendu")

    batch_id = corps.get("batch_id")
    device_id = corps.get("device_id")
    events = corps.get("events")

    if not isinstance(batch_id, str) or not 8 <= len(batch_id) <= 64:
        raise IngestError(422, "batch_id manquant ou mal forme")

    if not schema.valid_device_id(device_id):
        raise IngestError(422, f"device_id invalide : {device_id!r}")

    if not isinstance(events, list):
        raise IngestError(422, "events doit etre une liste")

    if len(events) > max_events:
        raise IngestError(413, f"{len(events)} evenements, plafond "
                               f"{max_events}")

    reponse = {"batch_id": batch_id, "device_id": device_id,
               "received": len(events), "stored": 0, "rejected": [],
               "replayed": False, "rows": {}}

    # Lot vide : c'est le "ping" du tracker (verifie l'adresse et la cle).
    if not events:
        return reponse

    connecteur = SOURCES[_source_du_lot(events)]
    maintenant = now or datetime.now(timezone.utc)

    # 1. Registres, dans leur propre transaction : la source et les
    # metriques doivent exister avant la moindre observation (cle
    # etrangere). Meme ordre que `store` et `_enregistrer` dans main.py.
    with session_scope() as session:
        source_id = get_or_create_source(session,
                                         connecteur.source_code(device_id),
                                         connecteur.source_label(device_id))
        metric_ids = sync_metrics(session, connecteur.METRICS)

    with session_scope() as session:
        # 2. Le brut d'abord. Sans sent_at : un renvoi du meme lot doit
        # avoir la meme empreinte.
        nouveau = insert_raw_payload(
            session, source_id, endpoint=connecteur.ENDPOINT,
            params={"batch_id": batch_id, "sent_at": corps.get("sent_at"),
                    "events": len(events)},
            fetched_at=maintenant,
            payload={"batch_id": batch_id, "device_id": device_id,
                     "events": events},
            filename=None)

        if not nouveau:
            reponse["replayed"] = True
            reponse["stored"] = len(events)
            return reponse

        # 3-4. Traduction et ecriture.
        lot, refus = connecteur.map_events(events, device_id, maintenant)
        reponse["rows"] = store_batch(session, source_id, metric_ids, lot)
        reponse["rejected"] = refus
        reponse["stored"] = len(events) - len(refus)

        # 5. Curseur : jusqu'ou cette machine est a jour. Il n'avance que :
        # un vieux lot renvoye (resend) ne doit pas le faire reculer.
        refuses = {r["event_id"] for r in refus}
        curseur = connecteur.cursor([e for e in events if isinstance(e, dict)
                                     and e.get("event_id") not in refuses])
        flux = connecteur.sync_endpoint(device_id)
        etat = get_sync_state(session, source_id, flux)

        if curseur is not None and etat.cursor_at is not None \
                and etat.cursor_at > curseur:
            curseur = etat.cursor_at

        mark_sync_success(session, source_id, flux, curseur)

    return reponse


def replay_archived(device_id: str | None = None) -> dict[str, int]:
    """Retraduit tous les lots PC archives (apres correction du mapper).

    Le pendant de `python main.py store` pour Polar : le brut est la
    verite, la base n'en est qu'une lecture. Idempotent.
    """
    totaux = {"lots": 0, "evenements": 0, "refus": 0}

    with session_scope() as session:
        lignes = session.execute(
            select(RawPayload.id, RawPayload.source_id, RawPayload.payload)
            .where(RawPayload.endpoint == pc_mapper.ENDPOINT)
            .order_by(RawPayload.fetched_at)).all()

    for _id, source_id, payload in lignes:
        machine = payload.get("device_id")

        if device_id and machine != device_id:
            continue

        with session_scope() as session:
            metric_ids = sync_metrics(session, pc_mapper.METRICS)
            lot, refus = pc_mapper.map_events(payload.get("events", []),
                                              machine)
            store_batch(session, source_id, metric_ids, lot)

        totaux["lots"] += 1
        totaux["evenements"] += len(payload.get("events", []))
        totaux["refus"] += len(refus)

    return totaux
