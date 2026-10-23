"""kb_search for sessions: a minimal read-only MCP stdio server over the
coordinator's playbook index (A1).

    python -m foundry_coordinator.playbook.kb_mcp [--db PATH | --state DIR]

Wire it into a project's kilo.jsonc like any other MCP server (per-project
opt-in). It speaks newline-delimited JSON-RPC 2.0, serves one tool (kb_search)
and opens the coordinator database read-only (SQLite URI mode=ro), so a session
can search the playbook but never write anything. No model call, no coordinator
process involvement. Default DB: $FOUNDRY_COORD_STATE/coordinator.sqlite, else
~/.foundry-coordinator/coordinator.sqlite.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

from .layout import PLAYBOOK_ID

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "foundry-playbook", "version": "1"}
TOOLS = [{
    "name": "kb_search",
    "description": "Search the cross-project playbook (templates, how-tos, verified fixes, "
                   "scripts, formats) the coordinator indexed. Returns the best-matching "
                   "entries with their file, heading and text.",
    "inputSchema": {"type": "object",
                    "properties": {"query": {"type": "string",
                                             "description": "words to look for in headings, "
                                                            "tags and text"},
                                   "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                    "required": ["query"]},
}]


def default_db() -> str:
    state = os.environ.get("FOUNDRY_COORD_STATE") or os.path.join(os.path.expanduser("~"),
                                                                  ".foundry-coordinator")
    return os.path.join(state, "coordinator.sqlite")


def open_ro(db: str) -> sqlite3.Connection:
    uri = "file:" + db.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def search(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[dict]:
    """The same ranking as MemoryIndex.search, standalone and read-only."""
    words = [w for w in re.findall(r"[A-Za-z0-9_./-]+", query or "") if len(w) >= 2][:12]
    if not words:
        return []
    try:
        q = " OR ".join('"' + w.replace('"', "") + '"*' for w in words)
        rows = conn.execute(
            "SELECT e.file, e.heading, e.text, bm25(memory_fts, 0, 0, 2.0, 5.0, 1.0, 3.0) AS rank "
            "FROM memory_fts f JOIN memory_entries e ON e.project_id=f.project_id AND "
            "e.entry_id=f.entry_id WHERE memory_fts MATCH ? AND f.project_id=? "
            "ORDER BY rank LIMIT ?", (q, PLAYBOOK_ID, limit)).fetchall()
        return [{"file": r["file"], "heading": r["heading"], "text": r["text"]} for r in rows]
    except sqlite3.OperationalError:
        pass
    out = []
    for r in conn.execute("SELECT file, heading, text, tags, ord FROM memory_entries WHERE "
                          "project_id=?", (PLAYBOOK_ID,)):
        low = f"{r['heading']}\n{r['tags'] or ''}\n{r['text']}".lower()
        score = 0
        for w in words:
            lw = w.lower()
            score += (3 if lw in (r["heading"] or "").lower() else 0) \
                + (2 if lw in (r["tags"] or "").lower() else 0) + (1 if lw in low else 0)
        if score:
            out.append((score, r))
    out.sort(key=lambda x: (-x[0], x[1]["file"], x[1]["ord"]))
    return [{"file": r["file"], "heading": r["heading"], "text": r["text"]}
            for _, r in out[:limit]]


def handle(conn, req: dict) -> dict | None:
    rid, method = req.get("id"), req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}}}}
    if method in ("notifications/initialized", "initialized") or rid is None:
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = req.get("params") or {}
        if p.get("name") != "kb_search":
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {p.get('name')!r}"}}
        args = p.get("arguments") or {}
        try:
            hits = search(conn, str(args.get("query") or ""),
                          max(1, min(20, int(args.get("limit") or 8))))
        except sqlite3.Error as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"playbook index unavailable: {e}"}],
                "isError": True}}
        if not hits:
            text = "No playbook entries match."
        else:
            text = "\n\n".join(f"[{h['file']} > {h['heading']}]\n{h['text']}" for h in hits)
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text[:20000]}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kb_mcp", description=__doc__)
    ap.add_argument("--db", default="", help="coordinator.sqlite path")
    ap.add_argument("--state", default="", help="coordinator state dir (holds coordinator.sqlite)")
    a = ap.parse_args(argv)
    db = a.db or (os.path.join(a.state, "coordinator.sqlite") if a.state else default_db())
    if not os.path.isfile(db):
        print(f"kb_mcp: no coordinator database at {db}", file=sys.stderr)
        return 2
    conn = open_ro(db)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            res = handle(conn, req)
        except Exception as e:                                    # noqa: BLE001
            res = {"jsonrpc": "2.0", "id": req.get("id"),
                   "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"[:300]}}
        if res is not None:
            out.write(json.dumps(res) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
