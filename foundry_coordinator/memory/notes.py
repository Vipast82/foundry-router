"""Session memory notes: the structured report block a session writes to record
a fact, and the edits the coordinator applies to a memory file.

    MEMORY_NOTE: file=DECISIONS.md heading="Studio: gate order" text="Stories run before the
    gate; the gate never re-runs a story that passed at the same head (UI-02b)."

One note per line (bare values run to the next ` key=`; quoted values may hold
spaces and `\\"`); `MEMORY_NOTE: {"file": ..., "heading": ..., "text": ...}` is
accepted too. The coordinator validates (known file, size cap, no secrets),
appends under the heading (a new `## heading` section when the heading is not
in the file), commits by explicit path and re-indexes. Reviewed facts, never
free prose: the text is stored as given, as one list item (or one table row
when it starts with `|`).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from . import layout

NOTE_LINE = re.compile(r"^[ \t>*-]*MEMORY_NOTE:\s*(.+?)\s*$", re.M)
_KV = re.compile(r'(\w+)=("((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|((?:(?!\s\w+=).)+))')
MAX_HEADING = 120
MAX_NOTES_PER_REPORT = 12
SECRETS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API secret key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS"), "Roblox .ROBLOSECURITY cookie"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"), "bearer token"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|x-api-key|open[_-]?cloud[_-]?key)"
                r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_\-]{12,}"), "credential assignment"),
]


def parse_notes(text: str) -> list[dict]:
    """Every MEMORY_NOTE line of a session's final message, in order: {file,
    heading, text, raw}. Malformed lines come back with "error" set so they can
    be reported, never silently dropped."""
    out = []
    for m in NOTE_LINE.finditer(text or ""):
        body = m.group(1).strip()
        note: dict = {"raw": body[:600]}
        if body.startswith("{"):
            try:
                d = json.loads(body)
            except ValueError:
                note["error"] = "not valid JSON"
                out.append(note)
                continue
            if not isinstance(d, dict):
                note["error"] = "not a JSON object"
                out.append(note)
                continue
            note.update({k: str(d.get(k) or "") for k in ("file", "heading", "text")})
        else:
            kv: dict = {}
            for km in _KV.finditer(body):
                key = km.group(1).lower()
                val = km.group(3) if km.group(3) is not None else km.group(4) \
                    if km.group(4) is not None else km.group(5)
                val = (val or "").strip()
                if km.group(3) is not None or km.group(4) is not None:
                    val = re.sub(r'\\(["\'\\])', r"\1", val)
                kv[key] = val
            if not kv:
                note["error"] = "no file=/heading=/text= fields"
                out.append(note)
                continue
            note.update({k: kv.get(k, "") for k in ("file", "heading", "text")})
        out.append(note)
    return out


def find_secret(text: str) -> str:
    for rx, what in SECRETS:
        if rx.search(text or ""):
            return what
    return ""


def validate(note: dict, max_chars: int = 1500) -> str:
    """"" when the note may be applied, else the reason."""
    if note.get("error"):
        return note["error"]
    file = layout.file_ok(note.get("file") or "")
    if not file:
        return (f"unknown memory file {note.get('file')!r} (one of "
                f"{', '.join(layout.FIXED_FILES)}, tracks/<name>.md, sprints/<ID>.md)")
    heading = (note.get("heading") or "").strip()
    if not heading:
        return "heading is empty"
    if len(heading) > MAX_HEADING:
        return f"heading is over {MAX_HEADING} characters"
    if "\n" in heading or heading.startswith("#"):
        return "heading must be one line without '#'"
    text = (note.get("text") or "").strip()
    if not text:
        return "text is empty"
    if len(text) > max_chars:
        return f"text is over {max_chars} characters"
    what = find_secret(text) or find_secret(heading)
    if what:
        return f"looks like a secret ({what}); memory never holds credentials"
    return ""


# ---------------------------------------------------------------------------- file edits

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip("#").strip()).lower()


def _sections(lines: list[str]) -> list[tuple[int, int, str, int]]:
    """[(start line of the heading, end line exclusive, title, level)] for every
    heading; fenced code is skipped."""
    heads = []
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = _HEADING.match(line.rstrip("\r"))
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out = []
    for k, (i, lvl, title) in enumerate(heads):
        end = len(lines)
        for j, lvl2, _ in heads[k + 1:]:
            if lvl2 <= lvl:
                end = j
                break
        out.append((i, end, title, lvl))
    return out


def find_section(text: str, heading: str) -> Optional[tuple[int, int, int]]:
    """(start, end, level) of the section whose title matches `heading` (the
    whole "A > B" path, or its last component), case-insensitively; None when
    the file has no such heading. The last match wins (newest section)."""
    lines = text.split("\n")
    want = _norm(heading.split(">")[-1])
    full = [_norm(p) for p in heading.split(">")]
    hit = None
    secs = _sections(lines)
    for i, end, title, lvl in secs:
        if _norm(title) == want:
            if len(full) > 1:
                # check the path: the enclosing headings
                path = [t for s, e, t, l in secs if s < i and e > i and l < lvl]
                if [_norm(p) for p in path][-(len(full) - 1):] != full[:-1]:
                    continue
            hit = (i, end, lvl)
    return hit


def _as_item(text: str) -> str:
    t = text.strip()
    if t.startswith("|") or re.match(r"^(?:[-*+]|\d+[.)])\s", t) or t.startswith("```"):
        return t
    return "- " + " ".join(t.split("\n"))


def append_to_section(text: str, heading: str, body: str, level: int = 2) -> str:
    """`body` (as one list item or table row) at the end of the heading's
    section, after its last non-blank line; a new `## heading` section at the
    end of the file when the heading is missing. Keeps the file's line ends."""
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    item = _as_item(body)
    lines = text.split("\n")
    hit = find_section(text, heading)
    if hit is None:
        tail = text.rstrip("\n")
        marks = "#" * max(2, min(6, level))
        new = (tail + "\n\n" if tail else "") + f"{marks} {heading.strip()}\n\n{item}\n"
    else:
        start, end, lvl = hit
        # the section's direct body ends at its first sub-heading
        direct_end = end
        fence = False
        for i in range(start + 1, end):
            if lines[i].lstrip().startswith("```"):
                fence = not fence
            elif not fence and _HEADING.match(lines[i]):
                direct_end = i
                break
        last = direct_end - 1
        if item.startswith("|"):
            # a table row joins the section's table
            rows = [i for i in range(start + 1, direct_end) if lines[i].strip().startswith("|")]
            if rows:
                last = rows[-1]
        while last > start and not lines[last].strip():
            last -= 1
        if last == start:
            # an empty section: a blank line between the heading and the item
            lines[start + 1:start + 1] = ["", item]
        elif item.startswith("|") and lines[last].strip().startswith("|"):
            lines[last + 1:last + 1] = [item]
        else:
            lines[last + 1:last + 1] = [item]
        new = "\n".join(lines)
        if not new.endswith("\n"):
            new += "\n"
    return new.replace("\n", "\r\n") if crlf else new


def replace_section(text: str, heading: str, body: str) -> Optional[str]:
    """The section's body (everything under the heading up to the next heading
    of the same or a higher level) replaced by `body`; None when the heading is
    missing."""
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    hit = find_section(text, heading)
    if hit is None:
        return None
    start, end, _ = hit
    lines = text.split("\n")
    new_body = body.strip("\n").split("\n") if body.strip() else []
    lines[start + 1:end] = [""] + new_body + [""]
    new = "\n".join(lines)
    new = re.sub(r"\n{3,}", "\n\n", new)
    if not new.endswith("\n"):
        new += "\n"
    return new.replace("\n", "\r\n") if crlf else new
