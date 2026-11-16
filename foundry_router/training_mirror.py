"""Records from other machines, centralised on the router (v0.181.162).

The router already holds every model call it routed. The rest of the record —
what the coding harness did (Kilo), what the coordinator decided, what was
committed (git) — is made on the workstation, and the router cannot read a
workstation's database or checkout. So the workstation sends those records here:

    POST /v1/foundry/training/events   {"source": "kilo", "cursor": "...",
                                        "events": [ ...normalized events... ]}
    GET  /v1/foundry/training/events/cursor?source=kilo

A batch is written straight to a spool file (`<data>/training/incoming/`) and
answered at once; the preparation worker (training_prep_worker.py) folds the
spool into the prepared store at the start of its next run, because that worker
owns the store and two writers on one SQLite file fight. The sender's `cursor`
is kept per source and handed back, so a sender that stops picks up where it
left off, and an event that arrives twice is ignored (its id is derived from the
source record).

The prepared store is what the nightly dataset is built from and what the
archive copies to the NAS, so a record that reaches here is centralised and
backed up without anything else running.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

MAX_EVENTS = 2000                   # per batch
MAX_BYTES = 32 * 1024 * 1024        # per batch, as JSON
KEEP_FAILED = 20                    # spool files that failed to ingest, kept for a look
_SOURCE = re.compile(r"^[a-z][a-z0-9_.-]{0,40}$")
DEFAULT_SENDER = "workstation"


def spool_dir(svc) -> Path:
    d = Path(svc.capture.data_dir) / "training" / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kv(sender: str, source: str) -> str:
    return f"training_mirror:{sender}:{source}"


def cursor(svc, source: str, sender: str = DEFAULT_SENDER) -> str:
    """Where this sender got to in this source. The sender is part of the key
    because the cursor is that sender's own position (a row number in its own
    store): two machines, or one machine's old and new store, count differently."""
    try:
        return str(svc.db.kv_get(_kv(sender or DEFAULT_SENDER, source)) or "")
    except Exception:                                             # noqa: BLE001
        return ""


def set_cursor(svc, source: str, value: str, sender: str = DEFAULT_SENDER) -> None:
    if value:
        svc.db.kv_set(_kv(sender or DEFAULT_SENDER, source), str(value))


def accept(svc, source: str, events: list, cur: str = "", sender: str = DEFAULT_SENDER) -> dict:
    """Write one batch to the spool. Returns what to tell the sender."""
    sender = sender or DEFAULT_SENDER
    if not _SOURCE.match(str(source or "")) or not _SOURCE.match(str(sender)):
        raise ValueError("source and sender must be short names like kilo, coordinator or git")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    if len(events) > MAX_EVENTS:
        raise ValueError(f"at most {MAX_EVENTS} events per batch")
    body = json.dumps({"source": source, "events": events}, ensure_ascii=False, default=str)
    if len(body) > MAX_BYTES:
        raise ValueError(f"a batch may be at most {MAX_BYTES // 2 ** 20} MB; send fewer events")
    d = spool_dir(svc)
    name = f"{sender}-{source}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json.gz"
    tmp = d / (name + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:             # compressed: these are large
        f.write(body)
    os.replace(tmp, d / name)
    set_cursor(svc, source, cur, sender)
    return {"accepted": len(events), "cursor": cursor(svc, source, sender), "spooled": name}


def pending(svc_or_dir) -> list:
    d = svc_or_dir if isinstance(svc_or_dir, Path) else spool_dir(svc_or_dir)
    return sorted(Path(p) for p in glob.glob(str(d / "*.json.gz")))


def waiting(svc) -> dict:
    """What the admin page shows: how much is spooled, not yet folded in."""
    files = pending(svc)
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files if f.exists()),
            "oldest": min((f.stat().st_mtime for f in files if f.exists()), default=None)}


def ingest(store, d: Path, log=lambda s: None, stop=lambda: False) -> dict:
    """Fold every spooled batch into `store` (the preparation worker's own store).
    A file that cannot be read is renamed .bad and left behind, never retried
    forever. Returns counts."""
    from foundry_training.schema import Event
    fields = set(Event.__dataclass_fields__)
    out = {"files": 0, "events": 0, "new": 0, "bad": 0, "sources": {}}
    for p in pending(d):
        if stop():
            break
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                batch = json.load(f)
            source = str(batch.get("source") or "")
            rows = [e for e in (batch.get("events") or []) if isinstance(e, dict)]
            events = []
            for e in rows:
                e = {k: v for k, v in e.items() if k in fields}
                e.setdefault("source_adapter", source)
                events.append(Event(**e))
            new, _old = store.put_events(events)
        except Exception as e:                                    # noqa: BLE001
            out["bad"] += 1
            log(f"mirror: {p.name} could not be folded in ({type(e).__name__}: {e}); kept as .bad")
            try:
                os.replace(p, p.with_suffix(p.suffix + ".bad"))
            except OSError:
                pass
            _trim_bad(d)
            continue
        out["files"] += 1
        out["events"] += len(events)
        out["new"] += new
        s = out["sources"].setdefault(source or "?", {"events": 0, "new": 0})
        s["events"] += len(events)
        s["new"] += new
        try:
            p.unlink()
        except OSError:
            pass
    if out["files"]:
        log(f"mirror: {out['new']:,} new event(s) from {out['files']} batch(es) "
            + ", ".join(f"{k} {v['new']:,}" for k, v in sorted(out["sources"].items())))
    return out


def _trim_bad(d: Path) -> None:
    bad = sorted(Path(p) for p in glob.glob(str(d / "*.bad")))
    for p in bad[:-KEEP_FAILED]:
        try:
            p.unlink()
        except OSError:
            pass


def status(svc) -> dict:
    """Per source: where the sender got to, and what is still spooled."""
    out = {"waiting": waiting(svc), "sources": {}}
    try:
        for r in svc.db.query("SELECT key, value FROM kv WHERE key LIKE 'training_mirror:%'"):
            parts = r["key"].split(":")
            if len(parts) == 3:
                out["sources"].setdefault(parts[2], {})[parts[1]] = r["value"]
    except Exception:                                             # noqa: BLE001
        pass
    return out
