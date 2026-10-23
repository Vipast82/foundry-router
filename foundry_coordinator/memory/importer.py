"""One-time import of today's project memory into docs/memory (`memory
import-state <project>`): the STATE.md sprint ledger (one sprints/<ID>.md per
row), its "Current API surface" (CONVENTIONS.md), docs/progress/state/
DECISIONS.md (DECISIONS.md), docs/progress/state/KNOWN_ISSUES.md
(KNOWN_ISSUES.md), the delivered operator notes and the active task_memory rows
(sprints/<task>.md). STATE.md itself is not changed: the CLOSE session keeps
appending its row until the plans move to memory files.

Idempotent through an import marker in the written files and the store's meta;
`force=True` imports again (appending, never rewriting what is there).
"""

from __future__ import annotations

import re
import time
from typing import Optional

from . import layout
from .notes import append_to_section

STATE_PATH = "docs/progress/STATE.md"
DECISIONS_PATH = "docs/progress/state/DECISIONS.md"
KNOWN_PATH = "docs/progress/state/KNOWN_ISSUES.md"
MARK = "<!-- memory import:"
LEDGER_HEADING = "Ledger (imported from docs/progress/STATE.md)"
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.M)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().replace("\r\n", "\n")
    except OSError:
        return ""


def section(text: str, title_part: str) -> str:
    """The body of the `## <title>` section whose title contains `title_part`
    (case-insensitive), up to the next `## `."""
    low = title_part.lower()
    heads = list(_H2.finditer(text))
    for i, m in enumerate(heads):
        if low in m.group(1).lower():
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end]
    return ""


def table_rows(text: str) -> list[dict]:
    """Rows of the first markdown table in `text`, keyed by lower-cased header."""
    header: Optional[list] = None
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if re.match(r"^:?-{2,}:?$", cells[0].replace(" ", "")):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        rows.append({header[i]: cells[i] for i in range(min(len(header), len(cells)))})
    return rows


def import_state(manager, P, actor: str = "operator", force: bool = False) -> dict:
    """Fold the existing project memory into docs/memory and commit it. Returns
    {"written": [repo-relative paths], "sha", "counts": {...}} or {"skipped": why}."""
    ws = P.profile.workspace
    key = f"memory_import:{P.id}"
    if manager.store.meta(key) and not force:
        return {"skipped": f"already imported ({manager.store.meta(key)}); use --force to append again"}
    created = layout.init(ws, P.profile.name)["created"] if not layout.exists(ws) else []
    root = layout.memory_dir(ws)
    texts: dict[str, str] = {}
    counts: dict[str, int] = {}

    def get(file: str) -> str:
        if file not in texts:
            cur = _read(f"{root}/{file}")
            texts[file] = cur if cur else layout.new_file_text(file, P.profile.name)
        return texts[file]

    def put(file: str, text: str) -> None:
        texts[file] = text

    def add(file: str, heading: str, body: str) -> None:
        put(file, append_to_section(get(file), heading, body))
        counts[file.split("/")[0]] = counts.get(file.split("/")[0], 0) + 1

    state = _read(f"{ws}/{STATE_PATH}")
    if not state:
        return {"skipped": f"{STATE_PATH} not found in {ws}"}
    try:
        head = P.ws.head()
    except Exception:                                             # noqa: BLE001
        head = ""
    stamp = f"{MARK} {STATE_PATH} @ {head[:12] or '?'} {time.strftime('%Y-%m-%d')} -->"

    # 1. the sprint ledger -> sprints/<ID>.md
    for r in table_rows(section(state, "ledger")):
        sid = (r.get("sprint") or r.get("id") or "").strip("` ")
        if not sid or not layout.file_ok(f"sprints/{sid}.md"):
            continue
        file = f"sprints/{sid}.md"
        if file not in texts and not _read(f"{root}/{file}"):
            put(file, f"# Sprint {sid} — {r.get('title', '')}\n\nNotes a session or the coordinator "
                      f"recorded for sprint {sid}.\n")
        parts = [f"Status {r.get('status', '?')}"]
        if r.get("date"):
            parts.append(r["date"])
        if r.get("commit"):
            parts.append(f"commit {r['commit']}")
        line = ", ".join(parts) + (f". {r['summary']}" if r.get("summary") else ".")
        if r.get("title"):
            line = f"{r['title']} — {line}"
        add(file, LEDGER_HEADING, line)

    # 2. the API surface -> CONVENTIONS.md
    api = section(state, "api surface")
    if api.strip():
        sub, buf = "General", []

        def flush_api():
            for item in _items(buf):
                add("CONVENTIONS.md", f"API surface: {sub}", item)
            buf.clear()
        for line in api.splitlines():
            m = re.match(r"^###\s+(.*?)\s*$", line)
            if m:
                flush_api()
                sub = m.group(1)
                continue
            buf.append(line)
        flush_api()

    # 3. state/DECISIONS.md -> DECISIONS.md
    for r in table_rows(_read(f"{ws}/{DECISIONS_PATH}")):
        q, d = r.get("question", ""), r.get("decision", "")
        if not d:
            continue
        who = r.get("sprint", "").strip("` ") or "?"
        title = re.sub(r"\s+", " ", q)[:70].rstrip(" ,.;:?") or "decision"
        heading = f"{who}: {title}"
        add("DECISIONS.md", heading, f"Decided ({who}): {d}")
        if q:
            add("DECISIONS.md", heading, f"Question: {q}")

    # 4. state/KNOWN_ISSUES.md -> KNOWN_ISSUES.md (its sections, before ## Resolved)
    known = _read(f"{ws}/{KNOWN_PATH}")
    if known.strip():
        body = re.sub(r"^#\s.*\n", "", known, count=1)
        body = re.sub(r"^>.*\n", "", body, flags=re.M).strip("\n")
        if body:
            cur = get("KNOWN_ISSUES.md")
            if "\n## Resolved" in cur:
                a, b = cur.split("\n## Resolved", 1)
                put("KNOWN_ISSUES.md", a.rstrip("\n") + "\n\n" + body + "\n\n## Resolved" + b)
            else:
                put("KNOWN_ISSUES.md", cur.rstrip("\n") + "\n\n" + body + "\n")
            counts["KNOWN_ISSUES.md"] = body.count("\n- ") + 1

    # 5. operator notes -> sprints/<task>.md
    try:
        msgs = [m for m in manager.store.operator_messages()
                if m["project_id"] == P.id and m["status"] in ("delivered", "queued", "delivering")]
    except Exception:                                             # noqa: BLE001
        msgs = []
    for m in msgs:
        file = f"sprints/{m['task_id']}.md"
        if not m.get("task_id") or not layout.file_ok(file):
            continue
        when = time.strftime("%Y-%m-%d", time.localtime(m.get("created_ts") or 0))
        text = " ".join(str(m.get("text") or "").split())[:1500]
        add(file, "Operator notes", f"[{when}, {m.get('actor') or 'operator'}] {text}")

    # 6. task_memory (lifecycle) -> sprints/<task>.md
    try:
        rows = manager.store.q("SELECT * FROM task_memory WHERE project_id=? AND status='active' "
                               "ORDER BY created_ts", (P.id,))
    except Exception:                                             # noqa: BLE001
        rows = []
    for r in rows:
        file = f"sprints/{r['task_id']}.md"
        if not r.get("task_id") or not layout.file_ok(file):
            continue
        text = " ".join(str(r.get("text") or "").split())[:1500]
        add(file, "Coordinator memory", f"[{r.get('kind')}, {r.get('authority')}] {text}")

    written = []
    for file, text in texts.items():
        if file in layout.FIXED_FILES and stamp not in text:
            text = text.rstrip("\n") + "\n\n" + stamp + "\n"
        path = f"{root}/{file}"
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(layout.rel_path(file))
    sha = ""
    written = sorted(set(written) | set(created))
    if written:
        sha, err = manager.commit(P, written, "MEMORY", "import STATE.md ledger, decisions, known "
                                  "issues and operator notes into docs/memory", actor=actor)
        if err:
            return {"written": sorted(written), "sha": "", "error": err, "counts": counts}
    manager.store.set_meta(key, f"{time.strftime('%Y-%m-%d %H:%M')} {sha[:12]}")
    manager.reindex(P, force=True)
    return {"written": sorted(written), "sha": sha, "counts": counts}


def _items(lines: list[str]) -> list[str]:
    """List items (with continuation lines) and paragraphs of a few lines."""
    out, cur = [], []
    for line in lines:
        s = line.rstrip()
        if not s.strip():
            if cur:
                out.append("\n".join(cur))
                cur = []
            continue
        if re.match(r"^(?:[-*+]|\d+[.)])\s", s) and not s.startswith((" ", "\t")):
            if cur:
                out.append("\n".join(cur))
            cur = [s]
        else:
            cur.append(s)
    if cur:
        out.append("\n".join(cur))
    return [x for x in out if len(x.strip()) >= 3]
