"""In-memory ring buffer of application log records, for the admin Dev-Log view.

Python exceptions/log lines normally go only to the container's stderr — you'd
have to SSH + read docker logs to see a traceback. This logging.Handler keeps
the last N records (with formatted tracebacks) in memory, exposed via an
endpoint the UI can filter, search, and live-tail. It's a *diagnostic* view:
capped and reset on restart. Persistent routing/MCP history stays in the DB
event_log (the Events tab).
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from itertools import count

_LEVELNO = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = 800):
        super().__init__()
        self._records: deque = deque(maxlen=capacity)
        self._seq = count(1)
        self._lock = threading.Lock()
        self._max_id = 0

    def emit(self, record: logging.LogRecord) -> None:
        # A logging handler must never raise — swallow everything.
        try:
            tb = ""
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
            entry = {
                "id": next(self._seq),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "traceback": tb,
            }
            with self._lock:
                self._records.append(entry)
                self._max_id = entry["id"]
        except Exception:
            pass

    def max_id(self) -> int:
        return self._max_id

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def snapshot(self, after: int = 0, level: str | None = None,
                 q: str | None = None, limit: int = 500) -> list[dict]:
        """Records newer than `after`, at or above `level` (minimum severity),
        matching free-text `q` (message / logger / traceback). Newest last."""
        with self._lock:
            items = list(self._records)
        minlv = _LEVELNO.get((level or "").upper(), 0)
        ql = (q or "").lower()
        out = []
        for e in items:
            if e["id"] <= after:
                continue
            if minlv and _LEVELNO.get(e["level"], 0) < minlv:
                continue
            if ql and ql not in e["message"].lower() and ql not in e["logger"].lower() \
                    and ql not in e["traceback"].lower():
                continue
            out.append(e)
        return out[-limit:]
