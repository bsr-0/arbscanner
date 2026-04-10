"""Health, readiness, and liveness probe endpoints for arbscanner.

This module exposes three HTTP probes commonly required by container
orchestrators (Docker, Kubernetes) and load balancers:

- Liveness (``GET /health`` and ``GET /live``): a lightweight check that
  the process is running and able to serve HTTP. It never touches
  external dependencies and always returns 200 when the server is up.
  Orchestrators use this to decide whether to restart a crashed pod.

- Readiness (``GET /ready``): a dependency check that verifies the
  service is prepared to serve real traffic. It validates that the
  SQLite database is reachable and that the matched-pairs cache is
  populated. Returns 503 when any dependency is missing so that load
  balancers can route traffic away until the service is ready.

The three probes are exposed via a module-level ``APIRouter`` that can
be included in the main FastAPI application. This module is
self-contained and has no runtime dependency on ``web.py``; it can be
imported and mounted independently.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from arbscanner.config import DB_PATH
from arbscanner.matcher import load_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["health"])


def get_version() -> str:
    """Return the installed arbscanner package version.

    Falls back to ``"0.0.0-dev"`` when the package does not expose a
    ``__version__`` attribute (e.g. during local development).
    """
    try:
        import arbscanner

        version = getattr(arbscanner, "__version__", None)
        if isinstance(version, str) and version:
            return version
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to read arbscanner.__version__")
    return "0.0.0-dev"


def _check_database() -> tuple[bool, str]:
    """Verify that the SQLite database is reachable.

    Opens a fresh connection to ``DB_PATH`` and executes ``SELECT 1``.
    Returns ``(ok, detail)`` where ``detail`` is an error message on
    failure or an empty string on success.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        cursor = conn.execute("SELECT 1")
        row = cursor.fetchone()
        if row is None or row[0] != 1:
            return False, "unexpected response from SELECT 1"
        return True, ""
    except Exception as exc:
        logger.exception("Database readiness check failed")
        return False, f"database error: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to close readiness-check connection")


def _check_matched_pairs() -> tuple[bool, str, int]:
    """Verify that the matched-pairs cache is populated.

    Returns ``(ok, detail, count)``. ``ok`` is ``False`` when the cache
    is empty or fails to load; ``detail`` carries a human-readable
    reason and ``count`` is the number of pairs found.
    """
    try:
        cache = load_cache()
    except Exception as exc:
        logger.exception("Matched pairs readiness check failed")
        return False, f"failed to load matched pairs cache: {exc}", 0

    count = len(cache.pairs)
    if count == 0:
        return False, "no matched pairs; run arbscanner match", 0
    return True, "", count


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe.

    Always returns 200 with a minimal payload as long as the process
    can serve HTTP. No external dependencies are consulted.
    """
    return {
        "status": "alive",
        "service": "arbscanner",
        "version": get_version(),
    }


@router.get("/live")
def live() -> dict[str, str]:
    """Liveness probe alias for Kubernetes compatibility.

    Behaves identically to :func:`health`; exposed under ``/live`` so
    that operators can use the conventional probe path.
    """
    return health()


@router.get("/ready")
def ready() -> JSONResponse:
    """Readiness probe.

    Checks that all downstream dependencies are available. Returns 200
    when every check passes and 503 otherwise so that load balancers
    and orchestrators can temporarily remove the instance from
    rotation.
    """
    db_ok, db_detail = _check_database()
    pairs_ok, pairs_detail, pairs_count = _check_matched_pairs()

    details: dict[str, Any] = {
        "version": get_version(),
        "matched_pairs_count": pairs_count,
    }
    if db_detail:
        details["database"] = db_detail
    if pairs_detail:
        details["matched_pairs"] = pairs_detail

    all_ok = db_ok and pairs_ok
    body: dict[str, Any] = {
        "status": "ready" if all_ok else "not_ready",
        "checks": {
            "database": db_ok,
            "matched_pairs": pairs_ok,
            "details": details,
        },
    }

    status_code = 200 if all_ok else 503
    return JSONResponse(status_code=status_code, content=body)
