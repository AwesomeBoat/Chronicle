"""Configuration commune des tests.

    python -m pytest tests/                      tout ce qui peut tourner
    python -m pytest tests/pc_tracker            le tracker seul, sans base

LA BASE DE TEST N'EST JAMAIS LA VRAIE
-------------------------------------
Les tests qui ecrivent en base (tests/chronicle, tests/integration) ne
tournent que si une base de test est EXPLICITEMENT designee :

    docker run -d --rm --name chronicle_pc_test -p 5433:5432 \\
        -e POSTGRES_DB=chronicle_test -e POSTGRES_USER=twin \\
        -e POSTGRES_PASSWORD=test_only postgres:16

    CHRONICLE_TEST_POSTGRES_HOST=localhost
    CHRONICLE_TEST_POSTGRES_PORT=5433
    CHRONICLE_TEST_POSTGRES_DB=chronicle_test
    CHRONICLE_TEST_POSTGRES_USER=twin
    CHRONICLE_TEST_POSTGRES_PASSWORD=test_only

Sans ces variables, ils sont sautes. Une base nommee `digitaltwin` (celle
de docker-compose.yml, les vraies donnees) est refusee d'office : les
tests vident les tables entre deux cas.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]

if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

_CLES = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
         "POSTGRES_PASSWORD")
TEST_DB = {cle: os.environ.get(f"CHRONICLE_TEST_{cle}") for cle in _CLES}
HAS_TEST_DB = all(TEST_DB.values())

if HAS_TEST_DB:
    if TEST_DB["POSTGRES_DB"] == "digitaltwin":
        raise RuntimeError("Refus : `digitaltwin` est la base reelle. "
                           "Utiliser une base de test dediee.")

    # AVANT tout import de config.py : load_dotenv() n'ecrase pas une
    # variable deja presente, donc c'est la base de test qui gagne.
    os.environ.update(TEST_DB)

# config.py exige ces variables ; une machine sans .env (CI, Omarchy) doit
# pouvoir lancer les tests.
for _cle in ("POLAR_CLIENT_ID", "POLAR_CLIENT_SECRET", "POLAR_REDIRECT_URI"):
    os.environ.setdefault(_cle, "test")

os.environ.setdefault("POSTGRES_PASSWORD", "test_only")
os.environ["CHRONICLE_API_KEY"] = "test-key"

requires_db = pytest.mark.skipif(
    not HAS_TEST_DB, reason="base de test non designee (CHRONICLE_TEST_*)")

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="Windows uniquement")


@pytest.fixture
def pc_home(tmp_path, monkeypatch):
    """Un dossier de tracker jetable (configuration, file, journaux)."""
    monkeypatch.setenv("CHRONICLE_PC_HOME", str(tmp_path / "pc-home"))
    return tmp_path / "pc-home"
