"""In-memory Dev-Log ring buffer (Phase 3): captures app logs + tracebacks that
otherwise only reach stderr, exposed for the UI to filter, search, and live-tail."""

import logging
import sys

from foundry_router.logbuffer import RingLogHandler


def _rec(level, name, msg, exc=None):
    return logging.LogRecord(name, level, "f", 1, msg, None, exc)


def test_snapshot_level_search_and_after():
    h = RingLogHandler()
    h.emit(_rec(logging.INFO, "foundry.a", "hello"))
    h.emit(_rec(logging.ERROR, "foundry.b", "boom happened"))
    allrecs = h.snapshot()
    assert [e["message"] for e in allrecs] == ["hello", "boom happened"]
    assert [e["level"] for e in h.snapshot(level="ERROR")] == ["ERROR"]   # min severity
    assert [e["level"] for e in h.snapshot(level="WARNING")] == ["ERROR"]  # WARNING+ incl ERROR
    assert len(h.snapshot(q="boom")) == 1 and len(h.snapshot(q="zzz")) == 0
    assert len(h.snapshot(q="foundry.b")) == 1                             # searches logger name
    first = allrecs[0]["id"]
    assert all(e["id"] > first for e in h.snapshot(after=first))          # incremental tail


def test_capacity_maxid_and_clear():
    h = RingLogHandler(capacity=2)
    for i in range(4):
        h.emit(_rec(logging.INFO, "x", f"m{i}"))
    assert [e["message"] for e in h.snapshot()] == ["m2", "m3"]           # oldest dropped
    assert h.max_id() == 4
    h.clear()
    assert h.snapshot() == [] and h.max_id() == 4                          # head id preserved


def test_captures_traceback():
    h = RingLogHandler()
    try:
        raise ValueError("kaboom")
    except ValueError:
        h.emit(_rec(logging.ERROR, "x", "failed", sys.exc_info()))
    tb = h.snapshot()[0]["traceback"]
    assert "Traceback" in tb and "ValueError: kaboom" in tb


# -- endpoint ---------------------------------------------------------------------

def test_devlog_endpoint_captures_search_and_clear(client):
    logging.getLogger("foundry.devtest").warning("unique-marker-xyz")
    d = client.get("/admin/api/devlog").json()
    assert any("unique-marker-xyz" in r["message"] for r in d["records"])
    assert d["max_id"] >= 1
    d2 = client.get("/admin/api/devlog", params={"q": "unique-marker-xyz"}).json()
    assert d2["records"] and all("unique-marker-xyz" in r["message"] for r in d2["records"])
    assert client.post("/admin/api/devlog/clear").json()["ok"] is True
    d3 = client.get("/admin/api/devlog").json()
    assert not any("unique-marker-xyz" in r["message"] for r in d3["records"])
