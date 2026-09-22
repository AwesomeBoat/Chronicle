"""L'application FastAPI : trois routes, la logique est dans ingest.py.

La base peut etre arretee (Docker eteint) : l'API reste debout et repond
503. Le tracker garde ses evenements et reessaie - c'est son role. Rien
n'est perdu, rien n'est ecrit a moitie.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from api import settings
from api.dashboard import router as dashboard_router
from api.ingest import IngestError, ingest_batch
from database.connection import check_connection, session_scope
from database.models import Source, SyncState

logger = logging.getLogger("api")

VERSION = "1.1.0"

app = FastAPI(
    title="Chronicle - API d'ingestion et dashboard PC",
    description="Recoit les evenements des sources qui poussent (PC) et "
                "sert le dashboard de lecture. Enveloppe commune : voir "
                "docs/PC_EVENTS.md.",
    version=VERSION)

app.include_router(dashboard_router)

# La page du dashboard : des fichiers statiques, sans etape de build. Les
# donnees arrivent par /api/v1/pc/* (api/dashboard.py).
DASHBOARD_DIR = Path(__file__).parent / "static" / "dashboard"
app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True),
          name="dashboard")


@app.get("/", include_in_schema=False)
def racine() -> RedirectResponse:
    return RedirectResponse("/dashboard/")

_CLE = settings.api_key()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Secret partage dans l'en-tete X-API-Key.

    compare_digest plutot que == : la comparaison ne doit pas fuiter, par
    son temps de reponse, combien de caracteres sont justes.
    """
    if not x_api_key or not secrets.compare_digest(x_api_key, _CLE):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "X-API-Key absente ou invalide")


@app.get("/health", tags=["etat"])
def health() -> dict:
    """Sans authentification : le tracker distingue ainsi "mauvaise
    adresse" de "mauvaise cle". Ne revele que des etats."""
    return {"status": "ok", "service": "chronicle", "version": VERSION,
            "database": "ok" if check_connection() else "unavailable",
            "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/v1/events", tags=["ingestion"],
          dependencies=[Depends(require_api_key)])
def ingest(corps: dict[str, Any] = Body(...)) -> dict:
    """Un lot d'evenements. 2xx = ecrit en base, brut compris."""
    try:
        return ingest_batch(corps, max_events=settings.MAX_EVENTS)

    except IngestError as erreur:
        raise HTTPException(erreur.status, erreur.detail)

    except OperationalError as erreur:
        # Base injoignable : 503, le tracker reessaiera plus tard.
        logger.warning("base indisponible : %s", erreur.orig)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "base de donnees indisponible")

    except SQLAlchemyError as erreur:
        logger.exception("ecriture impossible")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"ecriture impossible : {type(erreur).__name__}")


@app.get("/api/v1/devices", tags=["etat"],
         dependencies=[Depends(require_api_key)])
def devices() -> list[dict]:
    """Les machines connues et leur fraicheur."""
    try:
        with session_scope() as session:
            lignes = session.execute(
                select(Source.code, Source.label, SyncState.last_success_at,
                       SyncState.cursor_at)
                .join(SyncState, SyncState.source_id == Source.id,
                      isouter=True)
                .where(Source.code.like("pc:%"))
                .order_by(Source.code)).all()
    except OperationalError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "base de donnees indisponible")

    return [{"device_id": code.split(":", 1)[1], "label": label,
             "last_batch_at": dernier.isoformat() if dernier else None,
             "data_until": curseur.isoformat() if curseur else None}
            for code, label, dernier, curseur in lignes]


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(app, host=host or settings.HOST, port=port or settings.PORT,
                log_config=None, access_log=False)
