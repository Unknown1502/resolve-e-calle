"""Structured logging with secret redaction.

``docs/security-privacy.md`` asks for operational metadata in logs, not
transcripts and not full phone numbers. The redaction filter below is a
backstop, not a licence to log carelessly: it catches API keys and
E.164 numbers that slip through in a message or an extra field.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar, Token
from typing import Any

#: Correlation fields promoted to top-level keys when present, so a
#: workflow can be followed across the API, the worker and the provider
#: (master prompt, "Observability").
CORRELATION_FIELDS = (
    "request_id",
    "exception_id",
    "workflow_run_id",
    "batch_id",
    "attempt_no",
    "provider_call_id",
    "reason_code",
    "decision",
)

_SECRET_PATTERNS = (
    # CALL-E keys look like iams_live_… / iams_test_…
    re.compile(r"\biams_[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b(api[_-]?key|secret|token|password)\s*[=:]\s*\S+", re.IGNORECASE),
)
_PHONE = re.compile(r"\+[1-9]\d{6,14}")

_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    # Keep the country code and last two digits: enough to recognise a
    # number in an incident, not enough to redistribute it.
    return _PHONE.sub(lambda m: f"{m.group()[:3]}{'*' * 6}{m.group()[-2:]}", text)


# ── request correlation ──────────────────────────────────────────────
#
# A context variable rather than an argument threaded through every
# call: the point of a request id is that code deep in the policy engine
# gets correlated for free, without knowing an HTTP layer exists.

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str | None) -> Token[str | None]:
    """Bind a request id for the current context. Returns a reset token."""
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def get_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


class RequestIdFilter(logging.Filter):
    """Stamp the ambient request id onto every record that lacks one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id") and (rid := _request_id.get()):
            record.request_id = rid
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD:
                continue
            payload[key] = redact(value) if isinstance(value, str) else value

        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))

        ordered = {k: payload.pop(k) for k in ("ts", "level", "logger", "message")}
        for field in CORRELATION_FIELDS:
            if field in payload:
                ordered[field] = payload.pop(field)
        ordered.update(payload)
        return json.dumps(ordered, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Access logs duplicate what we already record, at a lower quality.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
