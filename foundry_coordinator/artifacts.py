"""Immutable artifact and source snapshots for proactive helper lanes.

A helper must never look at a moving target. Two kinds of snapshot:

  artifact  the exact bytes of a READY, authorized capture (or an explicitly
            approved design reference), copied once into the coordinator's
            evidence store (state_dir/evidence/<project>/<task>/artifacts/
            <sha256>.<ext>), bound to project/task/run/session/revision and its
            sha256. The same path with different bytes is a new GENERATION; the
            older generation is superseded and findings about it are never
            delivered for the newer image.
  source    HEAD plus a per-file sha256 manifest of the task's authorized files,
            read twice: if a file or HEAD changed between the two reads a writer
            is active and the snapshot is UNSTABLE (retry later, never reviewed).
            Two dirty trees with the same HEAD but different bytes get different
            snapshot ids. File bytes are kept content-addressed (per project) so
            a helper packet quotes exactly what was snapshotted.

            Identity and ownership are SCOPED: snapshot_id = hash(project, task,
            run, workspace, HEAD, manifest), so two projects (or tasks, or runs)
            with identical HEAD and bytes (clones, fixtures) never share a row
            or a blob root. content_fp = hash(HEAD, manifest) is kept separately
            for "has the code changed?" comparisons. Every lookup takes the
            caller's project (and task) and refuses a record owned by another.
            Rows written before v0.119.1 (content-only ids, content_fp NULL) are
            left as they are: still readable by their recorded owner only.

Readiness is deterministic: the capture tool's own record (tools/
capture-window.ps1 prints VALID=..., CAPTURE ... fileBytes=N and Saved: <path>)
is correlated with the file on disk (size must equal fileBytes, the image must be
structurally complete: PNG chunks through IEND with valid CRCs, JPEG SOI..EOI).
A file merely appearing is never "ready". The optional filesystem fallback
(default off) additionally needs the size/mtime to be stable across two scans.

Paths: only sprint-approved capture globs and explicitly approved reference
globs, resolved inside the workspace, no symlink/junction components, never a
credentials/.git path. Nothing here captures the screen or runs a tool.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import json
import os
import re
import struct
import time
import uuid
import zlib
from typing import Optional

from .profile import matches_any

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, session_id TEXT,
  kind TEXT, rel_path TEXT, generation INTEGER, sha256 TEXT, bytes INTEGER, width INTEGER,
  height INTEGER, mime TEXT, valid TEXT, validity TEXT, source TEXT, store_path TEXT,
  revision TEXT, subject TEXT, record TEXT, status TEXT, superseded_by TEXT, reason TEXT,
  prev_artifact TEXT, created_ts REAL,
  UNIQUE (project_id, task_id, kind, rel_path, sha256));
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(project_id, task_id, rel_path);
CREATE TABLE IF NOT EXISTS source_snapshots (
  snapshot_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, head TEXT,
  dirty INTEGER, manifest TEXT, status TEXT, reason TEXT, bytes INTEGER, created_ts REAL);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_owner ON source_snapshots(project_id, task_id);
CREATE TABLE IF NOT EXISTS fs_watch (
  project_id TEXT, task_id TEXT, rel_path TEXT, size INTEGER, mtime REAL, seen_ts REAL,
  stable INTEGER DEFAULT 0, PRIMARY KEY (project_id, task_id, rel_path));
"""

# never snapshotted, whatever a glob says
DENY = ["_secrets/*", "*/_secrets/*", ".git/*", "*/.git/*", ".env", "*.env", "*.key", "*.pem",
        "*credential*", "*secret*", "*.kdbx"]
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


# --------------------------------------------------------------------------- images

def inspect_image(data: bytes) -> dict:
    """Structural completeness of PNG/JPEG bytes (no decoding library).
    {mime, width, height, complete, reason}."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        pos, w, h, seen_end = 8, None, None, False
        while pos + 8 <= len(data):
            n, typ = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + n]
            crc = data[pos + 8 + n:pos + 12 + n]
            if len(body) < n or len(crc) < 4:
                return {"mime": "image/png", "width": w, "height": h, "complete": False,
                        "reason": f"truncated in chunk {typ.decode('latin-1')}"}
            if zlib.crc32(typ + body) & 0xffffffff != struct.unpack(">I", crc)[0]:
                return {"mime": "image/png", "width": w, "height": h, "complete": False,
                        "reason": f"CRC mismatch in chunk {typ.decode('latin-1')}"}
            if typ == b"IHDR" and n >= 8:
                w, h = struct.unpack(">II", body[:8])
            pos += 12 + n
            if typ == b"IEND":
                seen_end = True
                break
        if not seen_end:
            return {"mime": "image/png", "width": w, "height": h, "complete": False,
                    "reason": "no IEND chunk (partial write)"}
        if not w or not h:
            return {"mime": "image/png", "width": w, "height": h, "complete": False,
                    "reason": "no IHDR dimensions"}
        return {"mime": "image/png", "width": w, "height": h, "complete": True, "reason": ""}
    if data[:2] == b"\xff\xd8":
        w = h = None
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xD9:
                break
            ln = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2) and i + 9 <= len(data):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
            if marker == 0xDA:
                break
            i += 2 + ln
        ok = data.rstrip(b"\x00")[-2:] == b"\xff\xd9"
        return {"mime": "image/jpeg", "width": w, "height": h, "complete": bool(ok and w and h),
                "reason": "" if ok and w and h else ("no EOI marker (partial write)" if not ok
                                                     else "no frame dimensions")}
    return {"mime": "", "width": None, "height": None, "complete": False,
            "reason": "not a PNG or JPEG"}


# --------------------------------------------------------------------------- capture records

_VALID = re.compile(r"^VALID=(OK|INVALID)(?:\((.*)\))?", re.M)
_CAPTURE = re.compile(r"^CAPTURE\s+(\S+)\s+(\d+)x(\d+)\b(.*)$", re.M)
_SAVED = re.compile(r"^Saved(?: \((invalid|view|invalid view)\))?:\s*(.+?)(?:\s+\(kept for .*)?$",
                    re.M)


def parse_capture_output(text: str) -> list[dict]:
    """The capture procedure's machine-readable record (capture-window.ps1):
    one record per saved file. A record without VALID=OK is invalid evidence."""
    text = text or ""
    if "Saved" not in text:
        return []
    v = _VALID.findall(text)
    valid, reason = (v[-1][0], v[-1][1]) if v else ("UNKNOWN", "no VALID= line")
    stats = {}
    for m in _CAPTURE.finditer(text):
        kv = dict(re.findall(r"(\w+)=(\S+)", m.group(4)))
        stats = {"name": m.group(1), "width": int(m.group(2)), "height": int(m.group(3)),
                 "file_bytes": int(kv["fileBytes"]) if kv.get("fileBytes", "").isdigit() else None,
                 "fnv": kv.get("hash"), "format": kv.get("format")}
    out = []
    for m in _SAVED.finditer(text):
        tag = m.group(1) or ""
        if "view" in tag:
            continue                          # a derived view copy, not the evidence frame
        out.append({"path": m.group(2).strip().strip('"'), "valid": "invalid" if tag == "invalid"
                    or valid != "OK" else "ok",
                    "reason": reason if valid != "OK" else "", **stats,
                    "lines": [l for l in text.splitlines()
                              if l.startswith(("VALID", "CAPTURE", "VALIDITY", "Saved"))][:40]})
    return out


# --------------------------------------------------------------------------- paths

def _expand(patterns: list[str], task_id: str) -> list[str]:
    return [p.replace("{task}", task_id) for p in patterns or []]


def authorize(workspace: str, path: str, allowed: list[str], task_id: str,
              deny: Optional[list[str]] = None) -> tuple[Optional[str], str]:
    """(workspace-relative path, "") when `path` may be snapshotted, else
    (None, why). Absolute paths must resolve inside the workspace; no
    component may be a symlink or junction; deny patterns always win."""
    if not path:
        return None, "empty path"
    root = os.path.realpath(workspace)
    raw = path.replace("/", os.sep)
    absp = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(workspace, raw))
    try:
        rel = os.path.relpath(absp, os.path.abspath(workspace))
    except ValueError:
        return None, "outside the workspace (another drive)"
    rel = rel.replace("\\", "/")
    if rel.startswith("../") or rel == ".." or os.path.isabs(rel):
        return None, "outside the workspace"
    # every component must be a plain directory/file (no symlink, no junction)
    cur = os.path.abspath(workspace)
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur) or _is_junction(cur):
            return None, f"symlink or junction in path ({part})"
    real = os.path.realpath(absp)
    if os.path.normcase(real) != os.path.normcase(absp) or not (
            os.path.normcase(real).startswith(os.path.normcase(root) + os.sep)):
        return None, "resolves outside the workspace"
    if matches_any(rel, DENY + list(deny or [])):
        return None, "denied path (credentials / repository internals)"
    if not matches_any(rel, _expand(allowed, task_id)):
        return None, "not an approved capture/reference path for this task"
    return rel, ""


def _is_junction(p: str) -> bool:
    fn = getattr(os.path, "isjunction", None)
    try:
        return bool(fn(p)) if fn else False
    except OSError:
        return False


# --------------------------------------------------------------------------- the store

class ArtifactStore:
    def __init__(self, store, state_dir: str, clock=time.time):
        self.store = store
        self.state_dir = state_dir
        self.clock = clock
        store.conn.executescript(SCHEMA)
        # v0.119.1, additive: scoped identity. Existing rows keep content_fp NULL
        # (= legacy content-only id) and their recorded owner; nothing is rewritten.
        for col in ("content_fp TEXT", "workspace TEXT"):
            try:
                store.conn.execute(f"ALTER TABLE source_snapshots ADD COLUMN {col}")
            except Exception:                                    # noqa: BLE001
                pass

    # -- artifacts -------------------------------------------------------------

    def _dir(self, pid: str, tid: str) -> str:
        d = os.path.join(self.state_dir, "evidence", pid, re.sub(r"[^\w.-]", "_", tid), "artifacts")
        os.makedirs(d, exist_ok=True)
        return d

    def get(self, artifact_id: str) -> Optional[dict]:
        r = self.store.q("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        return self._out(r[0]) if r else None

    @staticmethod
    def _out(r: dict) -> dict:
        r = dict(r)
        for k in ("validity", "record"):
            try:
                r[k] = json.loads(r[k]) if r.get(k) else {}
            except ValueError:
                r[k] = {}
        return r

    def list(self, pid: str, tid: str, kind: str = "") -> list[dict]:
        rows = self.store.q("SELECT * FROM artifacts WHERE project_id=? AND task_id=?"
                            + (" AND kind=?" if kind else "") + " ORDER BY created_ts",
                            (pid, tid, kind) if kind else (pid, tid))
        return [self._out(r) for r in rows]

    def record(self, *, project_id: str, task_id: str, run_id: str, session_id: str,
               workspace: str, path: str, allowed: list[str], kind: str = "capture",
               source: str = "tool_result", revision: str = "", record: Optional[dict] = None,
               max_bytes: int = 8_000_000, subject: str = "", deny=None) -> dict:
        """Snapshot one artifact. Returns {"status": "recorded"|"duplicate"|
        "not_ready"|"rejected", "artifact": row-or-None, "reason": ...}.
        not_ready means "look again later" (size mismatch / incomplete);
        rejected is final for those bytes (unauthorized, invalid, too big)."""
        rec = dict(record or {})
        rel, why = authorize(workspace, path, allowed, task_id, deny)
        if rel is None:
            return {"status": "rejected", "artifact": None, "reason": why, "path": path}
        absp = os.path.join(workspace, rel)
        try:
            st = os.stat(absp)
        except OSError:
            return {"status": "not_ready", "artifact": None, "reason": "file not present yet",
                    "path": rel}
        if st.st_size > max_bytes:
            return {"status": "rejected", "artifact": None, "path": rel,
                    "reason": f"{st.st_size} bytes is over the image limit ({max_bytes})"}
        with open(absp, "rb") as f:
            data = f.read(max_bytes + 1)
        st2 = os.stat(absp)
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns) or len(data) != st.st_size:
            return {"status": "not_ready", "artifact": None, "path": rel,
                    "reason": "file changed while being read (still being written)"}
        if rec.get("file_bytes") is not None and int(rec["file_bytes"]) != len(data):
            return {"status": "not_ready", "artifact": None, "path": rel,
                    "reason": f"on-disk size {len(data)} != capture record fileBytes "
                              f"{rec['file_bytes']} (not the frame the tool reported, or partial)"}
        img = inspect_image(data)
        if not img["complete"]:
            return {"status": "not_ready", "artifact": None, "path": rel,
                    "reason": f"incomplete image: {img['reason']}"}
        if rec.get("width") and (img["width"], img["height"]) != (rec["width"], rec["height"]):
            return {"status": "rejected", "artifact": None, "path": rel,
                    "reason": f"image is {img['width']}x{img['height']} but the capture record "
                              f"says {rec['width']}x{rec['height']}"}
        sha = hashlib.sha256(data).hexdigest()
        prior = self.store.q("SELECT * FROM artifacts WHERE project_id=? AND task_id=? AND kind=? "
                             "AND rel_path=? ORDER BY generation", (project_id, task_id, kind, rel))
        same = [p for p in prior if p["sha256"] == sha]
        if same:
            return {"status": "duplicate", "artifact": self._out(same[-1]), "path": rel,
                    "reason": "same bytes as an existing generation"}
        ext = os.path.splitext(rel)[1].lower() or (".png" if img["mime"] == "image/png" else ".jpg")
        # short names keep deep state dirs under Windows MAX_PATH; the full sha256
        # is in the row and re-checked on every read
        dest = os.path.join(self._dir(project_id, task_id), sha[:24] + ext)
        if not os.path.exists(dest):
            tmp = dest + f".t{uuid.uuid4().hex[:4]}"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        valid = rec.get("valid") or ("unknown" if source != "reference" else "ok")
        aid = f"art-{sha[:12]}-{uuid.uuid4().hex[:4]}"
        gen = (max(p["generation"] for p in prior) + 1) if prior else 1
        current = [p for p in prior if p["status"] == "current"]
        with self.store.tx():
            self.store.x("INSERT INTO artifacts (artifact_id, project_id, task_id, run_id, "
                         "session_id, kind, rel_path, generation, sha256, bytes, width, height, "
                         "mime, valid, validity, source, store_path, revision, subject, record, "
                         "status, superseded_by, reason, prev_artifact, created_ts) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, project_id, task_id, run_id, session_id, kind, rel, gen, sha,
                          len(data), img["width"], img["height"], img["mime"], valid,
                          json.dumps({"reason": rec.get("reason") or "",
                                      "lines": rec.get("lines") or []})[:8000], source, dest,
                          revision, subject or os.path.splitext(os.path.basename(rel))[0],
                          json.dumps({k: rec.get(k) for k in ("name", "file_bytes", "fnv",
                                                               "format", "width", "height")
                                      if rec.get(k) is not None}), "current", None, "",
                          current[-1]["artifact_id"] if current else None, self.clock()))
            for p in current:
                self.store.x("UPDATE artifacts SET status='superseded', superseded_by=? WHERE "
                             "artifact_id=?", (aid, p["artifact_id"]))
        return {"status": "recorded", "artifact": self.get(aid), "path": rel,
                "superseded": [p["artifact_id"] for p in current], "reason": ""}

    def read_bytes(self, artifact: dict) -> bytes:
        """The snapshotted bytes (re-hashed: a tampered copy is refused)."""
        with open(artifact["store_path"], "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError(f"snapshot {artifact['artifact_id']} no longer matches its sha256")
        return data

    def scan(self, *, project_id: str, task_id: str, run_id: str, session_id: str, workspace: str,
             allowed: list[str], debounce_s: float, revision: str = "",
             max_bytes: int = 8_000_000, max_files: int = 200) -> list[dict]:
        """Filesystem fallback (default off): a file is ready only when its
        size and mtime were unchanged for debounce_s across two scans AND it
        is a complete image. Returns record() results for newly ready files."""
        now = self.clock()
        out = []
        seen = 0
        for pat in _expand(allowed, task_id):
            for absp in sorted(_glob.glob(os.path.join(workspace, pat), recursive=True)):
                if seen >= max_files or not os.path.isfile(absp):
                    continue
                seen += 1
                rel = os.path.relpath(absp, workspace).replace("\\", "/")
                if os.path.splitext(rel)[1].lower() not in IMAGE_EXT or "_INVALID" in rel:
                    continue
                st = os.stat(absp)
                w = self.store.q("SELECT * FROM fs_watch WHERE project_id=? AND task_id=? AND "
                                 "rel_path=?", (project_id, task_id, rel))
                if not w or (w[0]["size"], w[0]["mtime"]) != (st.st_size, st.st_mtime):
                    self.store.x("INSERT INTO fs_watch (project_id, task_id, rel_path, size, mtime, "
                                 "seen_ts, stable) VALUES (?,?,?,?,?,?,0) ON CONFLICT(project_id, "
                                 "task_id, rel_path) DO UPDATE SET size=excluded.size, "
                                 "mtime=excluded.mtime, seen_ts=excluded.seen_ts, stable=0",
                                 (project_id, task_id, rel, st.st_size, st.st_mtime, now))
                    continue
                if w[0]["stable"] or now - w[0]["seen_ts"] < debounce_s:
                    continue
                r = self.record(project_id=project_id, task_id=task_id, run_id=run_id,
                                session_id=session_id, workspace=workspace, path=rel,
                                allowed=allowed, source="filesystem", revision=revision,
                                max_bytes=max_bytes)
                if r["status"] != "not_ready":
                    self.store.x("UPDATE fs_watch SET stable=1 WHERE project_id=? AND task_id=? AND "
                                 "rel_path=?", (project_id, task_id, rel))
                out.append(r)
        return out

    def prune(self, retention_days: float) -> int:
        """Delete snapshot copies past retention (rows are kept as history,
        marked expired; Victor's approval artifacts in the workspace are untouched)."""
        cut = self.clock() - retention_days * 86400
        n = 0
        for r in self.store.q("SELECT artifact_id, store_path FROM artifacts WHERE created_ts < ? "
                              "AND status != 'expired'", (cut,)):
            others = self.store.q("SELECT COUNT(*) AS n FROM artifacts WHERE store_path=? AND "
                                  "created_ts >= ?", (r["store_path"], cut))[0]["n"]
            if not others:
                try:
                    os.remove(r["store_path"])
                except OSError:
                    pass
            self.store.x("UPDATE artifacts SET status='expired' WHERE artifact_id=?",
                         (r["artifact_id"],))
            n += 1
        return n

    # -- source snapshots --------------------------------------------------------

    def source_snapshot(self, *, ws, project_id: str, task_id: str, run_id: str, workspace: str,
                        patterns: list[str], max_bytes: int = 400_000,
                        max_files: int = 60) -> dict:
        """HEAD + manifest of the authorized files, read twice. A snapshot
        whose second read differs is 'unstable' (an active writer)."""
        files = []
        for pat in _expand(patterns, task_id):
            if any(ch in pat for ch in "*?["):
                for absp in sorted(_glob.glob(os.path.join(workspace, pat), recursive=True)):
                    if os.path.isfile(absp):
                        files.append(os.path.relpath(absp, workspace).replace("\\", "/"))
            elif os.path.isfile(os.path.join(workspace, pat)):
                files.append(pat)
        files = sorted(dict.fromkeys(files))
        skipped = []
        ok_files = []
        for rel in files:
            r, why = authorize(workspace, rel, ["*", "**/*"], task_id)
            (ok_files if r else skipped).append(rel if r else f"{rel}: {why}")
        ok_files = ok_files[:max_files]

        def read_all():
            total, man, blobs = 0, {}, {}
            for rel in ok_files:
                with open(os.path.join(workspace, rel), "rb") as f:
                    b = f.read()
                total += len(b)
                if total > max_bytes:
                    return None, None, total
                h = hashlib.sha256(b).hexdigest()
                man[rel], blobs[h] = h, b
            return man, blobs, total
        head1 = ws.head()
        man1, blobs, total = read_all()
        if man1 is None:
            return {"status": "too_large", "reason": f"authorized files exceed {max_bytes} bytes",
                    "snapshot_id": None}
        man2, _, _ = read_all()
        head2 = ws.head()
        if man1 != man2 or head1 != head2:
            return {"status": "unstable", "snapshot_id": None,
                    "reason": "files or HEAD changed between two reads (a writer is active); "
                              "not used as a review target"}
        dirty_paths = {p for _, p in ws.status()}
        dirty = any(p in dirty_paths for p in man1)
        mjson = json.dumps(man1, sort_keys=True)
        content_fp = hashlib.sha256((head1 + mjson).encode()).hexdigest()[:24]
        wsid = os.path.normcase(os.path.realpath(workspace))
        sid = "ss-" + hashlib.sha256("\0".join((project_id, task_id, run_id, wsid, head1, mjson))
                                     .encode()).hexdigest()[:20]
        bdir = os.path.join(self.state_dir, "snapshots", project_id, "blobs")
        os.makedirs(bdir, exist_ok=True)
        for h, b in blobs.items():
            dest = os.path.join(bdir, h[:32])
            if not os.path.exists(dest):
                tmp = dest + f".t{uuid.uuid4().hex[:4]}"
                with open(tmp, "wb") as f:
                    f.write(b)
                os.replace(tmp, dest)
        prior = self.store.q("SELECT project_id, task_id, run_id FROM source_snapshots WHERE "
                             "snapshot_id=?", (sid,))
        if prior and (prior[0]["project_id"], prior[0]["task_id"], prior[0]["run_id"]) != \
                (project_id, task_id, run_id):
            # only a hash collision could get here; never adopt another owner's row
            raise ValueError(f"snapshot id {sid} is owned by another project/task/run")
        if not prior:
            self.store.x("INSERT INTO source_snapshots (snapshot_id, project_id, task_id, run_id, "
                         "head, dirty, manifest, status, reason, bytes, created_ts, content_fp, "
                         "workspace) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, project_id, task_id, run_id, head1, int(dirty), mjson, "consistent",
                          "; ".join(skipped)[:1000], total, self.clock(), content_fp, wsid))
        return {"status": "consistent", "snapshot_id": sid, "content_fp": content_fp,
                "head": head1, "dirty": dirty, "manifest": man1, "skipped": skipped}

    def snapshot(self, snapshot_id: str, project_id: str, task_id: str = "") -> Optional[dict]:
        """The snapshot record, only for its owner: a record of another project
        (or, when task_id is given, another task) is never returned, whatever
        id the caller supplies. Legacy rows (content_fp NULL) follow the same
        rule with their recorded owner."""
        if not snapshot_id or not project_id:
            return None
        if task_id:
            r = self.store.q("SELECT * FROM source_snapshots WHERE snapshot_id=? AND project_id=? "
                             "AND task_id=?", (snapshot_id, project_id, task_id))
        else:
            r = self.store.q("SELECT * FROM source_snapshots WHERE snapshot_id=? AND project_id=?",
                             (snapshot_id, project_id))
        if not r:
            return None
        r = dict(r[0])
        r["manifest"] = json.loads(r["manifest"] or "{}")
        r["legacy"] = r.get("content_fp") is None
        return r

    def snapshot_file(self, snapshot_id: str, rel: str, project_id: str,
                      task_id: str = "") -> Optional[str]:
        s = self.snapshot(snapshot_id, project_id, task_id)
        if not s or rel not in s["manifest"]:
            return None
        p = os.path.join(self.state_dir, "snapshots", s["project_id"], "blobs",
                         s["manifest"][rel][:32])
        with open(p, "rb") as f:
            b = f.read()
        if hashlib.sha256(b).hexdigest() != s["manifest"][rel]:
            raise ValueError(f"snapshot blob for {rel} no longer matches its hash")
        return b.decode("utf-8", "replace")

    def drift(self, snapshot_id: str, workspace: str, paths: list[str], project_id: str,
              task_id: str = "") -> dict:
        """For each path: same as the snapshot, changed since, or not in it.
        An unknown or foreign snapshot compares as empty (absent / new file)."""
        s = self.snapshot(snapshot_id, project_id, task_id) or {"manifest": {}}
        out = {}
        for rel in paths:
            p = os.path.join(workspace, rel)
            cur = None
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    cur = hashlib.sha256(f.read()).hexdigest()
            want = s["manifest"].get(rel)
            if want:
                out[rel] = "unchanged" if cur == want else "changed" if cur else "deleted"
            else:
                out[rel] = "new file" if cur else "absent"
        return out
