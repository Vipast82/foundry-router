"""Project memory, repo layout (Team Lead piece 4, 2026-10-22).

Memory lives in the game repo under `docs/memory/`, git-tracked, so every
session, the chat and the operator read the same notes:

  DECISIONS.md      project-wide decisions, one `## <scope>: <title>` per decision
  CONVENTIONS.md    how code, files, commits and evidence are done here
  ASSETS.md         the asset registry (name, path, source sprint, status, notes)
  KNOWN_ISSUES.md   open quirks and pitfalls
  tracks/<name>.md  per-track notes (parallel tracks, tl-tracks)
  sprints/<ID>.md   per-sprint notes a session may append

`init()` creates them idempotently; `file_ok()` is the one place that says which
relative paths a note or a chat edit may target. Nothing here touches git: the
manager commits by explicit path.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

MEMORY_DIR = "docs/memory"
FIXED_FILES = ("DECISIONS.md", "CONVENTIONS.md", "ASSETS.md", "KNOWN_ISSUES.md")
SUBDIRS = ("tracks", "sprints")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")

TEMPLATES = {
    "DECISIONS.md": """# Decisions — {name}

Project-wide decisions, newest last. One `## <scope>: <short title>` heading per decision; the
body says what was decided, why, and which sprint decided it. Sessions append through
`MEMORY_NOTE` blocks in their final report; the operator edits from the Memory tab or the chat.
The coordinator indexes this file by heading and sends each session the decisions that match
its sprint, plus the most recent ones.
""",
    "CONVENTIONS.md": """# Conventions — {name}

How code, files, commits, tests and evidence are done here, grouped by `## <area>` headings.
A convention is a fact every session must follow, not a plan step. Sessions append through
`MEMORY_NOTE`; keep each entry to one or two sentences.
""",
    "ASSETS.md": """# Asset registry — {name}

Every delivered asset (mesh, texture, audio, image) with where it lives and which sprint made
it. Asset sprints append one table row per deliverable through `MEMORY_NOTE` (file=ASSETS.md,
heading=Registry). Status is one of planned | generated | imported | verified | retired.

## Registry

| Name | Path | Source sprint | Status | Notes |
|---|---|---|---|---|
""",
    "KNOWN_ISSUES.md": """# Known issues — {name}

Open quirks, pitfalls and workarounds, grouped by `## <area>` headings. When one is fixed, move
its entry under `## Resolved` with the sprint that fixed it rather than deleting it.

## Resolved
""",
}


def memory_dir(workspace: str) -> str:
    return os.path.join(workspace, *MEMORY_DIR.split("/"))


def file_ok(rel: str) -> Optional[str]:
    """The normalized memory-relative path ("DECISIONS.md", "tracks/ui.md",
    "sprints/UI-02b.md") for a path a note or the chat names, or None when it is
    not a memory file. Accepts the `docs/memory/` prefix and backslashes."""
    p = (rel or "").replace("\\", "/").strip().strip("/")
    if p.lower().startswith(MEMORY_DIR + "/"):
        p = p[len(MEMORY_DIR) + 1:]
    if not p or ".." in p.split("/"):
        return None
    parts = p.split("/")
    if len(parts) == 1:
        for f in FIXED_FILES:
            if parts[0].lower() == f.lower():
                return f
        return None
    if len(parts) == 2 and parts[0] in SUBDIRS:
        name = parts[1]
        if not name.lower().endswith(".md"):
            name += ".md"
        return f"{parts[0]}/{name}" if _NAME.match(name[:-3]) else None
    return None


def rel_path(file: str) -> str:
    """Repo-relative path of a memory file ("docs/memory/DECISIONS.md")."""
    return f"{MEMORY_DIR}/{file}"


def init(workspace: str, name: str = "", clock=time.time) -> dict:
    """Create the layout (idempotent). Returns {"created": [repo-relative paths],
    "existing": [...]} so the caller can commit exactly the new files."""
    root = memory_dir(workspace)
    created, existing = [], []
    os.makedirs(root, exist_ok=True)
    for f in FIXED_FILES:
        path = os.path.join(root, f)
        if os.path.isfile(path):
            existing.append(rel_path(f))
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(TEMPLATES[f].format(name=name or os.path.basename(
                os.path.abspath(workspace))))
        created.append(rel_path(f))
    for d in SUBDIRS:
        dpath = os.path.join(root, d)
        os.makedirs(dpath, exist_ok=True)
        keep = os.path.join(dpath, ".gitkeep")
        if os.path.isfile(keep) or any(x.endswith(".md") for x in os.listdir(dpath)):
            existing.append(rel_path(f"{d}/.gitkeep"))
            continue
        with open(keep, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("")
        created.append(rel_path(f"{d}/.gitkeep"))
    return {"created": created, "existing": existing, "dir": rel_path("").rstrip("/")}


def exists(workspace: str) -> bool:
    root = memory_dir(workspace)
    return os.path.isdir(root) and any(os.path.isfile(os.path.join(root, f)) for f in FIXED_FILES)


def new_file_text(file: str, name: str = "") -> str:
    """The starting text of a memory file created by a note or a chat edit."""
    if file in TEMPLATES:
        return TEMPLATES[file].format(name=name)
    kind, _, base = file.partition("/")
    stem = base[:-3] if base.lower().endswith(".md") else base
    if kind == "sprints":
        return f"# Sprint {stem}\n\nNotes a session or the coordinator recorded for sprint {stem}.\n"
    return f"# Track {stem}\n\nNotes for the parallel track {stem}.\n"


def list_files(workspace: str) -> list[str]:
    """Memory-relative paths of every .md file in the layout, fixed files first."""
    root = memory_dir(workspace)
    if not os.path.isdir(root):
        return []
    out = [f for f in FIXED_FILES if os.path.isfile(os.path.join(root, f))]
    for d in SUBDIRS:
        dpath = os.path.join(root, d)
        if os.path.isdir(dpath):
            out += sorted(f"{d}/{x}" for x in os.listdir(dpath)
                          if x.lower().endswith(".md") and os.path.isfile(os.path.join(dpath, x)))
    return out
