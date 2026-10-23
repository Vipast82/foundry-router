"""Project memory index: the markdown files under docs/memory parsed by heading
into entries, kept in the coordinator DB with a content hash per entry and an
FTS5 table for search (LIKE fallback when the sqlite build has no FTS5).

An entry is the unit a slice sends and a session's delta hashes: one list item,
one table row or one paragraph under a heading path ("Studio > Gate"). Tags are
inferred from the file (decisions, conventions, assets, known_issues, track:<n>,
sprint:<ID>), the sprint ids, repository paths and "needs" words found in the
heading and text, and the heading words themselves.

Re-indexing is cheap and incremental: a file is re-parsed only when its mtime
or size changed (or the commit the caller passes differs); nothing is re-sent
to a session unless its hash changed.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from . import layout

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS memory_files (
      project_id TEXT, file TEXT, mtime REAL, size INTEGER, hash TEXT, commit_sha TEXT,
      entries INTEGER, indexed_ts REAL, PRIMARY KEY (project_id, file))""",
    """CREATE TABLE IF NOT EXISTS memory_entries (
      project_id TEXT, entry_id TEXT, file TEXT, heading TEXT, level INTEGER, ord INTEGER,
      text TEXT, tags TEXT, hash TEXT, updated_ts REAL, PRIMARY KEY (project_id, entry_id))""",
    "CREATE INDEX IF NOT EXISTS idx_memory_entries_file ON memory_entries(project_id, file, ord)",
    """CREATE TABLE IF NOT EXISTS memory_slices (
      session_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, role TEXT, track TEXT,
      ts REAL, prompts INTEGER, tokens INTEGER, entries TEXT, budget INTEGER, hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory_notes (
      note_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, session_id TEXT, actor TEXT,
      file TEXT, heading TEXT, text TEXT, hash TEXT, status TEXT, detail TEXT, sha TEXT,
      ts REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_memory_notes_task ON memory_notes(project_id, task_id, ts)",
]
FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(project_id UNINDEXED, "
       "entry_id UNINDEXED, file, heading, text, tags, tokenize='unicode61')")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LIST = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_SPRINT_ID = re.compile(r"\b[A-Z][A-Z0-9]{0,4}(?:-[A-Z0-9]{1,6})?[a-z]?\b")
_PATH = re.compile(r"(?<![\w/])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_\-*]+(?:\.[A-Za-z0-9_*]+)*)")
_CODE_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*\.(?:lua|luau|py|json|toml|md|ps1|blend|fbx|"
                        r"png|wav|ogg|mp3))`")
NEEDS_WORDS = ("studio", "blender", "audio", "image", "images", "backend", "gpu", "rojo", "lune",
               "ui", "vision", "gate", "comfy", "qwen", "mcp", "wally", "datastore", "selene",
               "stylua", "capture", "mesh", "texture", "material", "animation", "sound", "music",
               "server", "client", "shared", "test", "tests", "story", "stories")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_STOP = {"the", "and", "for", "with", "from", "this", "that", "are", "not", "but", "into", "its",
         "was", "were", "has", "have", "any", "all", "one", "two", "per", "via", "see", "use",
         "used", "when", "then", "than", "also", "only", "each", "every", "never", "always",
         "imported", "note", "notes", "file", "files", "sprint", "sprints", "track", "tracks"}


def tokens(text: str) -> int:
    """A cheap token estimate (4 characters per token) for budgets."""
    return max(1, (len(text or "") + 3) // 4)


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class Entry:
    file: str
    heading: str                 # "H2 > H3" path (without the H1 title)
    level: int
    ord: int                     # position in the file (0-based, entries only)
    text: str
    tags: list = field(default_factory=list)

    @property
    def hash(self) -> str:
        return _h(self.text)

    @property
    def entry_id(self) -> str:
        return _h(f"{self.file}\n{self.heading}\n{self.text}")

    def as_dict(self) -> dict:
        return {"entry_id": self.entry_id, "file": self.file, "heading": self.heading,
                "level": self.level, "ord": self.ord, "text": self.text, "tags": list(self.tags),
                "hash": self.hash}


# ---------------------------------------------------------------------------- tags

def file_tags(file: str) -> list[str]:
    kind, _, base = file.partition("/")
    if not base:
        return [file[:-3].lower() if file.lower().endswith(".md") else file.lower()]
    stem = base[:-3] if base.lower().endswith(".md") else base
    if kind == "tracks":
        return [f"track:{stem.lower()}", stem.lower()]
    if kind == "sprints":
        return [f"sprint:{stem}", stem]
    return [kind.lower(), stem.lower()]


def path_tags(path: str) -> list[str]:
    """A repository path and its directory prefixes of two or more components,
    plus the basename stem: `src/client/UI/Shop.lua` -> src/client/ui,
    src/client/ui/shop.lua, shop."""
    p = path.replace("\\", "/").strip("/").lower()
    parts = [x for x in p.split("/") if x and x not in ("*", "**")]
    out = []
    for i in range(2, len(parts) + 1):
        seg = parts[:i]
        if any("*" in s for s in seg):
            break
        out.append("/".join(seg))
    if parts:
        last = parts[-1]
        if "*" not in last and "." in last:
            out.append(last.rsplit(".", 1)[0])
    return out


def sprint_ids(text: str) -> list[str]:
    """Sprint-like ids in `text` (S12, T1, UI-02b, UI-GT1): upper-case tokens that
    contain a digit, or an upper-case prefix with a dash and a suffix."""
    out = []
    for m in _SPRINT_ID.finditer(text or ""):
        tok = m.group(0)
        if not any(ch.isdigit() for ch in tok) and "-" not in tok:
            continue
        if "-" not in tok and len(tok) > 6:
            continue
        if tok not in out:
            out.append(tok)
    return out


def infer_tags(file: str, heading: str, text: str) -> list[str]:
    tags = list(file_tags(file))
    hay = f"{heading}\n{text}"
    for sid in sprint_ids(hay):
        tags.append(sid)
    for m in _PATH.finditer(hay):
        tags += path_tags(m.group(1))
    for m in _CODE_NAME.finditer(hay):
        tags.append(m.group(1).rsplit(".", 1)[0].lower())
    low = hay.lower()
    words = set(_WORD.findall(low))
    for w in NEEDS_WORDS:
        if w in words:
            tags.append(w)
    for w in _WORD.findall((heading or "").lower()):
        if w not in _STOP and len(w) >= 3:
            tags.append(w)
    seen, out = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:60]


# ---------------------------------------------------------------------------- parsing

def parse_markdown(file: str, text: str) -> list[Entry]:
    """Entries of one memory file: list items (with their continuation lines),
    table rows (header and separator rows excluded) and paragraphs, each under
    its heading path. Fenced code stays inside the entry it belongs to."""
    stack: list[tuple[int, str]] = []        # (level, title) of the open headings
    entries: list[Entry] = []
    body: list[str] = []

    def heading_path() -> tuple[str, int]:
        titles = [t for lvl, t in stack if lvl >= 2]
        if not titles and stack:
            return "", stack[-1][0]
        return " > ".join(titles), (stack[-1][0] if stack else 0)

    def flush():
        path, level = heading_path()
        if not path:
            body.clear()                # the file's title and preamble describe it: not facts
            return
        for chunk in _chunks(body):
            if len(chunk.strip()) < 3:
                continue
            entries.append(Entry(file, path, level, len(entries), chunk.strip("\n"),
                                 infer_tags(file, path, chunk)))
        body.clear()

    fence = False
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if line.lstrip().startswith("```"):
            fence = not fence
            body.append(line)
            continue
        m = None if fence else _HEADING.match(line)
        if m:
            flush()
            lvl, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, title))
            continue
        body.append(line)
    flush()
    return entries


def _chunks(lines: list[str]) -> list[str]:
    """Split a heading body into entry texts: table rows (header and separator
    rows dropped), list items with their continuation lines, paragraphs."""
    out: list[str] = []
    cur: list[str] = []
    kind = ""                                   # "list" | "table" | "para"
    fence = False
    prev_row = -1                               # index in `out` of the table's last row

    def close():
        nonlocal cur, kind, prev_row
        if cur:
            out.append("\n".join(cur))
        cur, kind, prev_row = [], "", -1

    for line in lines:
        s = line.strip()
        if line.lstrip().startswith("```"):
            fence = not fence
            if kind not in ("list", "para"):
                close()
                kind = "para"
            cur.append(line.rstrip())
            continue
        if fence:
            cur.append(line.rstrip())
            continue
        if not s:
            close()
            continue
        if s.startswith("|"):
            if kind != "table":
                close()
                kind = "table"
            if _TABLE_SEP.match(s):
                if prev_row >= 0 and prev_row == len(out) - 1:
                    out.pop()                   # the row before a separator is the header
                prev_row = -1
                continue
            out.append(s)
            prev_row = len(out) - 1
            continue
        if kind == "table":
            close()
        if _LIST.match(s) and not line.startswith((" ", "\t")):
            close()
            kind = "list"
            cur.append(line.rstrip())
            continue
        if kind == "list":
            cur.append(line.rstrip())           # continuation of the item
            continue
        if kind != "para":
            close()
            kind = "para"
        cur.append(line.rstrip())
    close()
    return out


# ---------------------------------------------------------------------------- the index

class MemoryIndex:
    def __init__(self, store):
        self.store = store
        conn = store.conn
        for sql in SCHEMA:
            conn.execute(sql)
        try:
            conn.execute(FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    # -- reading files ------------------------------------------------------------

    def reindex(self, project_id: str, workspace: str, commit: str = "",
                force: bool = False, files: Optional[list] = None,
                root: str = "") -> dict:
        """Parse every memory file whose mtime/size (or the commit) changed; drop
        files that vanished. Returns {"files", "entries", "changed": [files],
        "removed": [files]}. `files` + `root` index another layout with the same
        machinery (the playbook repo, keyed by its reserved project id)."""
        if files is None:
            files = layout.list_files(workspace)
        root = root or layout.memory_dir(workspace)
        known = {r["file"]: r for r in self.store.q(
            "SELECT * FROM memory_files WHERE project_id=?", (project_id,))}
        changed, total = [], 0
        now = time.time()
        for rel in files:
            path = os.path.join(root, *rel.split("/"))
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = known.pop(rel, None)
            if (prev and not force and abs((prev["mtime"] or 0) - st.st_mtime) < 1e-6
                    and prev["size"] == st.st_size and (not commit or prev["commit_sha"] == commit)):
                total += int(prev["entries"] or 0)
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
            if prev and prev["hash"] == h and not force:
                self.store.x("UPDATE memory_files SET mtime=?, size=?, commit_sha=? WHERE "
                             "project_id=? AND file=?", (st.st_mtime, st.st_size, commit,
                                                         project_id, rel))
                total += int(prev["entries"] or 0)
                continue
            n = self._replace_file(project_id, rel, parse_markdown(rel, text), now)
            self.store.x("INSERT OR REPLACE INTO memory_files (project_id, file, mtime, size, hash, "
                         "commit_sha, entries, indexed_ts) VALUES (?,?,?,?,?,?,?,?)",
                         (project_id, rel, st.st_mtime, st.st_size, h, commit, n, now))
            changed.append(rel)
            total += n
        removed = []
        for rel in known:
            self._replace_file(project_id, rel, [], now)
            self.store.x("DELETE FROM memory_files WHERE project_id=? AND file=?", (project_id, rel))
            removed.append(rel)
        return {"files": len(files), "entries": total, "changed": changed, "removed": removed}

    def _replace_file(self, project_id: str, rel: str, entries: list[Entry], now: float) -> int:
        with self.store.tx():
            self.store.x("DELETE FROM memory_entries WHERE project_id=? AND file=?",
                         (project_id, rel))
            if self.fts:
                self.store.x("DELETE FROM memory_fts WHERE project_id=? AND file=?",
                             (project_id, rel))
            seen = set()
            for e in entries:
                eid = e.entry_id
                if eid in seen:
                    continue                     # the same text twice under one heading
                seen.add(eid)
                self.store.x("INSERT INTO memory_entries (project_id, entry_id, file, heading, "
                             "level, ord, text, tags, hash, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (project_id, eid, e.file, e.heading, e.level, e.ord, e.text,
                              " ".join(e.tags), e.hash, now))
                if self.fts:
                    self.store.x("INSERT INTO memory_fts (project_id, entry_id, file, heading, text, "
                                 "tags) VALUES (?,?,?,?,?,?)",
                                 (project_id, eid, e.file, e.heading, e.text, " ".join(e.tags)))
        return len(seen)

    # -- reading the index ---------------------------------------------------------

    @staticmethod
    def _row(r: dict) -> dict:
        return {"entry_id": r["entry_id"], "file": r["file"], "heading": r["heading"],
                "level": r["level"], "ord": r["ord"], "text": r["text"],
                "tags": (r["tags"] or "").split(), "hash": r["hash"]}

    def entries(self, project_id: str, file: Optional[str] = None) -> list[dict]:
        if file:
            rows = self.store.q("SELECT * FROM memory_entries WHERE project_id=? AND file=? "
                                "ORDER BY ord", (project_id, file))
        else:
            rows = self.store.q("SELECT * FROM memory_entries WHERE project_id=? ORDER BY file, ord",
                                (project_id,))
        return [self._row(r) for r in rows]

    def files(self, project_id: str) -> list[dict]:
        return [{"file": r["file"], "entries": r["entries"], "hash": r["hash"],
                 "commit": r["commit_sha"], "indexed_ts": r["indexed_ts"], "size": r["size"]}
                for r in self.store.q("SELECT * FROM memory_files WHERE project_id=? ORDER BY file",
                                      (project_id,))]

    def fingerprint(self, project_id: str) -> str:
        rows = self.store.q("SELECT file, hash FROM memory_files WHERE project_id=? ORDER BY file",
                            (project_id,))
        return hashlib.sha1("\n".join(f"{r['file']}:{r['hash']}" for r in rows)
                            .encode()).hexdigest()[:16] if rows else ""

    def search(self, project_id: str, query: str, limit: int = 20) -> list[dict]:
        words = [w for w in re.findall(r"[A-Za-z0-9_./-]+", query or "") if len(w) >= 2][:12]
        if not words:
            return []
        if self.fts:
            q = " OR ".join('"' + w.replace('"', "") + '"*' for w in words)
            try:
                rows = self.store.q(
                    "SELECT e.*, bm25(memory_fts, 0, 0, 2.0, 5.0, 1.0, 3.0) AS rank FROM memory_fts f "
                    "JOIN memory_entries e ON e.project_id=f.project_id AND e.entry_id=f.entry_id "
                    "WHERE memory_fts MATCH ? AND f.project_id=? ORDER BY rank LIMIT ?",
                    (q, project_id, limit))
                return [self._row(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        # fallback: score by word hits in heading (3), tags (2) and text (1)
        out = []
        for e in self.entries(project_id):
            low = (e["heading"] + "\n" + " ".join(e["tags"]) + "\n" + e["text"]).lower()
            score = 0
            for w in words:
                lw = w.lower()
                if lw in e["heading"].lower():
                    score += 3
                if lw in (t.lower() for t in e["tags"]):
                    score += 2
                if lw in low:
                    score += 1
            if score:
                out.append((score, e))
        out.sort(key=lambda x: (-x[0], x[1]["file"], x[1]["ord"]))
        return [e for _, e in out[:limit]]
