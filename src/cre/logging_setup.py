"""Structured JSON logging."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from cre.config import Settings
from cre.request_id import request_id_var
from cre.telemetry import current_trace_ids

_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str, commit: str) -> None:
        super().__init__()
        self.service = service
        self.version = version
        self.commit = commit

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_trace_ids()
        reserved: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "version": self.version,
            "commit": self.commit,
            "request_id": request_id_var.get(),
            "trace_id": trace_id,
            "span_id": span_id,
        }
        payload: dict[str, Any] = {
            key: value
            for key, value in vars(record).items()
            if key not in _STANDARD_RECORD_ATTRS and key not in reserved
        }
        payload.update(reserved)
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _CreHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker type so configure_logging can replace only its own handlers."""


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _CreHandler)]:
        root.removeHandler(existing)
    handler = _CreHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            service=settings.service_name, version=settings.version, commit=settings.commit
        )
    )
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
