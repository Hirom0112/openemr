"""JSON structured logging + request_id propagation via contextvars.

The formatter emits a flat JSON object per record:
    {"ts","level","logger","msg", ...extras, "request_id"?}

Existing call sites use ``logger.info("msg", extra={...})``; ``extras`` are
merged into the top-level object so dashboards can filter by them directly.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from contextvars import ContextVar
from typing import Any, Final

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


# logging.LogRecord attributes that are NOT user-supplied extras. Everything
# else on the record came from the caller's extra={} dict and should be
# emitted at the top level of the JSON object.
_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Serialize a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = _coerce(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def _coerce(value: Any) -> Any:
    """Best-effort coercion of non-JSON-native values."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


class RequestIdFilter(logging.Filter):
    """Inject the current request_id ContextVar onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        if rid is not None and not hasattr(record, "request_id"):
            record.request_id = rid
        return True


def configure_json_logging(level: str | int) -> None:
    """Replace any existing handlers on the root logger with a JSON handler.

    Idempotent: safe to call from FastAPI startup (which may run twice in
    reload mode).
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(level)
