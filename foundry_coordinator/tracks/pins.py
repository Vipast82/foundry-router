"""Count pins (COORDINATOR_AUTOMATION_NEEDS item 21).

A test that pins a count ("the 54 service files", "54 services loaded, 29 driven") is edited
by every lane that adds one of the counted things. Two lanes each write 53 -> 54; merged,
the line either conflicts or - worse - merges cleanly and says 54 when there are 55, and
the merge gate fails its boot check (VQ-02 fixed EXPECTED_BOOT by hand after BS-4). The
RegressionIndex entry count already resolved itself (merge.py COUNTED_SPECS); this is the
same for any pin a project lists.

A pin is one line of `[automation] count_pins`:

    <file> :: <regex with one group: the number> :: <glob of the counted files>[ :: <regex>]

The count is the number of files the glob matches (under the worktree), or, with the last
part, the number of those files whose text matches that regex. Empty setting = the
project adapter's own list (`count_pins()`), if it has one.

  fix        after a lane is rebased onto master, each pin's number is set to the real
             count; a change is committed on the lane ("count pins: ...") before the merge
             gate runs, and is an event.
  conflicts  a conflict hunk in a pin's file whose two sides are the same once the pinned
             numbers and `--` comment lines are left out resolves itself: one side, the
             other side's comment lines added, the numbers set by `fix` afterwards. (Two
             lanes that both wrote 54 conflict only on their comment lines.) Any other
             hunk in that file still stops for the Lead repair or a human, as before.
"""

from __future__ import annotations

import glob as _glob
import os
import re
from dataclasses import dataclass
from typing import Optional

SEP = "::"


@dataclass
class Pin:
    file: str
    pattern: "re.Pattern"
    glob: str
    contains: Optional["re.Pattern"] = None
    line: str = ""


def parse(lines) -> list:
    """Pins from setting lines; raises ValueError naming the bad line."""
    out = []
    for raw in lines or []:
        parts = [p.strip() for p in str(raw).split(SEP)]
        if len(parts) not in (3, 4) or not all(parts[:3]):
            raise ValueError(f"count pin {raw!r}: want `file :: regex :: glob[ :: regex]`")
        try:
            pat = re.compile(parts[1])
            has = re.compile(parts[3]) if len(parts) == 4 and parts[3] else None
        except re.error as e:
            raise ValueError(f"count pin {raw!r}: {e}") from None
        if pat.groups != 1:
            raise ValueError(f"count pin {raw!r}: the regex needs exactly one group (the number)")
        out.append(Pin(parts[0].replace("\\", "/"), pat, parts[2].replace("\\", "/"), has,
                       str(raw)))
    return out


def for_project(P, setting) -> list:
    """The project's pins: the setting, else the adapter's own list. Never raises: a bad
    line gives no pins (settings validation already refused it at deploy)."""
    lines = list(setting or [])
    if not lines:
        fn = getattr(getattr(P, "adapter", None), "count_pins", None)
        try:
            lines = list(fn()) if callable(fn) else []
        except Exception:                                        # noqa: BLE001
            lines = []
    try:
        return parse(lines)
    except ValueError:
        return []


def count(wt: str, pin: Pin) -> Optional[int]:
    """The real count behind a pin, or None when it cannot be taken (no file matched a
    glob that names a folder that does not exist: never write 0 over a pin by mistake)."""
    base = os.path.join(wt, *pin.glob.split("/")[:-1])
    if not os.path.isdir(os.path.dirname(os.path.join(wt, *pin.glob.split("/")))) \
            and not os.path.isdir(base):
        return None
    files = [f for f in _glob.glob(os.path.join(wt, *pin.glob.split("/")), recursive=True)
             if os.path.isfile(f)]
    if pin.contains is None:
        return len(files)
    n = 0
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                if pin.contains.search(fh.read()):
                    n += 1
        except OSError:
            return None
    return n


def _set(text: str, pin: Pin, n: int) -> tuple:
    """(text with every match's number set to n, the old numbers that were different)."""
    was: list = []

    def put(m):
        if m.group(1) == str(n):
            return m.group(0)
        was.append(m.group(1))
        a, b = m.start(1) - m.start(0), m.end(1) - m.start(0)
        return m.group(0)[:a] + str(n) + m.group(0)[b:]
    return pin.pattern.sub(put, text), was


def fix(wt: str, pins: list) -> list:
    """Set every pin to its real count. Returns [{"file", "from", "to", "pin"}] for the
    numbers that changed; files are rewritten in place (newlines kept)."""
    out = []
    by_file: dict = {}
    for p in pins:
        by_file.setdefault(p.file, []).append(p)
    for rel, plist in by_file.items():
        path = os.path.join(wt, *rel.split("/"))
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except OSError:
            continue
        if "<<<<<<< " in text:
            continue                                 # an unresolved conflict: not ours to touch
        new = text
        for p in plist:
            n = count(wt, p)
            if n is None:
                continue
            new, was = _set(new, p, n)
            for w in was:
                out.append({"file": rel, "from": int(w), "to": n, "pin": p.line})
        if new != text:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
    return out


_HUNK = re.compile(r"<<<<<<< [^\r\n]*\r?\n(.*?)(?:\|\|\|\|\|\|\| [^\r\n]*\r?\n.*?)?=======\r?\n"
                   r"(.*?)>>>>>>> [^\r\n]*\r?\n", re.S)


def resolve(text: str, pins: list) -> str:
    """A pin file's conflict hunks resolved (see the module text). Raises ValueError when a
    hunk differs in more than pinned numbers and comment lines."""
    nl = "\r\n" if "\r\n" in text else "\n"

    def bare(side: str) -> list:
        s = side
        for p in pins:
            s, _ = _set(s, p, 0)
        return [l.strip() for l in s.splitlines() if l.strip() and not l.strip().startswith("--")]

    def merge(m) -> str:
        ours, theirs = m.group(1), m.group(2)
        if bare(ours) != bare(theirs):
            raise ValueError("a hunk differs in more than a pinned count")
        have = {l.strip() for l in theirs.splitlines()}
        notes = [l for l in ours.splitlines() if l.strip().startswith("--")
                 and l.strip() not in have]
        if not notes:
            return theirs
        lines = theirs.splitlines()
        # the lane's comment lines go after master's last comment line, else on top
        at = max((i + 1 for i, l in enumerate(lines) if l.strip().startswith("--")), default=0)
        return nl.join(lines[:at] + notes + lines[at:]) + nl
    return _HUNK.sub(merge, text)
