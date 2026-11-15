"""Rebuild a training store with each message stored once (v0.181.145).

    python -m foundry_training.compact <old store> <new store> [--verify-every N]
                                       [--limit N] [--with-datasets]

Reads the old store READ-ONLY and writes a new one beside it:

  events, raw_records   every row, its text replaced by references into
                        <new>/blobs.sqlite (blobs.py)
  correlations, gaps, checkpoints, ingest_runs, artifacts, artifact_refs
                        copied as they are
  curated.sqlite        copied whole (SQLite's backup API)
  artifacts/            files copied
  datasets/, eval/, library/   only with --with-datasets

It can be stopped and started again: it continues after the last row it wrote
(checkpoints `compact:events`, `compact:raw`), so it also catches up with rows
the old store received in the meantime. Every `--verify-every`-th row is read
back from the new store and compared with the old one; a difference stops the
run. Nothing in the old store is changed or deleted, here or anywhere else.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .store import TrainingStore

SMALL = ("correlations", "gaps", "ingest_runs", "artifacts", "artifact_refs")


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _size(root: Path) -> int:
    n = 0
    for name in ("normalized.sqlite", "raw/raw.sqlite", "curated.sqlite", "blobs.sqlite"):
        p = root / name
        for q in (p, Path(str(p) + "-wal")):
            if q.exists():
                n += q.stat().st_size
    return n


def _copy_rows(src: sqlite3.Connection, new_db, table: str, text_col: str, key: str, store,
               batch: int, verify_every: int, limit: int, log) -> dict:
    """events / raw_records: every row after the checkpoint, its text packed."""
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    if table == "raw_records":
        cols = [c for c in cols if c != "id"]            # the new store numbers its own
    last = int(store.checkpoint(key) or 0)
    top = src.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0] or 0
    out = {"table": table, "from": last, "to": top, "rows": 0, "verified": 0, "bytes_in": 0}
    sql = (f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES "
           f"({', '.join('?' * len(cols))})")
    t0 = t_log = time.time()
    while last < top and (not limit or out["rows"] < limit):
        rows = src.execute(f"SELECT rowid AS _rid, {', '.join(cols)} FROM {table} "
                           f"WHERE rowid > ? ORDER BY rowid LIMIT ?", (last, batch)).fetchall()
        if not rows:
            break
        pending: dict = {}
        packed_rows, checks = [], []
        for r in rows:
            d = dict(r)
            text = d[text_col] or "{}"
            out["bytes_in"] += len(text)
            try:
                value = json.loads(text)
            except ValueError:
                value = None                              # not JSON: kept exactly as it is
            if value is not None:
                packed = store.blobs._walk(value, pending)
                d[text_col] = json.dumps(packed, ensure_ascii=False)
                if verify_every and (d["_rid"] % verify_every == 0):
                    checks.append((d["_rid"], value, packed))
            packed_rows.append([d[c] for c in cols])
        store.blobs.flush(pending)
        new_db.many(sql, packed_rows)
        for rid, value, packed in checks:
            if store.blobs.unpack(packed) != value:
                raise RuntimeError(f"{table} row {rid}: the new store does not give back what "
                                   f"the old one holds; stopped, nothing was deleted")
            out["verified"] += 1
        last = rows[-1]["_rid"]
        out["rows"] += len(rows)
        store.set_checkpoint(key, str(last))
        if time.time() - t_log > 30:
            t_log = time.time()
            rate = out["bytes_in"] / max(1.0, time.time() - t0) / 1e6
            log(f"  {table}: row {last:,} of {top:,} ({100 * last / max(1, top):.1f}%), "
                f"{out['bytes_in'] / 1e9:.1f} GB read, {rate:.0f} MB/s, "
                f"{out['verified']} verified")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def compact(old: str, new: str, batch: int = 100, verify_every: int = 200, limit: int = 0,
            with_datasets: bool = False, log=print) -> dict:
    old_root = Path(os.path.expanduser(old)).resolve()
    new_root = Path(os.path.expanduser(new)).resolve()
    if old_root == new_root:
        raise ValueError("the new store must be a different folder from the old one")
    if not (old_root / "normalized.sqlite").exists():
        raise ValueError(f"{old_root} is not a training store (no normalized.sqlite)")
    store = TrainingStore(str(new_root), allow_in_repo=True)
    store.packing = True
    report: dict = {"old": str(old_root), "new": str(new_root), "old_bytes": _size(old_root)}
    norm = _ro(old_root / "normalized.sqlite")
    try:
        log(f"events: {old_root / 'normalized.sqlite'}")
        report["events"] = _copy_rows(norm, store.norm, "events", "facts", "compact:events",
                                      store, batch, verify_every, limit, log)
        for t in SMALL:
            cols = [r[1] for r in norm.execute(f"PRAGMA table_info({t})")]
            rows = norm.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
            store.norm.many(f"INSERT OR REPLACE INTO {t} ({', '.join(cols)}) VALUES "
                            f"({', '.join('?' * len(cols))})", [tuple(r) for r in rows])
            report[t] = len(rows)
        rows = norm.execute("SELECT source_key, cursor, updated_ts FROM checkpoints "
                            "WHERE source_key NOT LIKE 'compact:%'").fetchall()
        store.norm.many("INSERT OR REPLACE INTO checkpoints (source_key, cursor, updated_ts) "
                        "VALUES (?,?,?)", [tuple(r) for r in rows])
        report["checkpoints"] = len(rows)
    finally:
        norm.close()
    raw_path = old_root / "raw" / "raw.sqlite"
    if raw_path.exists():
        raw = _ro(raw_path)
        try:
            log(f"raw records: {raw_path}")
            report["raw_records"] = _copy_rows(raw, store.raw, "raw_records", "payload",
                                               "compact:raw", store, batch, verify_every,
                                               limit, log)
        finally:
            raw.close()
    cur_path = old_root / "curated.sqlite"
    if cur_path.exists():
        src = _ro(cur_path)
        try:
            with store.cur.lock:
                src.backup(store.cur.conn)
        finally:
            src.close()
        report["curated"] = "copied"
    copied = 0
    for sub in ("artifacts",) + (("datasets", "eval", "library") if with_datasets else ()):
        s, d = old_root / sub, new_root / sub
        if not s.exists():
            continue
        for path in s.rglob("*"):
            if path.is_file():
                target = d / path.relative_to(s)
                if not target.exists() or target.stat().st_size != path.stat().st_size:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    copied += 1
    report["files_copied"] = copied
    for db in (store.norm, store.raw, store.cur, store.blobs.db):
        with db.lock:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report["blobs"] = store.blobs.count()
    report["new_bytes"] = _size(new_root)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_training.compact", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--verify-every", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows per table (a trial)")
    ap.add_argument("--with-datasets", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = compact(a.old, a.new, verify_every=a.verify_every, limit=a.limit,
                    with_datasets=a.with_datasets,
                    log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=1))
    print(f"old {r['old_bytes'] / 1e9:.1f} GB -> new {r['new_bytes'] / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
