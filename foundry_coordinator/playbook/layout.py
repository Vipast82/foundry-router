"""Playbook repo layout (A1, 2026-10-23): the cross-project knowledge base.

The playbook is its own git repository (path per install: --playbook or
$FOUNDRY_PLAYBOOK), shared by every project the coordinator runs. It holds the
org-wide reusables the per-project memory deliberately leaves out:

  templates/<name>.md   sprint plans, closeout, evidence packet shapes
  howto/<name>.md       recurring procedures
  fixes/<name>.md       error signature -> verified fix
  scripts/<name>.md     reusable scripts as fenced code blocks, with usage
  formats/<name>.md     formatting standards

Everything is markdown in the same heading-entry format as docs/memory, so the
memory indexer, slicer and note machinery work on it unchanged. A script lives
inside its scripts/<name>.md as a fenced block (the entry parser keeps fences
inside the entry). `file_ok()` is the one place that says which relative paths
a PLAYBOOK_NOTE or a chat edit may target.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# the reserved project id the playbook is indexed under (memory_* tables are
# keyed by project_id; "@" can never collide with a real project id)
PLAYBOOK_ID = "@playbook"
SUBDIRS = ("templates", "howto", "fixes", "scripts", "formats")
NOTE_KEYWORD = "PLAYBOOK_NOTE"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")

README = """# Foundry playbook

The cross-project knowledge base every coordinator session can read: templates,
how-tos, verified fixes, scripts and formatting standards. One `## heading` per
entry; the coordinator indexes entries by heading and sends each session the
ones that match its role, files and needs — cross-project, beside the
project's own docs/memory slice.

- `templates/` sprint plans, closeout and evidence shapes
- `howto/` recurring procedures
- `fixes/` one `## <signature>: <title>` per verified fix: what the error
  looks like, the cause, the fix, where it was seen
- `scripts/` reusable scripts as fenced code blocks with usage notes
- `formats/` formatting standards

Sessions contribute through `PLAYBOOK_NOTE` lines in their final report
(file=fixes/<name>.md heading="..." text="..."); the coordinator validates and
commits. The operator edits from the Memory tab (playbook scope) or the chat
(kb_add / kb_edit). Facts and procedures, never credentials.
"""


def file_ok(rel: str) -> Optional[str]:
    """The normalized playbook-relative path ("fixes/rojo.md") for a path a note
    or the chat names, or None when it is not a playbook file. Accepts
    backslashes and a missing .md."""
    p = (rel or "").replace("\\", "/").strip().strip("/")
    if not p or ".." in p.split("/"):
        return None
    parts = p.split("/")
    if len(parts) != 2 or parts[0] not in SUBDIRS:
        return None
    name = parts[1]
    if not name.lower().endswith(".md"):
        name += ".md"
    return f"{parts[0]}/{name}" if _NAME.match(name[:-3]) else None


def files_hint() -> str:
    return "one of " + ", ".join(f"{d}/<name>.md" for d in SUBDIRS)


def init(path: str) -> dict:
    """Create the layout (idempotent; the caller ensures the git repo exists).
    Returns {"created": [repo-relative paths], "existing": [...]}."""
    created, existing = [], []
    os.makedirs(path, exist_ok=True)
    readme = os.path.join(path, "README.md")
    if os.path.isfile(readme):
        existing.append("README.md")
    else:
        with open(readme, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(README)
        created.append("README.md")
    for d in SUBDIRS:
        dpath = os.path.join(path, d)
        os.makedirs(dpath, exist_ok=True)
        keep = os.path.join(dpath, ".gitkeep")
        if os.path.isfile(keep) or any(x.lower().endswith(".md") for x in os.listdir(dpath)):
            existing.append(f"{d}/.gitkeep")
            continue
        with open(keep, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("")
        created.append(f"{d}/.gitkeep")
    return {"created": created, "existing": existing}


def exists(path: str) -> bool:
    return bool(path) and os.path.isdir(path) and \
        any(os.path.isdir(os.path.join(path, d)) for d in SUBDIRS)


def new_file_text(file: str) -> str:
    """The starting text of a playbook file created by a note or a chat edit."""
    kind, _, base = file.partition("/")
    stem = base[:-3] if base.lower().endswith(".md") else base
    what = {"templates": "Template", "howto": "How-to", "fixes": "Fixes",
            "scripts": "Script", "formats": "Format"}.get(kind, "Playbook")
    return f"# {what}: {stem}\n\nCross-project playbook entries; one `## heading` per entry.\n"


def list_files(path: str) -> list[str]:
    """Playbook-relative paths of every .md entry file, by directory."""
    if not path or not os.path.isdir(path):
        return []
    out = []
    for d in SUBDIRS:
        dpath = os.path.join(path, d)
        if os.path.isdir(dpath):
            out += sorted(f"{d}/{x}" for x in os.listdir(dpath)
                          if x.lower().endswith(".md") and os.path.isfile(os.path.join(dpath, x)))
    return out
