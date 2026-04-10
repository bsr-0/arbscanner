"""Logging configuration for arbscanner.

This module centralizes logging setup for the arbscanner package. It supports
two output modes:

1. Pretty console mode (default): human-readable one-line log records using
   the format ``"%(asctime)s %(levelname)-8s %(name)s: %(message)s"``.
2. JSON mode: one JSON object per line, suitable for log aggregators and
   structured ingestion pipelines.

The module uses only the Python standard library so it adds no dependencies.
Environment variables ``ARBSCANNER_LOG_LEVEL`` and ``ARBSCANNER_LOG_JSON``
can override the programmatic arguments to :func:`setup_logging`, which
makes it easy to tune logging in deployed environments without code changes.

Example::

    from arbscanner.logging_config import setup_logging, get_logger

    setup_logging(level="DEBUG", json_output=True)
    log = get_logger(__name__)
    log.info("scanner started", extra={"pairs": 42})
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys

# Standard attributes present on every ``logging.LogRecord``. We skip these
# when serializing "extra" fields in :class:`JsonFormatter` so that only
# user-supplied extras end up in the JSON payload.
_STANDARD_LOGRECORD_FIELDS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

# Loggers that are chatty by default and should be quieted to WARNING unless
# the caller explicitly opts in to seeing their output.
_DEFAULT_QUIET_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "uvicorn.access",
    "transformers",
)


class JsonFormatter(logging.Formatter):
    """Format log records as one-line JSON objects.

    Each emitted record has the fields ``ts`` (UTC ISO-8601 timestamp),
    ``level``, ``logger``, and ``msg``. Any additional attributes supplied
    via ``logger.info("...", extra={...})`` are merged into the top-level
    object, skipping standard ``LogRecord`` internals.

    Exception information, when present, is rendered into an ``exc_info``
    string field using the default traceback formatting.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize ``record`` into a single-line JSON string."""
        payload: dict[str, object] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Merge user-supplied "extra" fields, skipping LogRecord internals.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_FIELDS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = _coerce_json_safe(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def _coerce_json_safe(value: object) -> object:
    """Best-effort coercion of ``value`` into something ``json.dumps`` likes.

    Primitives and common containers pass through untouched; anything else
    falls back to its ``repr`` so the logger never raises on exotic types.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce_json_safe(v) for k, v in value.items()}
    return repr(value)


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    quiet_loggers: list[str] | None = None,
) -> None:
    """Configure the root logger for arbscanner.

    This is idempotent: calling it repeatedly will tear down any existing
    handlers on the root logger before installing a fresh ``StreamHandler``
    pointing at ``sys.stderr``.

    Args:
        level: Log level name such as ``"INFO"``, ``"DEBUG"``, ``"WARNING"``.
            Case-insensitive. Invalid values fall back to ``INFO``.
        json_output: When ``True``, emit JSON lines via :class:`JsonFormatter`.
            Otherwise use a human-readable text format.
        quiet_loggers: Additional logger names to clamp at ``WARNING``. These
            are added on top of the built-in defaults (``httpx``, ``httpcore``,
            ``urllib3``, ``uvicorn.access``, ``transformers``).

    Environment overrides:
        ``ARBSCANNER_LOG_LEVEL`` overrides ``level`` when set.
        ``ARBSCANNER_LOG_JSON`` overrides ``json_output`` when set to one of
        ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-insensitive).
    """
    # Env var overrides take precedence over programmatic args.
    env_level = os.environ.get("ARBSCANNER_LOG_LEVEL")
    if env_level:
        level = env_level

    env_json = os.environ.get("ARBSCANNER_LOG_JSON")
    if env_json is not None:
        json_output = env_json.strip().lower() in {"1", "true", "yes", "on"}

    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    # Idempotent: clear existing handlers so repeat calls don't stack up.
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    handler = logging.StreamHandler(stream=sys.stderr)
    formatter: logging.Formatter
    if json_output:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        )
    handler.setFormatter(formatter)
    handler.setLevel(numeric_level)

    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Clamp chatty loggers.
    quiet: list[str] = list(_DEFAULT_QUIET_LOGGERS)
    if quiet_loggers:
        quiet.extend(quiet_loggers)
    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for ``name``.

    This is a thin convenience wrapper around :func:`logging.getLogger` so
    callers can import a single symbol from this module for both setup and
    usage.
    """
    return logging.getLogger(name)


if __name__ == "__main__":
    # Small demo that exercises both output modes. Run with:
    #     python -m arbscanner.logging_config
    print("--- pretty console mode ---", file=sys.stderr)
    setup_logging(level="DEBUG", json_output=False)
    demo_log = get_logger("arbscanner.demo")
    demo_log.debug("debug message")
    demo_log.info("scanner started", extra={"pairs": 42, "exchange": "kalshi"})
    demo_log.warning("slow response", extra={"latency_ms": 1234})
    try:
        raise RuntimeError("simulated failure")
    except RuntimeError:
        demo_log.exception("unhandled error in demo")

    print("--- json mode ---", file=sys.stderr)
    setup_logging(level="DEBUG", json_output=True)
    demo_log = get_logger("arbscanner.demo")
    demo_log.debug("debug message")
    demo_log.info("scanner started", extra={"pairs": 42, "exchange": "kalshi"})
    demo_log.warning("slow response", extra={"latency_ms": 1234})
    try:
        raise RuntimeError("simulated failure")
    except RuntimeError:
        demo_log.exception("unhandled error in demo")
