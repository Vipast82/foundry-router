"""Storage for the training pipeline: separate places for separate trust levels.

  <root>/raw/raw.sqlite          source payloads as ingested (already redacted),
                                 keyed by (adapter, source record, payload hash)
  <root>/normalized.sqlite       normalized events, correlations, capture gaps,
                                 ingestion checkpoints and runs
  <root>/curated.sqlite          episodes, selection decisions, splits, datasets,
                                 dataset membership (lineage), deletions
  <root>/datasets/<id>/          exported training datasets (+ manifest.json)
  <root>/eval/<id>/              evaluation datasets, never mixed with training
  <root>/artifacts/sha256/..     images and other binaries, content-addressed

Directories are created 0700 and database files 0600. The root may not be
inside a git working tree unless git ignores it (raw captures must never be
committed by accident). Nothing here is uploaded anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .schema import Event

NORMALIZED = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, schema_version INTEGER, project_id TEXT, repository_id TEXT,
  task_id TEXT, session_id TEXT, parent_session_id TEXT, request_id TEXT, tool_call_id TEXT,
  source_adapter TEXT, source_record_id TEXT, part TEXT, timestamp REAL, seq INTEGER,
  event_type TEXT, actor_role TEXT, actor_model TEXT, requested_persona TEXT,
  actual_backend TEXT, actual_model TEXT, source_revision TEXT, artifact_references TEXT,
  capture_status TEXT, redaction_status TEXT, verification_status TEXT, facts TEXT,
  inferred TEXT, raw_ref TEXT, ingested_ts REAL, deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_ev_session ON events(project_id, session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_task ON events(project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_ev_type ON events(event_type);
CREATE TABLE IF NOT EXISTS correlations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, project_id TEXT, a TEXT, b TEXT,
  method TEXT, confidence REAL, observed INTEGER, ambiguity TEXT, ts REAL,
  UNIQUE(kind, a, b));
CREATE TABLE IF NOT EXISTS gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, gap_key TEXT UNIQUE, project_id TEXT, session_id TEXT,
  source_adapter TEXT, kind TEXT, detail TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS checkpoints (
  source_key TEXT PRIMARY KEY, cursor TEXT, updated_ts REAL);
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT, started_ts REAL, finished_ts REAL,
  status TEXT, counts TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS artifacts (
  sha256 TEXT PRIMARY KEY, mime TEXT, width INTEGER, height INTEGER, bytes INTEGER,
  status TEXT, first_seen REAL, provenance TEXT);
CREATE TABLE IF NOT EXISTS artifact_refs (
  ref_id TEXT PRIMARY KEY, project_id TEXT, event_id TEXT, original TEXT, sha256 TEXT,
  status TEXT, detail TEXT);
"""
RAW = """
CREATE TABLE IF NOT EXISTS raw_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_adapter TEXT, source_record_id TEXT,
  project_id TEXT, payload_sha TEXT, ingested_ts REAL, redaction TEXT, provenance TEXT,
  payload TEXT, deleted INTEGER DEFAULT 0,
  UNIQUE(source_adapter, source_record_id, payload_sha));
"""
CURATED = """
CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, group_id TEXT, kind TEXT,
  status TEXT, split TEXT, outcome TEXT, sources TEXT, event_ids TEXT, content_hash TEXT,
  near_sig TEXT, reasons TEXT, data TEXT, created_ts REAL, updated_ts REAL, policy_hash TEXT,
  deleted INTEGER DEFAULT 0, duplicate_of TEXT);
CREATE INDEX IF NOT EXISTS idx_ep_project ON episodes(project_id, kind, status);
CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT PRIMARY KEY, kind TEXT, target TEXT, purpose TEXT, created_ts REAL,
  path TEXT, manifest TEXT, status TEXT, validation TEXT);
CREATE TABLE IF NOT EXISTS dataset_members (
  dataset_id TEXT, episode_id TEXT, example_count INTEGER, PRIMARY KEY (dataset_id, episode_id));
CREATE TABLE IF NOT EXISTS deletions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT, project_id TEXT, ts REAL, reason TEXT,
  affected_datasets TEXT, scope TEXT);
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, context_hash TEXT, chosen_event TEXT,
  rejected_event TEXT, basis TEXT, judge TEXT, ts REAL, UNIQUE(chosen_event, rejected_event));
"""


class StoreError(RuntimeError):
    pass


def _secure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


class _DB:
    def __init__(self, path: Path, schema: str):
        new = not path.exists()
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(schema)
        if new:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, tuple(args))
            return cur.rowcount

    def many(self, sql, rows):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                n = 0
                for r in rows:
                    n += self.conn.execute(sql, r).rowcount
                self.conn.execute("COMMIT")
                return n
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise


class TrainingStore:
    def __init__(self, root: str, allow_in_repo: bool = False):
        self.root = Path(os.path.expanduser(root)).resolve()
        self._check_not_committed(allow_in_repo)
        for d in ("", "raw", "datasets", "eval", "artifacts"):
            _secure_dir(self.root / d)
        self.raw = _DB(self.root / "raw" / "raw.sqlite", RAW)
        self.norm = _DB(self.root / "normalized.sqlite", NORMALIZED)
        self.cur = _DB(self.root / "curated.sqlite", CURATED)

    def _check_not_committed(self, allow: bool) -> None:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return
        if top.returncode != 0 or allow:
            return
        ign = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q",
                              str(self.root)], capture_output=True)
        if ign.returncode != 0:
            raise StoreError(f"{self.root} is inside the git work tree {top.stdout.strip()} "
                             "and not ignored; raw captures must never be committed. Put the "
                             "store elsewhere (default ~/.foundry-training) or ignore it.")

    # -- raw ---------------------------------------------------------------------

    def put_raw(self, source_adapter: str, source_record_id: str, project_id: str,
                payload: dict, redaction: dict, provenance: dict) -> str:
        from .schema import content_hash
        sha = content_hash(payload)
        self.raw.x("INSERT OR IGNORE INTO raw_records (source_adapter, source_record_id, "
                   "project_id, payload_sha, ingested_ts, redaction, provenance, payload) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (source_adapter, source_record_id, project_id, sha, time.time(),
                    json.dumps(redaction), json.dumps(provenance, default=str),
                    json.dumps(payload, default=str, ensure_ascii=False)))
        return f"raw:{source_adapter}:{source_record_id}:{sha[:16]}"

    # -- events ----------------------------------------------------------------------

    def put_events(self, events: Iterable[Event]) -> tuple[int, int]:
        """Insert-or-ignore by deterministic id. Returns (new, already present)."""
        rows, bad = [], []
        now = time.time()
        for e in events:
            probs = e.validate()
            if probs:
                bad.append((e.event_id, probs))
                continue
            r = e.row()
            r["ingested_ts"] = now
            rows.append(r)
        if bad:
            raise StoreError(f"invalid events: {bad[:3]}")
        if not rows:
            return 0, 0
        cols = list(rows[0])
        new = self.norm.many(f"INSERT OR IGNORE INTO events ({', '.join(cols)}) VALUES "
                             f"({', '.join('?' * len(cols))})", [[r[c] for c in cols] for r in rows])
        return new, len(rows) - new

    def events(self, project_id: str, where: str = "", args: tuple = ()) -> list[dict]:
        rows = self.norm.q(f"SELECT * FROM events WHERE project_id=? AND deleted=0 "
                           f"{('AND ' + where) if where else ''} ORDER BY timestamp, seq, part",
                           (project_id, *args))
        for r in rows:
            for k in ("facts", "inferred", "artifact_references"):
                r[k] = json.loads(r[k] or ("[]" if k == "artifact_references" else "{}"))
        return rows

    def projects(self) -> list[str]:
        return [r["project_id"] for r in self.norm.q(
            "SELECT DISTINCT project_id FROM events ORDER BY project_id")]

    # -- gaps, correlations, checkpoints ----------------------------------------------

    def gap(self, project_id: str, session_id: str, source: str, kind: str, detail: str) -> None:
        from .schema import stable_id
        self.norm.x("INSERT OR IGNORE INTO gaps (gap_key, project_id, session_id, source_adapter, "
                    "kind, detail, ts) VALUES (?,?,?,?,?,?,?)",
                    (stable_id(project_id, session_id, source, kind, detail), project_id,
                     session_id, source, kind, detail[:500], time.time()))

    def resolve_gaps(self, project_id: str, kind: str, sessions) -> int:
        """Remove `kind` gaps of these sessions (the source has since supplied
        what was missing)."""
        sessions = [s for s in sessions if s]
        n = 0
        for i in range(0, len(sessions), 400):
            part = sessions[i:i + 400]
            n += self.norm.x("DELETE FROM gaps WHERE project_id=? AND kind=? AND session_id IN "
                             f"({','.join('?' * len(part))})", (project_id, kind, *part)) or 0
        return n

    def gaps(self, project_id: Optional[str] = None) -> list[dict]:
        return self.norm.q("SELECT * FROM gaps" + (" WHERE project_id=?" if project_id else "")
                           + " ORDER BY ts", (project_id,) if project_id else ())

    def correlate(self, kind: str, project_id: str, a: str, b: str, method: str,
                  confidence: float, observed: bool, ambiguity: str = "") -> None:
        self.norm.x("INSERT OR REPLACE INTO correlations (kind, project_id, a, b, method, "
                    "confidence, observed, ambiguity, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                    (kind, project_id, a, b, method, confidence, 1 if observed else 0,
                     ambiguity, time.time()))

    def correlations(self, project_id: str, kind: Optional[str] = None) -> list[dict]:
        return self.norm.q("SELECT * FROM correlations WHERE project_id=?"
                           + (" AND kind=?" if kind else ""),
                           (project_id, kind) if kind else (project_id,))

    def checkpoint(self, key: str) -> Optional[str]:
        r = self.norm.q("SELECT cursor FROM checkpoints WHERE source_key=?", (key,))
        return r[0]["cursor"] if r else None

    def set_checkpoint(self, key: str, cursor: str) -> None:
        self.norm.x("INSERT OR REPLACE INTO checkpoints (source_key, cursor, updated_ts) "
                    "VALUES (?,?,?)", (key, cursor, time.time()))

    def run_start(self, key: str) -> int:
        with self.norm.lock:
            cur = self.norm.conn.execute(
                "INSERT INTO ingest_runs (source_key, started_ts, status) VALUES (?,?,?)",
                (key, time.time(), "running"))
            return cur.lastrowid

    def run_finish(self, run_id: int, status: str, counts: dict, error: str = "") -> None:
        self.norm.x("UPDATE ingest_runs SET finished_ts=?, status=?, counts=?, error=? WHERE id=?",
                    (time.time(), status, json.dumps(counts), error[:1000], run_id))

    def interrupted_runs(self) -> list[dict]:
        """Runs that stopped early (killed: still 'running'; interrupted;
        failed) with no completed run of the same source after them."""
        return self.norm.q(
            "SELECT * FROM ingest_runs r WHERE status IN ('running','interrupted','error') AND "
            "NOT EXISTS (SELECT 1 FROM ingest_runs d WHERE d.source_key=r.source_key AND "
            "d.status='done' AND d.id > r.id) ORDER BY id")

    # -- artifacts ---------------------------------------------------------------------

    def put_artifact(self, data: bytes, mime: str, provenance: dict) -> dict:
        import hashlib
        from .artifacts import image_info
        sha = hashlib.sha256(data).hexdigest()
        d = self.root / "artifacts" / "sha256" / sha[:2]
        _secure_dir(d)
        f = d / sha
        if not f.exists():
            f.write_bytes(data)
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        info = image_info(data)
        meta = {"sha256": sha, "mime": info.get("mime") or mime, "width": info.get("width"),
                "height": info.get("height"), "bytes": len(data), "status": "present"}
        self.norm.x("INSERT OR IGNORE INTO artifacts (sha256, mime, width, height, bytes, status, "
                    "first_seen, provenance) VALUES (?,?,?,?,?,?,?,?)",
                    (sha, meta["mime"], meta["width"], meta["height"], len(data), "present",
                     time.time(), json.dumps(provenance, default=str)))
        return meta

    def artifact_path(self, sha: str) -> Path:
        return self.root / "artifacts" / "sha256" / sha[:2] / sha

    def ref_artifact(self, ref_id: str, project_id: str, event_id: str, original: str,
                     sha: Optional[str], status: str, detail: str = "") -> None:
        self.norm.x("INSERT OR REPLACE INTO artifact_refs (ref_id, project_id, event_id, original, "
                    "sha256, status, detail) VALUES (?,?,?,?,?,?,?)",
                    (ref_id, project_id, event_id, original[:500], sha, status, detail[:300]))

    # -- lineage and deletion -------------------------------------------------------------

    def datasets_containing(self, episode_id: str) -> list[str]:
        return [r["dataset_id"] for r in self.cur.q(
            "SELECT dataset_id FROM dataset_members WHERE episode_id=?", (episode_id,))]

    def forget_episode(self, episode_id: str, reason: str, scope: str = "episode") -> dict:
        """Mark an episode deleted (it never appears in a future export) and
        report every dataset already built from it. scope=sources also
        deletes its normalized events and raw records."""
        ep = self.cur.q("SELECT * FROM episodes WHERE episode_id=?", (episode_id,))
        if not ep:
            raise StoreError(f"unknown episode {episode_id}")
        affected = self.datasets_containing(episode_id)
        self.cur.x("UPDATE episodes SET deleted=1, status='deleted', updated_ts=? WHERE "
                   "episode_id=?", (time.time(), episode_id))
        removed = {"events": 0, "raw": 0}
        if scope == "sources":
            ids = json.loads(ep[0]["event_ids"] or "[]")
            for eid in ids:
                r = self.norm.q("SELECT raw_ref FROM events WHERE event_id=?", (eid,))
                removed["events"] += self.norm.x(
                    "UPDATE events SET deleted=1, facts='{}' WHERE event_id=?", (eid,))
                if r and r[0]["raw_ref"]:
                    parts = r[0]["raw_ref"].split(":")
                    if len(parts) >= 4:
                        removed["raw"] += self.raw.x(
                            "UPDATE raw_records SET deleted=1, payload='{}' WHERE source_adapter=? "
                            "AND source_record_id=? AND payload_sha LIKE ?",
                            (parts[1], ":".join(parts[2:-1]), parts[-1] + "%"))
        self.cur.x("INSERT INTO deletions (episode_id, project_id, ts, reason, affected_datasets, "
                   "scope) VALUES (?,?,?,?,?,?)", (episode_id, ep[0]["project_id"], time.time(),
                                                   reason, json.dumps(affected), scope))
        for ds in affected:
            self.cur.x("UPDATE datasets SET status='contains-deleted' WHERE dataset_id=?", (ds,))
        return {"episode_id": episode_id, "affected_datasets": affected, "removed": removed}

    def retention(self, raw_days: int, normalized_days: int) -> dict:
        now = time.time()
        out = {}
        if raw_days > 0:
            out["raw"] = self.raw.x("DELETE FROM raw_records WHERE ingested_ts < ?",
                                    (now - raw_days * 86400,))
        if normalized_days > 0:
            # events still referenced by a live episode are kept (lineage)
            live = set()
            for r in self.cur.q("SELECT event_ids FROM episodes WHERE deleted=0"):
                live.update(json.loads(r["event_ids"] or "[]"))
            old = self.norm.q("SELECT event_id FROM events WHERE ingested_ts < ?",
                              (now - normalized_days * 86400,))
            drop = [r["event_id"] for r in old if r["event_id"] not in live]
            for eid in drop:
                self.norm.x("DELETE FROM events WHERE event_id=?", (eid,))
            out["normalized"] = len(drop)
            out["kept_for_lineage"] = len(old) - len(drop)
        return out
