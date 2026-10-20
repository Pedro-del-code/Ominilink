"""Registro de eventos: redação de segredos, trilha de auditoria em memória e configuração de logging."""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections import deque

_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (re.compile(r"\bdev_[0-9a-f]{16}\.[A-Za-z0-9_-]{20,}"), "dev_***.***"),
    (re.compile(r"(?i)\b(token|secret|password|enrollment[_-]?key|authorization)(\s*[=:]\s*)([^\s,;&\"']+)"), r"\1\2***"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Mascara credenciais em qualquer registro de log antes da emissão."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_format: bool = False) -> None:
    root = logging.getLogger("omnilink")
    root.setLevel(level.upper())
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(_JsonFormatter() if json_format else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.propagate = False


class AuditLog:
    """Trilha de auditoria circular em memória (eventos relevantes de segurança e entrega)."""

    def __init__(self, maxlen: int = 500):
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._log = logging.getLogger("omnilink.audit")

    def emit(self, event: str, **fields) -> None:
        entry = {"ts": round(time.time(), 3), "event": event, **fields}
        with self._lock:
            self._events.append(entry)
        self._log.info("event=%s %s", event, " ".join(f"{k}={v}" for k, v in fields.items()))

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._events)[-limit:]
