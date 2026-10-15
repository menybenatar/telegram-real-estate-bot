"""Logging with automatic secret redaction. Secrets must never reach a log line."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

# Telegram bot tokens look like 123456789:AAH... ; also scrub anything inside a /bot<token>/ URL.
_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b")
_BOT_URL_RE = re.compile(r"/bot[^/\s]+/")
_REGISTERED_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _REGISTERED_SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _BOT_URL_RE.sub("/bot***/", text)
    return _TOKEN_RE.sub("***", text)


class _Formatter(logging.Formatter):
    def __init__(self, as_json: bool):
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None) or {}
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = record.getMessage()
        exc = self.formatException(record.exc_info) if record.exc_info else None
        if self.as_json:
            payload = {"ts": timestamp, "level": record.levelname, "logger": record.name, "msg": message, **fields}
            if exc:
                payload["exc"] = exc
            line = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            extras = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in fields.items())
            line = f"{timestamp} {record.levelname:<7} {record.name} {message}" + (f" {extras}" if extras else "")
            if exc:
                line += "\n" + exc
        return redact(line)


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("LOG_FORMAT") or "text").lower()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(as_json=(fmt == "json")))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # urllib3 debug logs contain full request URLs (including the bot token) - keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def log_fields(**fields):
    """Helper: logger.info("msg", extra=log_fields(a=1)) adds structured fields."""
    return {"fields": fields}
