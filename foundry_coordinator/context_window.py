"""A session model's context window, and the thresholds relative to it (2026-10-27).

Victor: "we are allowing 262k context and output reserve is currently at 32768". Every
context threshold is a share of the session model's USABLE window (context minus the
output reserve), read from the Kilo config the session runs with
(provider.<id>.models.<persona>.limit {context, output}):

  coordinator handoff   [automation] context_ceiling_pct (95)
  handoff lane          [lanes.handoff] context_pct (90)
  session zones         Amber 0.75, Red 0.90 (written for the project's checkpoint tool)
  running-session stop  min(Red, usable - 24k): the coordinator stops a BUSY session there,
                        asks it to commit and report, then hands off (engine _context_guard)

A fixed number (context_ceiling_tokens, context_threshold) is used only when the window
is unknown. The local Qwen persona (262,144 / 32,768) has 229,376 usable tokens: handoff
~217.9k, Amber ~172k, Red ~206k. A 200k Claude persona (200,000 / 8,192): 191,808 usable,
handoff ~182k, Amber ~144k, Red ~173k.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

AMBER = 0.75
RED = 0.90
FILE = os.path.join(".plan", "tmp", "context_window.json")


def model_limits(P, persona: str) -> Optional[tuple[int, int]]:
    """(context, output) of `persona` in the project's Kilo config (then the global one),
    or None when no config names it."""
    if not persona:
        return None
    from foundry_automation import jsonc
    from foundry_automation import service_map as SM
    from .kilo_config import kilo_path
    paths = []
    try:
        paths.append(kilo_path(P.profile))
    except Exception:                                             # noqa: BLE001
        pass
    paths += list(SM.global_kilo_paths())
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = jsonc.parse(f.read())
        except (OSError, ValueError, jsonc.JsoncError):
            continue
        for prov in ((data or {}).get("provider") or {}).values():
            m = ((prov or {}).get("models") or {}).get(persona) if isinstance(prov, dict) else None
            lim = (m or {}).get("limit") if isinstance(m, dict) else None
            if isinstance(lim, dict) and lim.get("context"):
                try:
                    return int(lim["context"]), int(lim.get("output") or 0)
                except (TypeError, ValueError):
                    return None
    return None


def usable(P, persona: str) -> Optional[int]:
    lim = model_limits(P, persona)
    if not lim:
        return None
    ctx, out = lim
    return max(0, ctx - out) or None


def ceiling(P, persona: str) -> int:
    """The handoff ceiling for a session of `persona`: context_ceiling_pct of its usable
    window, else the fixed context_ceiling_tokens (0 = off)."""
    from .automation import get as auto_get
    fixed = int(auto_get(P, "context_ceiling_tokens") or 0)
    if not fixed:
        return 0                                   # handoff turned off
    try:
        ratio = int(auto_get(P, "context_ceiling_pct") or 0) / 100.0
    except (KeyError, TypeError, ValueError):
        ratio = 0.0
    u = usable(P, persona) if ratio > 0 else None
    return int(u * ratio) if u else fixed


WRAPUP_MARGIN = 24000      # one large tool result (a Studio call returned ~22k tokens)
HARD_MARGIN = 6000         # past this, a still-running session is stopped outright


def context_of(transcript: list) -> Optional[int]:
    """The context of the session's latest assistant turn (input + cache read/write),
    from a Kilo transcript (GET /session/{id}/message), or None."""
    for m in reversed(transcript or []):
        info = (m or {}).get("info") or {}
        if info.get("role") != "assistant":
            continue
        tk = info.get("tokens") or {}
        cache = tk.get("cache") or {}
        n = int(tk.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
        if n:
            return n
    return None


def wrapup(P, persona: str) -> int:
    """Where a RUNNING session is stopped and asked to commit and report (0 = unknown
    window): the Red zone, but never closer than WRAPUP_MARGIN to the usable window.
    The idle-only ceiling alone let GF-GATE1 (2026-10-27) go 207k -> 230k in one Studio
    call and die in Kilo's compaction with two hours of uncommitted work."""
    u = usable(P, persona)
    if not u:
        return 0
    try:
        margin = int(P.profile.limit("context_wrapup_margin_tokens", WRAPUP_MARGIN))
    except (AttributeError, TypeError, ValueError):
        margin = WRAPUP_MARGIN
    return max(1, min(int(u * RED), u - margin))


def hard_stop(P, persona: str) -> int:
    u = usable(P, persona)
    return max(1, u - HARD_MARGIN) if u else 0


def lane_threshold(P, persona: str, lane: dict) -> int:
    """The handoff lane's threshold: lane context_pct of the usable window, else its
    fixed context_threshold."""
    fixed = int(lane.get("context_threshold") or 0)
    try:
        ratio = int(lane.get("context_pct") or 0) / 100.0
    except (TypeError, ValueError):
        ratio = 0.0
    u = usable(P, persona) if ratio > 0 else None
    return int(u * ratio) if u else fixed


def _ignored(root: str, rel: str) -> bool:
    """True when git ignores `rel` in `root` (AU ignores tmp/); a project that doesn't
    would see the file as an unrelated untracked change and fail its clean checks."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root, "check-ignore", "-q", rel.replace(os.sep, "/")],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def write_file(P, persona: str, session_id: str = "", workspace: str = "") -> Optional[dict]:
    """<workspace>/.plan/tmp/context_window.json for the project's checkpoint tool (AU
    tools/guard/checkpoint.py): the session's window and its Amber/Red zones."""
    lim = model_limits(P, persona)
    if not lim:
        return None
    ctx, out = lim
    u = max(0, ctx - out)
    doc = {"persona": persona, "session_id": session_id, "context": ctx, "output": out,
           "usable": u, "amber": int(u * AMBER), "red": int(u * RED),
           "handoff": ceiling(P, persona), "ts": time.time()}
    root = workspace or P.profile.workspace
    path = os.path.join(root, FILE)
    if not _ignored(root, FILE):
        return None                    # never leave an untracked file in a project's tree
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        return None
    return doc
