"""Proactive helper lanes: deterministic event ingestion and lane policy.

Every lane is OPTIONAL frontier work that runs through the existing helper
journal (helpers.py): admission on fresh quota below the cutoff, binding with a
coordinator-chosen optional purpose before any inference, read-only sessions,
delivery only when the primary is idle. Nothing here makes a model call.

While the primary works, the coordinator reads its transcript (GET only, rate
limited) and classifies COMPLETED tool results with plain code:

  capture     the capture procedure's record (VALID=/CAPTURE/Saved:) -> an
              immutable artifact snapshot (artifacts.py) -> visual observer
  failure     a test/gate/tool command that failed -> normalized signature
              (numbers, hashes, paths, times stripped) -> failure triage, one
              unit per signature (repeats are coalesced, not re-dispatched)
  checkpoint  a commit / HEAD move -> a consistent source snapshot ->
              incremental review of what changed since the last reviewed one
  gate        a GATE <task> PASS|FAIL line -> evidence reconciliation
  api         a Roblox/Luau API error (not a valid member, nil method,
              deprecated, unknown global) -> documentation research per symbol
  context     the primary's context occupancy crosses the handoff threshold
  run_start   a new implementation run -> preflight / independent test design

All lanes default OFF ([lanes] enabled = false and every lane enabled = false);
existing [helpers] behavior is unchanged when they are off.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Optional

from .profile import matches_any

SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_events (
  event_key TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, session_id TEXT,
  kind TEXT, signature TEXT, detail TEXT, part_id TEXT, status TEXT, tries INTEGER DEFAULT 0,
  unit_id TEXT, count INTEGER DEFAULT 1, ts REAL, updated_ts REAL);
CREATE INDEX IF NOT EXISTS idx_lane_events_task ON lane_events(project_id, task_id, kind);
CREATE TABLE IF NOT EXISTS lane_cache (
  cache_key TEXT PRIMARY KEY, project_id TEXT, lane TEXT, unit_id TEXT, result TEXT, ts REAL);
"""

# lane -> (unit kind = Foundry optional purpose, execution mode, base priority)
LANES = {
    "failure_triage": ("failure_triage", "kilo", 0),
    "incremental_review": ("incremental_review", "kilo", 1),
    "test_design": ("test_design", "kilo", 1),
    "proposer": ("code_proposal", "kilo", 1),
    "evidence": ("evidence_reconciliation", "kilo", 2),
    "handoff": ("handoff_prep", "kilo", 2),
    "visual": ("visual_observation", "direct", 3),
    "research": ("research", "kilo", 3),
    "preflight": ("preflight", "kilo", 4),
    # v0.126: ahead-of-time work for the primary's next plan steps (draft patch,
    # exact commands, gotchas) written to shared memory the primary reads
    "prework": ("prework", "kilo", 1),
}
# lanes with their own budget: they never use the task-wide lane unit /
# dispatch caps that reactive lanes share (so triage cannot starve them)
OWN_BUDGET_LANES = ("prework",)
KIND_LANE = {v[0]: k for k, v in LANES.items()}
LANE_KINDS = tuple(v[0] for v in LANES.values())
SEVERITY = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

LANE_DEFAULTS = {
    "failure_triage": {"max_per_task": 4, "max_running": 2},
    "incremental_review": {"max_per_task": 4, "max_running": 1, "min_interval_seconds": 600},
    "test_design": {"max_per_task": 2, "max_running": 1},
    "proposer": {"max_per_task": 6, "max_running": 2,
                 "skills": ["au-luau-development", "au-roblox-development"],
                 "ui_skills": ["au-roblox-ui-development"]},
    "evidence": {"max_per_task": 3, "max_running": 1},
    "handoff": {"max_per_task": 2, "max_running": 1, "context_threshold": 140000},
    "visual": {"max_per_task": 6, "max_running": 1, "debounce_seconds": 20,
               "persona": "Foundry-Kilo-Visual-Review",
               "capture_globs": ["docs/evidence/{task}/*.png", "docs/evidence/{task}/*.jpg"],
               "reference_globs": [], "max_image_bytes": 8_000_000, "fs_fallback": False,
               "min_confidence": 0.75, "min_severity": "medium", "propose": True,
               "skills": ["au-ui-review"], "max_tokens": 3000, "viewport": "",
               "retention_days": 30},
    "research": {"max_per_task": 4, "max_running": 1, "cache_hours": 168, "max_lookups": 6},
    "preflight": {"max_per_task": 1, "max_running": 1},
    "prework": {"max_per_task": 60, "max_running": 2, "steps_ahead": 2,
                "dir": ".plan/tmp/prework",
                "skills": ["au-luau-development", "au-roblox-development"],
                "ui_skills": ["au-roblox-ui-development"]},
}


def settings(P) -> dict:
    raw = dict((getattr(P.profile, "raw", None) or {}).get("lanes") or {})
    out = {
        "enabled": bool(raw.get("enabled", False)),
        "transcript_poll_seconds": float(raw.get("transcript_poll_seconds", 20)),
        "max_lane_units_per_task": int(raw.get("max_lane_units_per_task", 24)),
        "max_dispatches_per_task": int(raw.get("max_dispatches_per_task", 16)),
        # v0.126.2: the two caps above are per RUN; this lifetime ceiling still
        # spans every run of the task (runaway guard), default 4x the run cap
        "max_lane_units_lifetime": int(raw.get("max_lane_units_lifetime",
                                               4 * int(raw.get("max_lane_units_per_task", 24)))),
        "max_cards_per_delivery": int(raw.get("max_cards_per_delivery", 6)),
        "max_deliveries_per_session": int(raw.get("max_deliveries_per_session", 3)),
        "min_delivery_severity": str(raw.get("min_delivery_severity", "medium")),
        "aging_seconds": float(raw.get("aging_seconds", 900)),
        "max_retries": int(raw.get("max_retries", 1)),
        "high_value_lanes": list(raw.get("high_value_lanes") or []),
        "skill_dirs": list(raw.get("skill_dirs") or [".kilo/skills"]),
        "snapshot_max_bytes": max(1, min(8_000_000, int(raw.get("snapshot_max_bytes", 400_000)))),
        "failure_commands": list(raw.get("failure_commands") or [
            "lune", "gate.ps1", "done_check", "pytest", "rojo build", "selene", "luau-analyze",
            "stylua", "run_regression", "GateRunner"]),
        "lanes": {},
    }
    for lane, dflt in LANE_DEFAULTS.items():
        sec = dict(raw.get(lane) or {})
        merged = {**dflt, **sec}
        merged["enabled"] = bool(out["enabled"] and sec.get("enabled", False))
        out["lanes"][lane] = merged
    return out


def lane_on(ls: dict, lane: str) -> bool:
    return bool(ls["enabled"] and ls["lanes"].get(lane, {}).get("enabled"))


# --------------------------------------------------------------------------- classifiers

_NUM = re.compile(r"\d+(?:\.\d+)?")
_HEX = re.compile(r"\b[0-9a-f]{7,64}\b", re.I)
_PATHS = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\- ]+){2,}")
_TIME = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?")
_FAIL_LINE = re.compile(r"^\s*(?:FAIL\b|FAILED\b|LUNE \d+/\d+ FAIL|GATE \S+ FAIL|NEGATIVE CONTROL "
                        r"FAIL|DONE-CHECK \S+ FAIL|error[:\[]|Error:|.*Traceback \(most recent|"
                        r".*\bassert(?:ion)? failed|.*\bexpected .* got\b)", re.I)
_API = [
    (re.compile(r"(\w+) is not a valid member of (\w+)"), lambda m: f"{m.group(2)}.{m.group(1)}"),
    (re.compile(r"attempt to call a nil value \((?:method|field) '(\w+)'\)"),
     lambda m: f"nil:{m.group(1)}"),
    (re.compile(r"'?(\w+(?:\.\w+)?)'? is deprecated"), lambda m: f"deprecated:{m.group(1)}"),
    (re.compile(r"Unknown global '(\w+)'"), lambda m: f"global:{m.group(1)}"),
    (re.compile(r"Key '(\w+)' not found in (?:class|table) '(\w+)'"),
     lambda m: f"{m.group(2)}.{m.group(1)}"),
]


def normalize_failure(lines: list[str]) -> str:
    out = []
    for l in lines:
        l = _TIME.sub("T", l)
        l = _PATHS.sub("<path>", l)
        l = _HEX.sub("H", l)
        l = _NUM.sub("N", l)
        out.append(re.sub(r"\s+", " ", l).strip())
    return hashlib.sha256("\n".join(sorted(set(out))[:8]).encode()).hexdigest()[:16]


def tool_view(part: dict) -> Optional[dict]:
    """A completed (or errored) tool call, flattened. Running/pending -> None."""
    if part.get("type") != "tool":
        return None
    st = part.get("state") or {}
    if st.get("status") not in ("completed", "error"):
        return None
    inp = st.get("input")
    cmd = (inp.get("command") or inp.get("cmd") or "") if isinstance(inp, dict) else str(inp or "")
    out = st.get("output") if isinstance(st.get("output"), str) else json.dumps(st.get("output")) \
        if st.get("output") is not None else ""
    meta = st.get("metadata") or {}
    exit_code = meta.get("exit") if isinstance(meta, dict) else None
    if exit_code is None and isinstance(meta, dict):
        exit_code = meta.get("exitCode")
    return {"tool": part.get("tool") or "", "command": str(cmd)[:2000], "output": out or "",
            "error": str(st.get("error") or ""), "status": st.get("status"),
            "exit": exit_code, "part_id": part.get("id") or "", "call_id": part.get("callID") or "",
            "input": inp}


def classify(tv: dict, task_id: str, ls: dict) -> list[dict]:
    """Events from one completed tool call: [{kind, key, signature, detail}]."""
    ev = []
    text = (tv["output"] or "") + ("\n" + tv["error"] if tv["error"] else "")
    pid = tv["part_id"]
    # captures: the procedure's own record
    from .artifacts import parse_capture_output
    for rec in parse_capture_output(text):
        ev.append({"kind": "capture", "key": f"capture:{pid}:{rec['path']}",
                   "signature": rec["path"], "detail": rec})
    # gate verdicts
    for m in re.finditer(r"^GATE %s (PASS|FAIL)\b.*$" % re.escape(task_id), text, re.M):
        ev.append({"kind": "gate", "key": f"gate:{pid}:{m.group(1)}", "signature": m.group(1),
                   "detail": {"line": m.group(0)[:300], "command": tv["command"][:300]}})
    # Match execution, not a test filename mentioned in a read/search command.
    command = tv["command"]
    reading = bool(re.search(r"\boutline\.py\b",command,re.I) or re.search(
        r"^\s*(?:&\s+)?(?:rg|grep|Select-String|Get-Content|cat|head|tail)\b",command,re.I))
    test_like = tv["tool"] in ("bash", "shell") and any(
        c.lower() in command.lower() for c in ls["failure_commands"]) and not reading
    runtime = bool(re.search(r"(?:eval_(?:server|client)_runtime|execute_luau)$", tv["tool"]))
    runtime_error = tv['status'] == 'error'
    runtime_message = tv['error']
    if runtime and not runtime_error:
        try:
            body = json.loads(tv['output'])
            runtime_error = isinstance(body,dict) and (body.get('ok') is False or body.get('isError') is True)
            if runtime_error:
                runtime_message = str(body.get('error') or body.get('content') or body)
        except (ValueError,TypeError):
            pass
    failed_exit = tv["exit"] not in (None, 0, "0")
    lines = [l.strip() for l in text.splitlines() if _FAIL_LINE.match(l)][:40]
    # A Studio evaluation tool error is execution evidence; printed FAIL lines
    # inside a successful negative-control evaluation alone are not.
    if (test_like and (failed_exit or lines or tv["status"] == "error")) or \
            (runtime and runtime_error):
        head = " ".join(tv["command"].split()[:3])
        sig = normalize_failure([head or tv['tool']] + (lines or [runtime_message or f"exit {tv['exit']}"]))
        ev.append({"kind": "failure", "key": f"failure:{pid}", "signature": sig,
                   "detail": {"command": tv["command"][:1000], "exit": tv["exit"],
                              "lines": lines[:20], "excerpt": _excerpt(text, lines)}})
    # commits
    if tv["tool"] in ("bash", "shell") and re.search(r"\bgit\b.*\bcommit\b", tv["command"]) and \
            tv["status"] == "completed" and "nothing to commit" not in text and not failed_exit:
        ev.append({"kind": "commit", "key": f"commit:{pid}", "signature": "",
                   "detail": {"command": tv["command"][:300]}})
    # API uncertainty
    seen = set()
    for rx, sym in _API:
        for m in rx.finditer(text):
            s = sym(m)
            if s not in seen:
                seen.add(s)
                ev.append({"kind": "api", "key": f"api:{pid}:{s}", "signature": s,
                           "detail": {"symbol": s, "line": m.group(0)[:300]}})
    return ev


def _excerpt(text: str, lines: list[str], n: int = 6000) -> str:
    if not lines:
        return text[-n:]
    i = text.find(lines[0])
    start = max(0, i - 1500)
    return text[start:start + n]


def context_tokens(info: dict) -> Optional[int]:
    tok = (info or {}).get("tokens") or {}
    if not tok:
        return None
    cache = tok.get("cache") or {}
    return int(tok.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)


# --------------------------------------------------------------------------- envelopes

RESULT_FENCE = "helper-result"
_RESULT_RE = re.compile(r"```" + RESULT_FENCE + r"\s*\n(.*?)```", re.S)


def envelope_instructions(unit_id: str, base: str) -> str:
    return ("End your final message with ONE fenced block (the coordinator parses it; prose "
            "above it is shown to people):\n```" + RESULT_FENCE + "\n"
            + json.dumps({"assignment": unit_id, "base": base, "summary": "<one line>",
                          "findings": [{"id": "F1", "severity": "high|medium|low|info",
                                        "confidence": 0.0, "observed": "<what the evidence "
                                        "shows>", "hypothesis": "<inferred cause, if any>",
                                        "paths": ["<path>:<line>"], "in_scope": True}],
                          "patch": "<unified diff against base, or empty>",
                          "affected_paths": [], "checks_run": [], "checks_proposed": [],
                          "skills_used": [], "sources": [], "limitations": "",
                          "next_action": "<one concrete step for the primary>"})
            + "\n```\nchecks_run lists only checks you actually executed (helpers normally run "
              "none); everything else is checks_proposed. Never claim a test passed.")


def parse_envelope(text: str) -> dict:
    m = None
    for m in _RESULT_RE.finditer(text or ""):
        pass
    if not m:
        return {"structured": False, "summary": (text or "").strip()[:300], "findings": []}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"structured": False, "parse_error": "helper-result block is not JSON",
                "summary": (text or "").strip()[:300], "findings": []}
    if not isinstance(d, dict):
        return {"structured": False, "summary": "", "findings": []}
    fs = []
    for f in (d.get("findings") or [])[:30]:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "info").lower()
        try:
            conf = max(0.0, min(1.0, float(f.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        fs.append({"id": str(f.get("id") or f"F{len(fs) + 1}")[:20],
                   "severity": sev if sev in SEVERITY else "info", "confidence": conf,
                   "observed": str(f.get("observed") or "")[:2000],
                   "hypothesis": str(f.get("hypothesis") or "")[:2000],
                   "paths": [str(p)[:300] for p in (f.get("paths") or [])[:20]],
                   "in_scope": f.get("in_scope") is not False,
                   "region": str(f.get("region") or "")[:200],
                   "verify": str(f.get("verify") or "")[:1000]})
    lst = lambda k, n=30: [str(x)[:500] for x in (d.get(k) or [])[:n] if x]   # noqa: E731
    # v0.121.7: the visual observer names the image and the source separately
    # (only present when the helper sent them; other lanes keep "base")
    ident = {k: str(d.get(k))[:80] for k in ("artifact_id", "source_snapshot_id") if d.get(k)}
    return {**ident, "structured": True, "assignment": str(d.get("assignment") or "")[:60],
            "base": str(d.get("base") or "")[:80], "summary": str(d.get("summary") or "")[:500],
            "findings": fs, "patch": str(d.get("patch") or "")[:60000],
            "affected_paths": lst("affected_paths"), "checks_run": lst("checks_run"),
            "checks_proposed": lst("checks_proposed"), "skills_used": lst("skills_used"),
            "sources": lst("sources"), "limitations": str(d.get("limitations") or "")[:2000],
            "next_action": str(d.get("next_action") or "")[:1000],
            "image_seen": d.get("image_seen"), "subject_present": d.get("subject_present"),
            "validity": str(d.get("validity") or "")[:40] if d.get("validity") else ""}


_DIFF_FILE = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.M)
_DIFF_OLD = re.compile(r"^--- (?:a/)?(\S+)", re.M)


def patch_paths(patch: str) -> list[str]:
    paths = [p for p in _DIFF_FILE.findall(patch or "") if p != "/dev/null"]
    paths += [p for p in _DIFF_OLD.findall(patch or "") if p != "/dev/null"]
    return sorted(dict.fromkeys(p.replace("\\", "/") for p in paths))


def validate_patch(patch: str, scope: list[str]) -> dict:
    """Deterministic checks on a proposed unified diff (never applied here)."""
    if not (patch or "").strip():
        return {"has_patch": False, "paths": [], "out_of_scope": [], "well_formed": True}
    paths = patch_paths(patch)
    hunks = len(re.findall(r"^@@ ", patch, re.M))
    bad = [p for p in paths if p.startswith(("/", "../")) or ".." in p.split("/")
           or not matches_any(p, scope)]
    return {"has_patch": True, "paths": paths, "out_of_scope": bad,
            "well_formed": bool(paths) and hunks > 0, "hunks": hunks}


def now() -> float:
    return time.time()
