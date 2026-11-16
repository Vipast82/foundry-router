"""The worker behind training_prep.py (v0.181.159): a process of its own.

    python -m foundry_router.training_prep_worker --capture <capture.sqlite>
        --store <dir> --out <dir> --status <file> [--router-db <foundry.sqlite>]
        [--unbound chat] [--max-tokens 32768] [--pause 0.02]

It reads the router's capture store (read-only, a few hundred records at a time,
never one long read), keeps what it has read in a training store of its own
(each message once), and writes the keep-everything dataset from it
(foundry_training.sessions). A process, not a thread: unpacking and re-reading
hundreds of thousands of conversations is CPU work, and in a thread it would
take the router's interpreter from the chats it is serving. It runs at low
priority, rests between batches, picks up where the last run stopped, and stops
when its status file's folder gains a `prep.stop` file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PAGE = 200


def capture_rows(path: str, after_id: int, pause: float, should_stop):
    """Capture records after `after_id`, oldest first, PAGE at a time: each page is
    its own short read, so the router's store is never held open for the run."""
    from foundry_training.blobs import BlobStore
    while not should_stop():
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM capture_records WHERE id > ? ORDER BY id LIMIT ?", (after_id, PAGE))]
            if not rows:
                return
            unpack = lambda v: v                                  # noqa: E731
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='blobs'").fetchone():
                class _Ro:
                    def q(self, sql, args=()):
                        return [dict(x) for x in c.execute(sql, tuple(args)).fetchall()]
                unpack = BlobStore(_Ro()).unpack
            for d in rows:
                d["body"] = unpack(json.loads(d["body"] or "{}"))
                d["redaction"] = json.loads(d["redaction"] or "{}")
        finally:
            c.close()
        for d in rows:
            yield d
        after_id = rows[-1]["id"]
        if pause:
            time.sleep(pause * len(rows))


def outcome_rows(db_path: str, after_id: int):
    if not db_path or not os.path.exists(db_path):
        return
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='training_outcomes'").fetchone():
            return
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM training_outcomes WHERE id > ? ORDER BY id", (after_id,))]
    finally:
        c.close()
    for r in rows:
        try:
            r["evidence"] = json.loads(r["evidence"]) if isinstance(r.get("evidence"), str) else r.get("evidence")
        except ValueError:
            pass
        yield r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_router.training_prep_worker")
    ap.add_argument("--capture", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--status", required=True)
    ap.add_argument("--router-db", default="")
    ap.add_argument("--unbound", default="chat")
    ap.add_argument("--name", default="all-sessions")
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--pause", type=float, default=0.02)
    a = ap.parse_args(argv)
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    status_path = Path(a.status)
    stop_file = status_path.parent / "prep.stop"
    st = {"state": "running", "phase": "reading", "started": time.time(), "pid": os.getpid(),
          "records": 0, "new_events": 0, "error": ""}
    last_write = [0.0]

    def save(force=False):
        if not force and time.time() - last_write[0] < 2:
            return
        last_write[0] = time.time()
        st["at"] = time.time()
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, status_path)

    def should_stop():
        return stop_file.exists()

    save(True)
    try:
        from foundry_training import sessions
        from foundry_training.adapters import foundry
        from foundry_training.ingest import Ingestor
        from foundry_training.store import TrainingStore
        store = TrainingStore(a.store, allow_in_repo=True)
        ing = Ingestor(store)
        key = "foundry:capture"
        st["from"] = int(store.checkpoint(key) or 0)

        def counted(rows):
            for r in rows:
                st["records"] += 1
                st["through"] = r["id"]
                save()
                yield r
        c = ing.run(key, foundry.records(counted(capture_rows(a.capture, st["from"], a.pause, should_stop)),
                                         None, a.unbound),
                    foundry.normalize, cursor_of=lambda rid, payload: payload.get("id") or 0)
        st["new_events"] = c.get("new_events", 0)
        st["skipped"] = c.get("skipped", 0)
        okey = "foundry-outcomes:router"
        o = ing.run(okey, foundry.outcome_records(outcome_rows(a.router_db, int(store.checkpoint(okey) or 0))),
                    foundry.normalize_outcome, cursor_of=lambda rid, payload: payload.get("id") or 0)
        st["outcomes"] = o.get("new_events", 0)
        if should_stop():
            st.update(state="stopped", phase="", finished=time.time())
            save(True)
            return 0
        st["phase"] = "writing the dataset"
        save(True)
        m = sessions.export(store, store.projects(), name=a.name, max_tokens=a.max_tokens,
                            out_root=a.out, log=lambda s: None)
        st.update(state="done", phase="", finished=time.time(),
                  dataset={k: m.get(k) for k in ("path", "sessions", "conversations", "examples",
                                                 "tokens", "tiers", "skipped", "files")})
        save(True)
        return 0
    except BaseException as e:                                    # noqa: BLE001
        import traceback
        st.update(state="error", phase="", finished=time.time(),
                  error=f"{type(e).__name__}: {e}"[:600], trace=traceback.format_exc()[-1200:])
        save(True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
