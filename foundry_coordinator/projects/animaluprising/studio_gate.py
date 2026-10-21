"""AnimalUprising studio gate runner (UI-T0; plan AU_UI_REWORK_PLAN.md section 6).

One command per sprint replaces the printed CALL plan a model used to execute by
hand: host checks (ported from tools/gate.ps1), Edit-mode story captures at the
five AU_* device sizes with UiLayoutCheck lines, wiring/focus/touch checks the
sprint plan declares, the affected regression (GateRunner.run's CALL plan executed
by this script, bridge + DYNARG + tightBefore handled), GateRunner.tally, a console
error check, then one verdict JSON and one evidence summary block.

Usage (any cwd; --repo is the AU checkout):
  py -m foundry_coordinator.projects.animaluprising.studio_gate --repo C:\\Roblox\\AnimalUprising <cmd>
    doctor                                   preflight; exit 0 ready / 2 not ready (prints fixes)
    stories  --sprint ID [--story NAME ...]  Edit-mode stories only (fast loop)
    sprint   --sprint ID --files a,b [--selection SEL] [--tier B] [--background]
    regression --selection ui|full|S45,S46 [--sprint ID] [--background]
    status   --sprint ID [--wait MINUTES]    read status.json / latest.json (--wait blocks <= 9 min)
Exit codes: 0 PASS, 1 FAIL, 2 not ready / tooling error / still running. The last
printed line is `GATE <ID> PASS|FAIL` (status while running: `GATE <ID> RUNNING ...`).

Outputs (AU repo, git-ignored under tmp/): .plan/tmp/gate/<ID>/<head12>.<mode>.json (+
<head12>.<mode>.raw.log with every payload and raw output), latest.json, latest/<mode>.json,
status.json; .plan/tmp/gate/runner.lock while a runner drives Studio (a second one exits 2);
captures under docs/evidence/UI-v3/<ID>/; sprint mode appends ONE block to
docs/progress/<ID>_EVIDENCE_RAW.log in the gate.ps1 -Finish shape (GATE RUN, PASS: GATE HEAD,
=== GATE FINISH ===, VERDICT lines, 'GATE <ID> PASS|FAIL'). --run-id (or env
FOUNDRY_GATE_RUN_ID) is written into the verdict so a caller can tell its own run.

Regression: every suite runs in its OWN fresh solo playtest (boot CALL 1-2 re-run and
required PASS in each), bridge suites always alone; RegressionIndex entry fields read here:
isolation = "shared" (opt out), setup = {"server.<expr>", ...} (after the boot gate),
waits = {[stepIdx] = seconds}, rerunAfter = {"S48"}, runLast = true (S46 always last); a
suite whose run hit only tool errors is retried once in a fresh playtest. Results stay keyed
by the original CALL numbers for GateRunner.tally.

Sprint plan file docs/progress/<ID>_gate.json, read from HEAD (all keys optional):
  {"schema": 1,
   "stories":  [{"name": "Hud.normal", "module": "ReplicatedStorage.TestLib.SampleStory",
                 "fixture": {...}, "sizes": ["1920x1080", ...]}],
   "wiring":   [{"id": "hud.snapshot", "vm": "server|client|edit", "code": "return ...",
                 "expect": "PASS: W1", "tightBefore": false}],
   "touch":    [same shape as wiring],
   "focus":    ["PanelName" | {"id": "x", "story": "Hud.normal"} | {"id": "x", "panel": "HudV3"}],
   "files":    ["src/client/UI/v3/Hud.lua"],
   "selection": "ui"}
Fail closed everywhere: UNKNOWN (tool error, timeout, unparsable output) never counts as PASS.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Callable, Optional

from ... import mcp_stdio as M

MODULE = "foundry_coordinator.projects.animaluprising.studio_gate"
DEVICES = [("AU_1920x1080", 1920, 1080), ("AU_1366x768", 1366, 768), ("AU_1024x768", 1024, 768),
           ("AU_844x390", 844, 390), ("AU_390x844", 390, 844)]
SIZE_NAMES = [f"{w}x{h}" for _, w, h in DEVICES]
UI_SHARED_FILES = {"src/shared/Config/Strings.lua", "src/shared/Config/Ui.lua"}
REMOTES_FILE = "src/shared/Net/Remotes.lua"
NO_REBOOT_BRIDGE = {"S11_5_boot_check"}      # the boot gate itself: never manufacture a boot (§21)
DEFAULT_ROJO_PORT = 34872
WAIT_MAX_MINUTES = 9.0
WAIT_DEFAULT_MINUTES = 8.0
RESULTS = ("PASS", "FAIL", "SKIP", "UNKNOWN")
SETTLE_S = 1.5                   # after a playtest has a player (triage 2026-10-20, item 7)
TIGHT_GAP_MS = 500               # a tightBefore pair further apart than this is UNKNOWN
RUN_LAST = {"S46"}               # no teardown: quiesces the match (triage: run it last)
LOCK_NAME = "runner.lock"        # .plan/tmp/gate/runner.lock: one runner drives Studio
SAFE_PATH_RE = re.compile(r"[A-Za-z0-9_./\-]+")
STORY_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
CREATE_NO_WINDOW = M.CREATE_NO_WINDOW
_NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}

CALL_RE = re.compile(r"^CALL (\d+) \[vm=(\w+)\] (.*?) :: (.*)$")
NOTE_TIGHT_RE = re.compile(r"^NOTE (\S+): CALL (\d+) must be issued back-to-back with CALL (\d+)")
NOTE_DYN_RE = re.compile(r"^NOTE (\S+): CALL (\d+) is DYNARG")
TOKEN_RE = re.compile(r"<([A-Za-z0-9_]+)>")
SUITE_RE = re.compile(r"^SUITE (\S+) pass=(\d+) fail=(\d+) expect=(\S+) designedFails=(\d+) VERDICT (PASS|FAIL|SKIP)")
VERIFY_RE = re.compile(r"VERIFY matched=(\d+)/(\d+) mismatched=(\d+) unresolved=(\d+)")
HASH_RE = re.compile(r"^HASH_FNV (\d+) files at [0-9a-f]+ PASS$")
CONSOLE_ERR_RE = re.compile(
    r"(Stack Begin|^\s*\[?(?:error|ERROR)\]?[:\s]|^\s*\S+:\d+: |attempt to (?:index|call|perform|compare|concatenate)"
    r"|is not a valid member of|Requested module experienced an error)")


# ---- small pure helpers ----------------------------------------------------------
def luau_long(s: str) -> str:
    """A Luau long-bracket literal holding s exactly (leading newline protected)."""
    s = str(s)
    n = 0
    while ("]" + "=" * n + "]") in s or s.endswith("]" + "=" * n):
        n += 1
    eq = "=" * n
    return f"[{eq}[\n{s}]{eq}]"


def luau_value(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v or v in (float("inf"), float("-inf")):
            raise ValueError("no Luau literal for a non-finite number")     # nan/inf -> nil
        return repr(v)
    if isinstance(v, str):
        return luau_long(v)
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(luau_value(x) for x in v) + "}"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            key = f"[{k}]" if isinstance(k, int) else f"[ {luau_long(str(k))} ]"   # "[ [[" never lexes as "[[["
            parts.append(f"{key} = {luau_value(x)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"no Luau literal for {type(v).__name__}")


def png_size(data: bytes) -> Optional[tuple]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def extract_json(text: str):
    """The JSON object a Luau snippet returned (tolerates prints/prefixes around it)."""
    text = text or ""
    for cand in [text.strip()] + [ln.strip() for ln in reversed(text.splitlines())]:
        if cand.startswith("{") or cand.startswith("["):
            try:
                return json.loads(cand)
            except ValueError:
                pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    return None


def safe_id(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", s or ""):
        raise SystemExit(f"bad sprint id {s!r} (letters, digits, . _ - only)")
    return s


def check(cid: str, result: str, detail: str = "", *, required: bool = True, artifacts=None) -> dict:
    assert result in RESULTS, result
    return {"id": cid, "result": result, "required": bool(required), "detail": str(detail)[:500],
            "artifacts": list(artifacts or [])}


def overall(checks: list, error: Optional[str] = None) -> str:
    """PASS only if every required check is PASS (UNKNOWN/SKIP on a required check fail)."""
    if error:
        return "ERROR"
    req = [c for c in checks if c.get("required")]
    if not req:
        return "FAIL"
    return "PASS" if all(c.get("result") == "PASS" for c in req) else "FAIL"


def lines_result(text: str, expect: str, tool_error: bool) -> tuple:
    """(result, detail) for a snippet check: expected PASS-line prefix present and no FAIL line."""
    if tool_error:
        return "UNKNOWN", "tool error: " + (text or "")[:200]
    lines = [ln.strip() for ln in (text or "").splitlines()]
    fails = [ln for ln in lines if ln.startswith("FAIL:")]
    if fails:
        return "FAIL", fails[0][:200]
    if expect and any(ln.startswith(expect) for ln in lines):
        return "PASS", next(ln for ln in lines if ln.startswith(expect))[:200]
    return "FAIL", f"expected line starting {expect!r} missing"


# ---- CALL plan -------------------------------------------------------------------
def parse_plan(text: str) -> dict:
    """GateRunner.run output -> {calls:[{n, vm, kind, tool, payload, tight, dynarg, file}], error, notes}.
    kind: server|client|edit (executable), bridge, skip (offstudio), manual (not executable)."""
    calls, notes, error = {}, [], None
    tight, dyn = set(), set()
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if ln.startswith("FAIL: GATE regression"):
            error = ln
        m = NOTE_TIGHT_RE.match(ln)
        if m:
            tight.add(int(m.group(2)))
        m2 = NOTE_DYN_RE.match(ln)
        if m2:
            dyn.add(int(m2.group(2)))
        if ln.startswith("NOTE"):
            notes.append(ln)
            continue
        m = CALL_RE.match(ln)
        if not m:
            continue
        n, vm, tool, payload = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        c = {"n": n, "vm": vm, "tool": tool, "payload": payload, "file": None, "line": ln}
        if tool.startswith("SKIP"):
            c["kind"] = "skip"
        elif tool.startswith("MANUAL (bridge)"):
            c["kind"] = "bridge"
            fm = re.search(r"\btests/([\w.\-/]+?\.luau?)\b", payload)
            c["file"] = fm.group(1) if fm else None
        elif tool.startswith("MANUAL"):
            c["kind"] = "manual"
        elif vm in ("server", "client", "edit"):
            c["kind"] = vm
        else:
            c["kind"] = "manual"
        calls[n] = c
    out = []
    for n in sorted(calls):
        c = calls[n]
        c["tight"] = n in tight
        c["dynarg"] = n in dyn
        c["tokens"] = TOKEN_RE.findall(c["payload"]) if c["dynarg"] else []
        out.append(c)
    if error is None and not out:
        error = "no CALL lines in GateRunner.run output"
    return {"calls": out, "notes": notes, "error": error}


def flatten_value(v) -> str:
    """A returned value -> raw lines. Suites return a table of lines, which arrives as a
    JSON array (community `result`/`returnValue`, often JSON-ENCODED inside a string) or,
    from the official execute_luau, as an object keyed "1","2",...; GateRunner.tally only
    counts column-0 `PASS: ` lines, so join them one per line (live run 2026-10-20: S45,
    S46 and AS_C16 tallied 0-5 of 12-23 real PASS lines without this)."""
    if v is None:
        return ""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("[", "{"):
            try:
                d = json.loads(s)
            except ValueError:
                return v
            if isinstance(d, list) or (isinstance(d, dict) and d and all(
                    re.fullmatch(r"\d+", str(k)) for k in d)):
                return flatten_value(d)
        return v
    if isinstance(v, list):
        return "\n".join(x if isinstance(x, str) else json.dumps(x) for x in v)
    if isinstance(v, dict):
        if v and all(re.fullmatch(r"\d+", str(k)) for k in v):
            return "\n".join(x if isinstance(x, str) else json.dumps(x)
                             for _, x in sorted(v.items(), key=lambda kv: int(kv[0])))
        return json.dumps(v)
    return json.dumps(v)


def one_line(text, cap: int = 200) -> str:
    """A tool error's text as ONE line (so its printed PASS: lines can never be counted)."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:cap]


def count_lines(text: str) -> tuple:
    """GateRunner.tally's countLines: (pass, fail, captured) over column-0 lines."""
    p = f = c = 0
    for line in (str(text or "") + "\n").split("\n")[:-1]:
        line = line.rstrip("\r")
        if line.startswith("PASS: "):
            p += 1
        elif line.startswith("FAIL: "):
            f += 1
        if line[:1] and not line[:1].isspace():
            c += 1
    return p, f, c


def mirror_verdict(entry: dict, texts: list, offstudio: bool = False) -> tuple:
    """GateRunner.tally's per-suite verdict for one run of a suite -> (verdict, detail)."""
    if offstudio:
        return "SKIP", "offstudio"
    p = f = c = 0
    for t in texts:
        a, b, d = count_lines(t)
        p, f, c = p + a, f + b, c + d
    want, dfail = entry.get("expectPass"), int(entry.get("designedFails") or 0)
    if c == 0:
        return "FAIL", "no captured lines"
    if want is None:
        return "FAIL", f"expect UNKNOWN (pass={p} fail={f})"
    if p == want and f == dfail:
        return "PASS", f"pass={p} fail={f}"
    return "FAIL", f"pass={p} (want {want}), fail={f} (want {dfail})"


def _dyn_value(tok: str, raw: str) -> Optional[str]:
    """The last `tok=value` / `tok: value` in raw. *Json tokens take a balanced JSON
    value up to the end of the line; scalars must be [\\w.-]+ (no trailing , or ))."""
    val = None
    for m in re.finditer(re.escape(tok) + r"\s*[=:]\s*", raw or ""):
        rest = raw[m.end():].split("\n", 1)[0]
        if rest[:1] in ("{", "["):
            try:
                _, end = json.JSONDecoder().raw_decode(rest)
                val = rest[:end]
                continue
            except ValueError:
                pass
        sm = re.match(r"[\w.\-]+", rest)
        if sm and not tok.lower().endswith("json"):
            val = sm.group(0)
    return val


def resolve_dynarg(payload: str, tokens: list, earlier: list) -> tuple:
    """Substitute each <token> from the most recent earlier raw output (the caller
    passes only the same suite's successful earlier CALLs). A JSON value - or any
    value sitting in a quoted placeholder '<tok>' / "<tok>" - is inserted as a Luau
    long string, so quotes and \\u escapes can't break the code. Returns
    (payload, missing_tokens)."""
    missing = []
    for tok in dict.fromkeys(tokens):
        val = None
        for raw in reversed(earlier):
            val = _dyn_value(tok, raw)
            if val is not None:
                break
        if val is None:
            missing.append(tok)
            continue
        lit = luau_long(val)
        for q in ("'", '"'):
            payload = payload.replace(f"{q}<{tok}>{q}", lit)
        payload = payload.replace(f"<{tok}>", lit if val[:1] in ("{", "[") else val)
    return payload, missing


def parse_tally(text: str) -> dict:
    suites = {}
    for ln in (text or "").splitlines():
        m = SUITE_RE.match(ln.strip())
        if m:
            suites[m.group(1)] = {"verdict": m.group(6), "line": ln.strip()}
    lines = [ln.strip() for ln in (text or "").splitlines()]
    verdict = "UNKNOWN"
    if any(ln.startswith("PASS: GATE regression") for ln in lines):
        verdict = "PASS"
    if any(ln.startswith("FAIL: GATE regression") for ln in lines):
        verdict = "FAIL"
    return {"suites": suites, "verdict": verdict}


# ---- RegressionIndex (pure-data Luau) -> Python -----------------------------------
class _LuauData:
    """Tiny parser for the pure-data Luau table literals RegressionIndex.lua holds."""

    def __init__(self, s: str, pos: int = 0):
        self.s, self.i = s, pos

    def ws(self):
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("--", self.i):
                m = re.match(r"--\[(=*)\[", s[self.i:])
                if m:
                    end = s.find("]" + m.group(1) + "]", self.i)
                    self.i = len(s) if end < 0 else end + len(m.group(1)) + 2
                else:
                    nl = s.find("\n", self.i)
                    self.i = len(s) if nl < 0 else nl + 1
            else:
                break

    def value(self):
        self.ws()
        s, i = self.s, self.i
        c = s[i]
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string()
        m = re.match(r"\[(=*)\[", s[i:])
        if m:
            close = "]" + m.group(1) + "]"
            end = s.find(close, i)
            body = s[i + len(m.group(0)):end]
            self.i = end + len(close)
            return body[1:] if body.startswith("\n") else body
        m = re.match(r"-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", s[i:])
        if m:
            self.i += len(m.group(0))
            t = m.group(0)
            return int(t, 16) if "x" in t.lower() else (float(t) if any(x in t for x in ".eE") else int(t))
        m = re.match(r"[A-Za-z_]\w*", s[i:])
        if m:
            self.i += len(m.group(0))
            return {"true": True, "false": False, "nil": None}.get(m.group(0), m.group(0))
        raise ValueError(f"unexpected {s[i:i + 20]!r} at {i}")

    def string(self):
        s, q = self.s, self.s[self.i]
        self.i += 1
        out = []
        while s[self.i] != q:
            ch = s[self.i]
            if ch == "\\":
                nxt = s[self.i + 1]
                out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}.get(nxt, nxt))
                self.i += 2
            else:
                out.append(ch)
                self.i += 1
        self.i += 1
        return "".join(out)

    def table(self):
        self.i += 1
        arr, obj = [], {}
        while True:
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1
                break
            m = re.match(r"([A-Za-z_]\w*)\s*=(?!=)", self.s[self.i:])
            if m:
                self.i += len(m.group(0))
                obj[m.group(1)] = self.value()
            elif self.s[self.i] == "[" and not re.match(r"\[=*\[", self.s[self.i:]):
                self.i += 1
                k = self.value()
                self.ws()
                self.i += 1                                    # ]
                self.ws()
                self.i += 1                                    # =
                obj[k] = self.value()
            else:
                arr.append(self.value())
            self.ws()
            if self.s[self.i] in ",;":
                self.i += 1
        return obj if obj else arr


def parse_regression_index(text: str) -> dict:
    out = {"entries": [], "subsets": {}}
    for name in ("entries", "subsets"):
        m = re.search(r"local\s+" + name + r"\s*=\s*", text)
        if m:
            v = _LuauData(text, m.end()).value()
            out[name] = v if v else ([] if name == "entries" else {})
    return out


def _git(repo: str, *args, timeout: float = 60.0) -> tuple:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, **_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 99, str(e)


def comment_only_change(repo: str, path: str, base: str = "HEAD~1") -> bool:
    rc, out = _git(repo, "diff", "-U0", base, "HEAD", "--", path)
    if rc != 0:
        return False
    for ln in out.splitlines():
        if ln.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if ln.startswith(("+", "-")):
            body = ln[1:].strip()
            if body and not body.startswith("--"):
                return False
    return True


def affected_selection(files: list, index: dict, *, suite_text: Optional[Callable] = None,
                       comment_only: Optional[Callable] = None) -> str:
    """Changed files -> GateRunner selection ("ui", "S45,S46", or "" for none).
    A suite is hit when a changed file is its tests/<file>.lua, is listed in its optional
    `files`, is a mount leaf of the same name, or (src/ files) its module stem appears in the
    suite's source (suite_text(file) -> str). Config/Strings.lua, Config/Ui.lua, comment-only
    Remotes.lua edits and unmapped src/ files map to the `ui` subset."""
    entries = index.get("entries") or []
    ui = False
    hit = set()
    for f in files or []:
        f = f.replace("\\", "/").strip().removeprefix("./")
        if not f:
            continue
        if f in UI_SHARED_FILES or (f == REMOTES_FILE and comment_only and comment_only(f)):
            ui = True
            continue
        stem = os.path.splitext(os.path.basename(f))[0]
        if stem == "init":
            stem = os.path.basename(os.path.dirname(f))
        mine = []
        for e in entries:
            efile = e.get("file") or ""
            leafs = {str(p).split(".")[-1] for p in (e.get("mounts") or {}).values()}
            if f == f"tests/{efile}.lua" or f in (e.get("files") or []) or (
                    f.startswith("tests/") and stem in leafs):
                mine.append(e["id"])
            elif f.startswith("src/") and suite_text is not None and len(stem) >= 4:
                if re.search(r"\b" + re.escape(stem) + r"\b", suite_text(efile) or ""):
                    mine.append(e["id"])
        if not mine and f.startswith("src/"):
            ui = True            # unmapped src/ (UI or shared/server): never zero regression (M11)
        hit.update(mine)
    ui_members = list((index.get("subsets") or {}).get("ui") or [])
    if ui and hit <= set(ui_members):
        return "ui"
    ids = set(hit) | (set(ui_members) if ui else set())
    return ",".join(e["id"] for e in entries if e.get("id") in ids)


# ---- CALL -> suite mapping (mirror of GateRunner selectEntries/buildPlan/stepVm) ----
def select_entries(index: dict, selection: str) -> Optional[list]:
    entries = [e for e in index.get("entries") or [] if isinstance(e, dict)]
    if selection == "full":
        return entries
    sub = (index.get("subsets") or {}).get(selection)
    if sub is not None:
        members = set(sub)
        return [e for e in entries if e.get("id") in members]
    by = {e.get("id"): e for e in entries}
    out = []
    for part in re.findall(r"[^,\s]+", selection or ""):
        if part not in by:
            return None
        out.append(by[part])
    return out


def step_vm(step: str, entry: dict) -> str:
    if step != "main":
        m = re.match(r"^(\w+)\.", step)
        if m:
            return m.group(1)
    return str(entry.get("vm") or "")


def is_bridge_entry(e: dict) -> bool:
    return e.get("loader") == "bridge" or (
        "main" in (e.get("steps") or []) and e.get("vm") not in ("offstudio", "edit"))


def map_calls(plan: dict, index: dict, selection: str) -> Optional[dict]:
    """{call n: (entry, stepIdx)} when the CALL plan is exactly what buildPlan would emit
    for this index + selection (count, numbering and VMs); None otherwise (fail closed:
    the caller then cannot isolate suites and records an UNKNOWN)."""
    entries = select_entries(index, selection)
    if not entries:
        return None
    rows, n = {}, 2
    for e in entries:
        for si, step in enumerate(e.get("steps") or [], 1):
            n += 1
            rows[n] = (e, si, str(step))
    calls = {c["n"]: c for c in plan.get("calls") or []}
    if set(calls) != {1, 2} | set(rows):
        return None
    for k, (e, si, step) in rows.items():
        vm = step_vm(step, e)
        want = "host" if vm == "offstudio" else vm
        if calls[k]["vm"] != want:
            return None
    return {k: (e, si) for k, (e, si, _) in rows.items()}


def mount_require(dm_path: str) -> str:
    """GateRunner.mountRequireExpr."""
    top, _, rest = (dm_path or "").partition(".")
    if top == "ServerScriptService" and rest:
        return f'require(game:GetService("ServerScriptService"):WaitForChild("{rest}"))'
    if top == "StarterPlayer" and "." in rest:
        mid, leaf = rest.split(".", 1)
        return f'require(game:GetService("StarterPlayer"):WaitForChild("{mid}"):WaitForChild("{leaf}"))'
    return f'error("unknown mount path {dm_path}")'


def step_payload(entry: dict, step: str) -> Optional[tuple]:
    """(vm, payload) for a `vm.expr` step string, built like GateRunner.emitCall; None
    when the VM is not runner-executable."""
    vm = step_vm(step, entry)
    expr = step[len(vm) + 1:] if step.startswith(vm + ".") else step
    mounts = entry.get("mounts") or {}
    if vm == "server":
        return vm, (f"local S = {mount_require(mounts.get('server') or '')}; local P1 = "
                    f'(game:GetService("Players"):GetPlayers()[1] or error("no player in world")); '
                    f"return ({expr})")
    if vm == "client":
        return vm, (f"local S = {mount_require(mounts.get('client') or '')}; local P1 = "
                    f'game:GetService("Players").LocalPlayer; return ({expr})')
    if vm == "edit":
        return vm, f"return ({expr})"
    return None


def entry_waits(entry: dict) -> dict:
    """waits = {[stepIdx] = seconds} -> {int: float} (a plain list counts from 1)."""
    w = entry.get("waits") or {}
    items = enumerate(w, 1) if isinstance(w, list) else w.items()
    out = {}
    for k, v in items:
        try:
            out[int(k)] = max(0.0, min(float(v), 120.0))
        except (TypeError, ValueError):
            pass
    return out


# ---- Rojo mounts (default.project.json) -> datamodel paths -------------------------
def _rojo_base(name: str) -> str:
    n = re.sub(r"\.luau?$", "", name)
    return re.sub(r"\.(server|client)$", "", n)


def rojo_mounts(project: dict) -> list:
    """[(repo $path, datamodel path list)] for every node of a Rojo project tree."""
    out = []

    def walk(node, dm):
        if not isinstance(node, dict):
            return
        p = node.get("$path")
        if isinstance(p, str) and dm:
            out.append((p.replace("\\", "/").rstrip("/"), list(dm)))
        for k, v in node.items():
            if not k.startswith("$"):
                walk(v, dm + [k])
    walk((project or {}).get("tree") or {}, [])
    return out


def rojo_resolve(mounts: list, repo_path: str) -> list:
    """Datamodel paths ("Service.A.B") a repo file is mounted at (all of them)."""
    f = repo_path.replace("\\", "/")
    out = []
    for src, dm in mounts:
        if f == src:
            out.append(".".join(dm))
        elif f.startswith(src + "/"):
            parts = f[len(src) + 1:].split("/")
            if parts[-1] in ("init.lua", "init.luau", "init.server.lua", "init.client.lua"):
                parts = parts[:-1]
            else:
                parts[-1] = _rojo_base(parts[-1])
            out.append(".".join(dm + parts))
    return out


# ---- Luau snippets ---------------------------------------------------------------
DEVICE_HELPER = r'''
local SDS = game:GetService("StudioDeviceSimulatorService")
local function idOf(d) if type(d) == "table" then return d.Id or d.id or d.DeviceId or d.Name or d.name end return d end
local function nameOf(id, d)
    if type(d) == "table" and (d.Name or d.name) then return d.Name or d.name end
    local ok, info = pcall(function() return SDS:GetDeviceInfoAsync(id) end)
    if ok and type(info) == "table" then return info.Name or info.name end
    return tostring(id)
end
local function findDevice(name)
    for _, d in ipairs(SDS:GetDeviceListAsync()) do
        local id = idOf(d)
        if nameOf(id, d) == name then return id end
    end
    return nil
end
'''

FRESH_LIB = r"""
-- The Edit VM caches require() per ModuleScript instance, so a Rojo edit to TestLib would
-- never be seen (KNOWN_ISSUES 42). Keep one clone of the whole TestLib folder (so
-- script.Parent lookups inside it still resolve) and rebuild it when any source changes.
local function __freshLib()
    local lib = game:GetService("ReplicatedStorage"):WaitForChild("TestLib")
    local parts = {}
    for _, m in ipairs(lib:GetChildren()) do
        if m:IsA("ModuleScript") then
            table.insert(parts, m.Name .. "\0" .. m.Source)
        end
    end
    table.sort(parts)
    local key = table.concat(parts, "\1")
    local e = _G.__AU_TESTLIB
    if e and e.key == key and e.folder and e.folder.Parent then
        return e
    end
    if e and e.folder then
        pcall(function() e.folder:Destroy() end)
    end
    local folder = lib:Clone()
    folder.Name = "__AU_TestLib_fresh"
    folder.Parent = game:GetService("ServerStorage")
    e = { key = key, folder = folder, mods = {} }
    _G.__AU_TESTLIB = e
    return e
end
local function __freshRequire(name)
    local e = __freshLib()
    if e.mods[name] == nil then
        e.mods[name] = require(e.folder:WaitForChild(name))
    end
    return e.mods[name]
end
"""

# GateRunner (and the RegressionIndex it requires) live in ServerScriptService and are
# cached by the Edit VM's require exactly like TestLib (live run 2026-10-20: Studio planned
# 61 CALLs from a stale index while HEAD had 62). Load a fresh GateRunner clone whose index
# lookup points at a fresh index clone; rebuilt whenever either source changes.
FRESH_GATE = r"""
local function __freshGate()
    local SSS = game:GetService("ServerScriptService")
    local gr, ri = SSS:WaitForChild("GateRunner"), SSS:WaitForChild("RegressionIndex")
    local key = gr.Source .. "\0" .. ri.Source
    local e = _G.__AU_GATERUNNER
    if e and e.key == key and e.folder and e.folder.Parent then
        return e.mod
    end
    if e and e.folder then
        pcall(function() e.folder:Destroy() end)
    end
    local folder = Instance.new("Folder")
    folder.Name = "__AU_GateRunner_fresh"
    folder.Parent = game:GetService("ServerStorage")
    local g = gr:Clone()
    local src, n = g.Source:gsub('ServerScriptService:FindFirstChild%("RegressionIndex"%)', 'script:FindFirstChild("RegressionIndex")')
    if n < 1 then
        error("GateRunner: RegressionIndex lookup not found for the fresh clone")
    end
    g.Source = src
    g.Parent = folder
    ri:Clone().Parent = g
    local mod = require(g)
    _G.__AU_GATERUNNER = { key = key, folder = folder, mod = mod }
    return mod
end
"""

SWITCH_AND_MOUNT = DEVICE_HELPER + FRESH_LIB + r'''
local HS = game:GetService("HttpService")
local id = findDevice(__NAME__)
if id == nil then id = SDS:CreateDeviceAsync({Name = __NAME__, Width = __W__, Height = __H__, PixelDensity = 96}) end
SDS:SetDeviceAsync(id); task.wait(0.35)
SDS:SetScalingModeAsync(Enum.DeviceSimulatorScalingMode.ActualResolution); task.wait(0.35)
local H = __freshRequire("StoryHarness")
local ok, res = pcall(H.mountAndCheck, __MODULE__, __FIXTURE__, {size = __SIZE__, story = __STORY__})
if not ok then res = {ok = false, error = tostring(res)} end
return HS:JSONEncode(res)
'''

UNMOUNT = FRESH_LIB + r'''
local H = __freshRequire("StoryHarness")
H.unmount()
return "UNMOUNTED"
'''

STOP_SIM = r'''
local SDS = game:GetService("StudioDeviceSimulatorService")
local ok, err = pcall(function() SDS:StopSimulationAsync() end)
return ok and "SIM STOPPED" or ("SIM STOP FAILED " .. tostring(err))
'''

# Regression suites assume a 1920x1080 viewport (UI_01 c21a-c25a). With the simulator
# stopped, Studio uses its window size (2531x1128 on a big monitor, UI-02a 2026-10-20), so
# the regression lane starts on the AU_1920x1080 device at actual resolution.
SET_DESKTOP_DEVICE = DEVICE_HELPER + r'''
local id = findDevice("AU_1920x1080")
if id == nil then id = SDS:CreateDeviceAsync({Name = "AU_1920x1080", Width = 1920, Height = 1080, PixelDensity = 96}) end
SDS:SetDeviceAsync(id); task.wait(0.35)
SDS:SetScalingModeAsync(Enum.DeviceSimulatorScalingMode.ActualResolution); task.wait(0.35)
return "DEVICE AU_1920x1080"
'''

FOCUS_STORY = FRESH_LIB + r'''
local H = __freshRequire("StoryHarness")
local F = __freshRequire("FocusWalk")
local ok, res = pcall(H.mount, __MODULE__, __FIXTURE__)
if not ok then return "FAIL: FOCUS " .. __ID__ .. " mount error " .. tostring(res) end
local ok2, out = pcall(F.walk, res.root, {name = __ID__})
H.unmount()
return ok2 and out or ("FAIL: FOCUS " .. __ID__ .. " walk error " .. tostring(out))
'''

FOCUS_PANEL = r'''
-- runs in the play VM (fresh require cache per playtest; no ServerStorage on a client)
local F = require(game:GetService("ReplicatedStorage"):WaitForChild("TestLib"):WaitForChild("FocusWalk"))
return F.run(__PANEL__, {name = __ID__})
'''

DOCTOR = DEVICE_HELPER + r'''
local HS = game:GetService("HttpService")
local RS = game:GetService("ReplicatedStorage")
local SSS = game:GetService("ServerScriptService")
local out = {placeName = game.Name, devices = {}, created = {}}
out.showDev = game:GetService("StarterGui").ShowDevelopmentGui
out.http = HS.HttpEnabled
local tl = RS:FindFirstChild("TestLib")
out.testLib = tl ~= nil
out.harness = tl ~= nil and tl:FindFirstChild("StoryHarness") ~= nil
out.focusWalk = tl ~= nil and tl:FindFirstChild("FocusWalk") ~= nil
out.layoutCheck = tl ~= nil and tl:FindFirstChild("UiLayoutCheck") ~= nil
out.gateRunner = SSS:FindFirstChild("GateRunner") ~= nil
out.regressionIndex = SSS:FindFirstChild("RegressionIndex") ~= nil
local okList, errList = pcall(function()
    for _, d in ipairs(__DEVICES__) do
        local id = findDevice(d[1])
        if id == nil then
            local ok, newId = pcall(function() return SDS:CreateDeviceAsync({Name = d[1], Width = d[2], Height = d[3], PixelDensity = 96}) end)
            if ok and newId ~= nil then table.insert(out.created, d[1]); id = newId end
        end
        out.devices[d[1]] = id ~= nil
    end
end)
if not okList then out.deviceError = tostring(errList) end
return HS:JSONEncode(out)
'''

LIVE_HASH = r'''
-- GateRunner.verifyLive's fnv1a32 for files its MOUNTED_TESTS table does not know
-- (e.g. tests/lib/* -> ReplicatedStorage.TestLib.*), resolved from default.project.json
local HS = game:GetService("HttpService")
local function fnv1a32(s)
    local h = 2166136261
    for i = 1, #s do
        h = bit32.bxor(h, s:byte(i))
        local lo = h % 65536
        local hi = (h - lo) / 65536
        h = (((hi * 16777619) % 65536) * 65536 + (lo * 16777619) % 4294967296) % 4294967296
    end
    return h
end
local out = {}
for _, p in ipairs(__PATHS__) do
    local names = string.split(p, ".")
    local ok, inst = pcall(function() return game:GetService(names[1]) end)
    if not ok then inst = nil end
    for i = 2, #names do
        if inst == nil then break end
        inst = inst:FindFirstChild(names[i])
    end
    if inst ~= nil and inst:IsA("LuaSourceContainer") then
        out[p] = string.format("%08x", fnv1a32(inst.Source))
    else
        out[p] = "missing"
    end
end
return HS:JSONEncode(out)
'''

BRIDGE = r'''
local HttpService = game:GetService("HttpService")
local file = __FILE__
local src = HttpService:GetAsync("http://127.0.0.1:__PORT__/tests/" .. file)
if __REBOOT__ then
    local okSL, SL = pcall(function() return require(game:GetService("ServerScriptService"):WaitForChild("Server"):WaitForChild("ServiceLoader")) end)
    if okSL and type(SL) == "table" and SL.HeartbeatConn == nil and SL.Boot then SL:Boot() end
end
local mod = Instance.new("ModuleScript")
mod.Name = "__GateBridge_" .. file:gsub("%W", "_")
mod.Source = src
mod.Parent = game:GetService("ServerStorage")
local ok, out = pcall(require, mod)
mod:Destroy()
if not ok then return "FAIL: GATE bridge " .. file .. " — " .. tostring(out) end
if type(out) == "table" then out = table.concat(out, "\n") end
return tostring(out)
'''


def fill(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k.upper()}__", v)
    return tpl


# ---- repo bridge (http.server on a free 127.0.0.1 port) ---------------------------
BRIDGE_FILE_RE = re.compile(r"[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.luau?")


class RepoBridge:
    """Serves ONLY GET /tests/<file>.lua(u), with the content committed at `rev`
    (git show), never the working tree and never anything else in the repo (M8)."""

    def __init__(self, root: str, rev: Optional[Callable] = None):
        self.root = root
        self.rev = rev or (lambda: "HEAD")
        self.httpd = None

    def content(self, path: str) -> Optional[bytes]:
        m = re.fullmatch(r"/tests/(.+)", path.split("?", 1)[0])
        if not m or not BRIDGE_FILE_RE.fullmatch(m.group(1)):
            return None
        try:
            p = subprocess.run(["git", "-C", self.root, "show", f"{self.rev()}:tests/{m.group(1)}"],
                               capture_output=True, timeout=30, **_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    def start(self) -> int:
        if self.httpd is None:
            bridge = self

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    body = bridge.content(self.path)
                    if body is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
            self.httpd.daemon_threads = True
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1]

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


# ---- the runner ------------------------------------------------------------------
def default_run_cmd(argv: list, cwd: str, timeout: float) -> tuple:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, **_NO_WINDOW)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).rstrip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    except OSError as e:
        return 127, f"cannot run {argv[0]}: {e}"


ENVELOPE_KEYS = {"result", "returnValue", "output", "error"}


def unwrap_community(r: dict) -> dict:
    """robloxstudio-mcp wraps every eval in a JSON envelope. eval_*_runtime:
    {"ok":..,"bridge":"ok","result":"<value>","output":[..],"error":..}; execute_luau
    (the bridge path): {"success":..,"returnValue":"<value>","output":[..],"message":..,
    "error":..}. A table value arrives JSON-encoded inside the string. Hand back the
    printed output plus the flattened value as plain text (live runs 2026-10-20: without
    this every suite tallied pass=0, AS_C16's 12 array lines tallied 0, and the S28/S39
    bridges' `returnValue` came back empty). Fail closed: any `error`, ok:false,
    success:false or an error-typed output entry marks the result as an error."""
    try:
        d = json.loads(r.get("text") or "")
    except (ValueError, TypeError):
        return r
    if not isinstance(d, dict) or not (ENVELOPE_KEYS & set(d)):
        return r
    parts, err = [], False
    for o in d.get("output") or []:
        if isinstance(o, dict):
            kind = str(o.get("messageType") or o.get("level") or o.get("type") or "").lower()
            if "error" in kind or kind == "err":
                err = True
            parts.append(str(o.get("message", json.dumps(o))))
        else:
            parts.append(str(o))
    for key in ("result", "returnValue"):
        if d.get(key) is not None:
            parts.append(flatten_value(d[key]))
    if d.get("error"):
        parts.append("ERROR: " + str(d["error"]))
    out = dict(r)
    out["text"] = "\n".join(p for p in parts if p != "")
    out["is_error"] = (bool(r.get("is_error")) or err or bool(d.get("error"))
                       or d.get("ok") is False or d.get("success") is False)
    return out


BOOT_RE = re.compile(r"GateRunner\"\)+\.boot\(\)")


def boot_ok(text: str, err: bool) -> tuple:
    """(ok, reason) for a boot CALL 1/2: no tool error, a `PASS: BOOT` line, no FAIL line."""
    if err:
        return False, "tool error/timeout: " + one_line(text)
    lines = [ln.strip() for ln in (text or "").splitlines()]
    bad = next((ln for ln in lines if ln.startswith("FAIL")), None)
    if bad:
        return False, bad[:200]
    if not any(ln.startswith("PASS: BOOT") for ln in lines):
        return False, "no 'PASS: BOOT' line: " + one_line(text)
    return True, next(ln for ln in lines if ln.startswith("PASS: BOOT"))[:200]


def console_delta(before: Optional[list], after: Optional[list]) -> list:
    """Console lines that appeared between two get_console_output reads."""
    if before is None or after is None:
        return []
    if after[:len(before)] == before:
        return after[len(before):]
    left = collections.Counter(before)
    out = []
    for ln in after:
        if left[ln] > 0:
            left[ln] -= 1
        else:
            out.append(ln)
    return out


class GateInterrupted(Exception):
    """SIGTERM / CTRL_BREAK: unwind through the finally blocks (stop the playtest,
    close the MCP children) and write an ERROR verdict."""


class Gate:
    def __init__(self, repo: str, sprint: str, mode: str, *, place: Optional[str] = None,
                 official=None, community=None, run_cmd: Callable = default_run_cmd,
                 call_timeout: float = 120.0, out=print, run_id: str = ""):
        self.repo = os.path.abspath(repo)
        self.sprint, self.mode = sprint, mode
        self.run_id = run_id or ""
        # No --place: the single open Studio is used (the workstation's place file is
        # named after a restore, e.g. place_master_restore3.rbxl, not the project);
        # with several Studios open, --place picks one on BOTH servers (official
        # studio_id, community instance_id) and anything ambiguous is refused.
        self.place = place
        self._official, self._community = official, community
        self._owned = []
        self._conn_err: dict = {}
        self.run_cmd = run_cmd
        self.call_timeout = call_timeout
        self.out = out
        self.checks, self.captures = [], []
        self.regression = None
        self.started = time.time()
        self.head = None
        self.index = None
        self.gate_dir = os.path.join(self.repo, ".plan", "tmp", "gate", sprint)
        os.makedirs(self.gate_dir, exist_ok=True)
        self.raw_path = None
        self.verdict_path = None
        self._raw = []
        self.bridge = RepoBridge(self.repo, rev=lambda: self.head or "HEAD")
        self.console_base = None
        self._playing = False
        self._last_end = None
        self._all_results: dict = {}

    # ---- clients ----------------------------------------------------------------
    def _connect(self, kind: str) -> M.McpClient:
        """A failed connect is cached for the run: every later call fails at once
        instead of spawning a new server and waiting init_timeout again (M2)."""
        if kind in self._conn_err:
            raise M.McpError(f"{kind}: connect failed earlier in this run: {self._conn_err[kind]}")
        try:
            c = M.connect(kind)
        except M.McpError as e:
            self._conn_err[kind] = str(e)[:300]
            raise
        self._owned.append(c)
        return c

    @property
    def official(self) -> M.OfficialStudio:
        if self._official is None:
            self._official = M.OfficialStudio(self._connect("official"), place=self.place)
        if isinstance(self._official, M.McpClient):
            self._official = M.OfficialStudio(self._official, place=self.place)
        return self._official

    @property
    def community(self):
        if self._community is None:
            self._community = M.CommunityStudio(self._connect("community"), place=self.place)
        if isinstance(self._community, M.McpClient):
            self._community = M.CommunityStudio(self._community, place=self.place)
        return self._community

    def preflight(self):
        """Both servers up and bound to ONE Studio before any Studio step; raises
        McpError (-> ERROR verdict, exit 2) otherwise (M2, M10)."""
        self.official.resolve()
        com = self.community
        if isinstance(com, M.CommunityStudio):
            com.resolve()
            self.raw("preflight", "", f"official={self.official.studio_name} "
                                      f"community={com.place_name or '?'} instance={com.instance_id or '-'}")

    def close(self):
        self.bridge.stop()
        for c in self._owned:
            with contextlib.suppress(Exception):
                c.close()

    # ---- raw log / status -------------------------------------------------------
    def raw(self, label: str, payload: str, text: str):
        self._raw.append(f"--- {time.strftime('%H:%M:%S')} {label}\n>>> {payload}\n<<< {text}\n")

    def status(self, phase: str, progress: str = ""):
        write_json(os.path.join(self.gate_dir, "status.json"),
                   {"sprint": self.sprint, "pid": os.getpid(), "started": self.started,
                    "phase": phase, "progress": progress, "updated": time.time(), "mode": self.mode,
                    "run_id": self.run_id, "playing": self._playing})

    def add(self, c: dict):
        self.checks.append(c)
        return c

    # ---- tool calls (never raise; tool errors come back as (text, True)) ---------
    def edit(self, code: str, label: str = "edit", timeout: Optional[float] = None) -> tuple:
        return self._call(label, code, lambda: self.official.execute_luau(
            code, "Edit", timeout=timeout or self.call_timeout))

    def vm(self, vm: str, code: str, label: str, timeout: Optional[float] = None) -> tuple:
        t = timeout or self.call_timeout
        if vm == "edit":
            return self.edit(code, label, t)
        tool = {"server": "eval_server_runtime", "client": "eval_client_runtime"}.get(vm)
        if tool is None:
            return f"UNKNOWN: no tool for vm={vm}", True
        return self._call(label, code, lambda: unwrap_community(
            self.community.call_tool(tool, {"code": code}, timeout=t)))

    def _call(self, label: str, payload: str, fn) -> tuple:
        try:
            r = fn()
            text, err = r["text"], r["is_error"]
        except M.McpError as e:
            text, err = f"UNKNOWN: {e}", True
        self.raw(label + (" [tool error]" if err else ""), payload, text)
        return text, err

    # ---- host checks (tools/gate.ps1 port) --------------------------------------
    def host_checks(self, files: list, tier: str = "B") -> dict:
        self.status("host", "0/7")
        rc, head = self.run_cmd(["git", "rev-parse", "HEAD"], self.repo, 30)
        head = head.strip()
        if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", head):
            self.head = head
            self.add(check("host.head", "PASS", head[:7]))
        else:
            self.add(check("host.head", "FAIL", "git rev-parse failed: " + head[:200]))
        if files:
            rc, st = self.run_cmd(["git", "status", "--porcelain", "--"] + files, self.repo, 60)
            dirty = [ln for ln in st.splitlines() if ln.strip()]
            self.add(check("host.clean_tree", "PASS" if rc == 0 and not dirty else "FAIL",
                           "files under test committed" if rc == 0 and not dirty else
                           "not committed (R-C): " + "; ".join(dirty)[:300]))
        else:
            self.add(check("host.clean_tree", "FAIL", "no files under test (--files / plan files[])"))
        _, a = self.run_cmd(["git", "diff", "--stat"], self.repo, 60)
        _, b = self.run_cmd(["git", "diff", "--stat", "--ignore-cr-at-eol"], self.repo, 60)
        self.add(check("host.crlf", "PASS" if a == b else "FAIL",
                       "" if a == b else "CRLF-only churn in the working tree"))
        expect = self._hash_maps(files)
        if tier == "C":
            _, d = self.run_cmd(["git", "diff", "--stat", "HEAD~1", "--", "src", "tests"], self.repo, 60)
            self.add(check("host.no_code", "PASS" if not d.strip() else "FAIL",
                           "" if not d.strip() else "src/tests changed since the previous commit (tier C)"))
        self.status("host", "5/7")
        lune = shutil.which("lune")
        if not lune:
            self.add(check("lune.specs", "FAIL", "lune not on PATH; run rokit install"))
            self.add(check("lune.negative_control", "FAIL", "lune not on PATH; run rokit install"))
        else:
            rc, o = self.run_cmd([lune, "run", "tests/lune/run.luau"], self.repo, 600)
            self.raw("host lune", "lune run tests/lune/run.luau", o)
            ok = any(re.match(r"^LUNE \d+/\d+ PASS", ln) for ln in o.splitlines())
            self.add(check("lune.specs", "PASS" if ok else "FAIL",
                           next((ln for ln in o.splitlines() if ln.startswith("LUNE")), o[-200:])))
            rc, o = self.run_cmd([lune, "run", "tests/lune/run.luau", "--", "--negative"], self.repo, 600)
            self.raw("host lune negative", "lune run tests/lune/run.luau -- --negative", o)
            ok = any(ln.startswith("NEGATIVE CONTROL PASS") for ln in o.splitlines())
            self.add(check("lune.negative_control", "PASS" if ok else "FAIL", "" if ok else o[-200:]))
        self.status("host", "7/7")
        return expect

    def _hash_maps(self, files: list) -> dict:
        ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        script = os.path.join(self.repo, "tools", "hash_fnv.ps1")
        jp = os.path.join(self.gate_dir, "gate_expect.json")
        jf = os.path.join(self.gate_dir, "gate_expect_full.json")
        for p in (jp, jf):                      # never read a stale map from an earlier run
            with contextlib.suppress(OSError):
                os.remove(p)
        # file names reach `pwsh -Command`: only plain repo paths (PowerShell also treats
        # U+2018-U+201B as quotes, so escaping alone is not enough; M7)
        unsafe = [f for f in files if not SAFE_PATH_RE.fullmatch(f) or ".." in f.split("/")]
        if unsafe:
            self.add(check("host.hash_expect", "FAIL", "unsafe file name(s) under test: "
                           + ", ".join(repr(f) for f in unsafe)[:300]))
            return {}

        def q(s):
            return "'" + s.replace("'", "''") + "'"

        def run(arg: str) -> tuple:
            cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f"& {q(script)} -Rev HEAD {arg}"]
            rc, o = self.run_cmd(cmd, self.repo, 300)
            self.raw("host hash_fnv", " ".join(cmd), "\n".join(ln for ln in o.splitlines()
                                                              if not ln.startswith("REV ")))
            return rc, o

        n1 = n2 = None
        # hash_fnv.ps1 writes Join-Path (Get-Location) $Json, so -Json must be relative to
        # the repo (the cwd here); an absolute path became "C:\repo\C:\..." (live run 2026-10-20)
        if files:
            _, o1 = run("-Paths " + ",".join(q(f) for f in files) + f" -Json {q(rel(self.repo, jp))}")
            n1 = next((m.group(1) for m in map(HASH_RE.match, o1.splitlines()) if m), None)
        _, o2 = run(f"-All -Json {q(rel(self.repo, jf))}")
        n2 = next((m.group(1) for m in map(HASH_RE.match, o2.splitlines()) if m), None)
        ok = n2 is not None and (n1 is not None or not files)
        self.add(check("host.hash_expect", "PASS" if ok else "FAIL",
                       f"files={n1} full={n2}" if ok else "hash_fnv.ps1 did not report PASS for both maps",
                       artifacts=[rel(self.repo, jp), rel(self.repo, jf)]))
        try:
            with open(jp, encoding="utf-8-sig") as f:
                return (json.load(f) or {}).get("files") or {}
        except (OSError, ValueError):
            return {}

    def live_verify(self, expect: dict):
        """Studio sources == HEAD for the files under test. GateRunner.verifyLive knows
        src/ and its MOUNTED_TESTS; files it reports `SKIP (unmounted)` are resolved here
        from default.project.json at HEAD (tests/lib/* -> ReplicatedStorage.TestLib.*;
        live run 2026-10-20 reported matched=0/0 for the three TestLib files). Never PASS
        with 0 matched; a Studio-loaded file mounted nowhere is UNKNOWN with its name."""
        if not expect:
            self.add(check("host.live_hash", "UNKNOWN", "no hash map for the files under test"))
            return
        code = (FRESH_GATE + 'return __freshGate()'
                f".verifyLive({luau_value(expect)})")
        text, err = self.edit(code, "verifyLive")
        m = VERIFY_RE.search(text or "")
        if err or not m:
            self.add(check("host.live_hash", "UNKNOWN", (text or "")[:200]))
            return
        matched, total, mism, unres = map(int, m.groups())
        skipped = [p for p in re.findall(r"^SKIP \(unmounted\) (\S+)", text or "", re.M) if p in expect]
        mounts = rojo_mounts(project_at(self.repo, self.head or "HEAD") or {})
        extra = {p: rojo_resolve(mounts, p) for p in skipped}
        unmounted = [p for p, dm in extra.items() if not dm]
        extra = {p: dm for p, dm in extra.items() if dm}
        detail = [m.group(0)]
        if extra:
            paths = sorted({d for dm in extra.values() for d in dm})
            t2, e2 = self.edit(fill(LIVE_HASH, paths=luau_value(paths)), "live hash (Rojo mounts)")
            got = None if e2 else extract_json(t2)
            if not isinstance(got, dict):
                self.add(check("host.live_hash", "UNKNOWN", "Rojo-mount hash read failed: " + one_line(t2)))
                return
            for p, dm in sorted(extra.items()):
                lives = [str(got.get(d) or "missing") for d in dm]
                want = str(expect.get(p))
                total += 1
                if all(x == want for x in lives):
                    matched += 1
                elif "missing" in lives:
                    unres += 1
                    detail.append(f"UNRESOLVED {p} live={'/'.join(lives)} head={want}")
                else:
                    mism += 1
                    detail.append(f"MISMATCH {p} live={'/'.join(lives)} head={want}")
        # tests/lune/** run headless in Lune and are never mounted in Studio (UI-02a, 2026-10-20)
        studio = [p for p in unmounted if p.startswith(("src/", "tests/"))
                  and not p.startswith("tests/lune/")]
        summary = f"matched={matched}/{total} mismatched={mism} unresolved={unres}"
        if mism or unres:
            self.add(check("host.live_hash", "FAIL", "; ".join([summary] + detail[1:])))
        elif studio:
            self.add(check("host.live_hash", "UNKNOWN", f"{summary}; not mounted anywhere in Studio "
                           f"(GateRunner or default.project.json): " + ", ".join(studio)))
        elif matched == 0:
            self.add(check("host.live_hash", "SKIP", f"{summary}; no Studio-loaded file under test",
                           required=False))
        else:
            self.add(check("host.live_hash", "PASS", summary))

    # ---- stories ----------------------------------------------------------------
    def run_mock(self, stories: list, only: Optional[list] = None):
        """lane "mock" (UI-D1): render HTML mock pages through the reference renderer instead
        of mounting Studio stories. Every (story, size) frame becomes a check and a capture,
        so the coordinator's vision approval sees them like Studio captures. The page for each
        story comes from docs/design/ui-overhaul/v3/references.json (committed by the sprint)."""
        from . import reference_render as RR
        picked = [s for s in stories if isinstance(s, dict) and s.get("name")
                  and (not only or s["name"] in only)]
        if not picked:
            self.add(check("story.none", "FAIL", "no mock stories selected (plan stories[] / --story)"))
            return
        out_dir = os.path.join(self.repo, "docs", "evidence", "UI-v3", self.sprint)
        viewport_noted: set = set()
        for i, st in enumerate(picked, 1):
            self.status("mock", f"{i - 1}/{len(picked)}")
            name = st["name"]
            sizes = [RR.parse_size(z) for z in (st.get("sizes") or [])] or None
            try:
                res = RR.render_references(self.repo, [name], sizes, out_dir=out_dir,
                                           out=lambda m: self._raw.append(m + "\n"))
            except Exception as e:                                    # noqa: BLE001
                self.add(check(f"mock.{name}", "FAIL", f"render failed: {type(e).__name__}: {e}"[:300]))
                continue
            for r in res:
                size = r.get("size") or ""
                if r.get("viewport_note") and size not in viewport_noted:
                    # diagnostic, never required: this shell's headless viewport is short of
                    # the frame, so a mock sized from clientWidth/Height leaves an empty band
                    viewport_noted.add(size)
                    self.add(check(f"renderer.viewport@{size}", "FAIL",
                                   "headless viewport is short of the frame in this shell: "
                                   + str(r["viewport_note"]), required=False))
                path = r.get("path") or os.path.join(out_dir, RR.ref_filename(name, *RR.parse_size(size)))
                ok = bool(r.get("ok"))
                self.add(check(f"story.{name}@{size}", "PASS" if ok else "FAIL",
                               str(r.get("detail") or "")[:300], artifacts=[rel(self.repo, path)]))
                if ok:
                    self.captures.append({"story": name, "size": size, "path": rel(self.repo, path),
                                          "geometry_defects": 0, "layout_lines": [], "source": "mock"})
        self.status("mock", f"{len(picked)}/{len(picked)}")

    def run_stories(self, stories: list, only: Optional[list] = None):
        stories = [s for s in stories or [] if not only or (isinstance(s, dict) and s.get("name") in only)]
        if not stories:
            self.add(check("story.none", "FAIL", "no stories selected (plan stories[] / --story)"))
            return
        ev_dir = os.path.join(self.repo, "docs", "evidence", "UI-v3", self.sprint)
        os.makedirs(ev_dir, exist_ok=True)
        jobs = []
        for i, s in enumerate(stories, 1):
            if not isinstance(s, dict):
                self.add(check(f"story.{i}", "FAIL", "story entry is not an object"))
                continue
            name = s.get("name")
            if not isinstance(name, str) or not STORY_NAME_RE.fullmatch(name) or ".." in name:
                self.add(check(f"story.{i}", "FAIL", f"bad story name {name!r} (letters, digits, . _ -)"))
                continue
            sizes = s.get("sizes")
            if sizes is not None and (not isinstance(sizes, list) or not sizes
                                      or any(x not in SIZE_NAMES for x in sizes)):
                self.add(check(f"story.{name}", "FAIL", f"sizes {sizes!r} must be a non-empty list of "
                               + ", ".join(SIZE_NAMES)))
                continue
            mine = [(s, d) for d in DEVICES if sizes is None or f"{d[1]}x{d[2]}" in sizes]
            if not mine:
                self.add(check(f"story.{name}", "FAIL", "no capture jobs"))
            jobs += mine
        if not jobs:
            return
        try:
            for i, (s, (dev, w, h)) in enumerate(jobs, 1):
                self.status("stories", f"{i - 1}/{len(jobs)}")
                self._one_story(s, dev, w, h, ev_dir)
        finally:
            self.edit(STOP_SIM, "StopSimulationAsync")

    def _one_story(self, s: dict, dev: str, w: int, h: int, ev_dir: str):
        name, size = s["name"], f"{w}x{h}"
        cid = f"story.{name}@{size}"
        code = fill(SWITCH_AND_MOUNT, name=luau_long(dev), w=str(w), h=str(h),
                    module=luau_long(s.get("module") or ""), fixture=luau_value(s.get("fixture")),
                    size=luau_long(size), story=luau_long(name))
        text, err = self.edit(code, f"story {name}@{size}")
        res = None if err else extract_json(text)
        cap = {"story": name, "size": size, "path": None, "geometry_defects": None, "layout_lines": []}
        self.captures.append(cap)
        if not isinstance(res, dict):
            self.add(check(cid, "UNKNOWN", "mount output unparsable: " + (text or "")[:200]))
            self.edit(UNMOUNT, "unmount")
            return
        lines = [ln for ln in res.get("lines") or [] if isinstance(ln, str)]
        cap["layout_lines"] = lines[:60]
        cap["geometry_defects"] = res.get("defects")
        cap["summary"] = res.get("summary")
        problems = []
        if not res.get("ok"):
            problems.append("mount failed: " + str(res.get("error"))[:200])
        screen = res.get("screen") or {}
        if (screen.get("w"), screen.get("h")) != (w, h):
            problems.append(f"screen {screen.get('w')}x{screen.get('h')} != {size}")
        if type(res.get("defects")) is not int:                 # True is not a count (M4)
            problems.append("no defect count")
        elif res["defects"] > 0:
            problems.append(f"{res['defects']} layout defect(s)")
        fails = [ln for ln in lines if ln.strip().startswith("FAIL")]
        if fails:
            problems.append(f"{len(fails)} FAIL layout line(s): {fails[0][:120]}")
        unknown = False
        try:
            r = self.community.call_tool("capture_screenshot", {"format": "png"}, timeout=self.call_timeout)
            png = None if r["is_error"] else next((b for mime, b in r["images"] if "png" in mime), None)
            self.raw(f"capture {name}@{size}" + (" [tool error]" if r["is_error"] else ""),
                     "capture_screenshot png", r["text"][:300])
        except M.McpError as e:
            png, r = None, {"text": str(e)}
        if png is None:
            unknown = True
            problems.append("no PNG captured: " + r["text"][:120])
        else:
            path = os.path.join(ev_dir, f"{name}@{size}.png")
            if os.path.dirname(os.path.abspath(path)) != os.path.abspath(ev_dir):   # belt and braces
                raise RuntimeError(f"capture path escapes the evidence dir: {path}")
            with open(path, "wb") as f:
                f.write(png)
            cap["path"] = rel(self.repo, path)
            got = png_size(png)
            cap["capture_size"] = f"{got[0]}x{got[1]}" if got else None
            if got != (w, h):
                problems.append(f"capture {cap['capture_size']} != {size}")
        self.edit(UNMOUNT, "unmount")
        result = "PASS" if not problems else ("UNKNOWN" if unknown and len(problems) == 1 else "FAIL")
        self.add(check(cid, result, "; ".join(problems) or str(res.get("summary") or "ok"),
                       artifacts=[cap["path"]] if cap["path"] else []))

    # ---- snippet checks (wiring / touch / focus) --------------------------------
    def run_snippets(self, prefix: str, items: list):
        prev_end = None
        for i, it in enumerate(items or [], 1):
            self.status(prefix, f"{i - 1}/{len(items)}")
            cid = f"{prefix}.{it.get('id') or i}"
            vm, code = it.get("vm") or "client", it.get("code") or ""
            if not code:
                self.add(check(cid, "UNKNOWN", "no code"))
                continue
            t0 = time.monotonic()
            text, err = self.vm(vm, code, cid)
            gap = "" if not (it.get("tightBefore") and prev_end) else f" gap={1000 * (t0 - prev_end):.0f}ms"
            prev_end = time.monotonic()
            res, detail = lines_result(text, it.get("expect") or "PASS:", err)
            self.add(check(cid, res, detail + gap))

    def run_focus(self, items: list, stories: list, in_play: bool):
        by_name = {s.get("name"): s for s in stories or [] if isinstance(s, dict)}
        for i, it in enumerate(items or [], 1):
            it = {"panel": it} if isinstance(it, str) else dict(it)
            fid = it.get("id") or it.get("panel") or it.get("story") or str(i)
            cid = f"focus.{fid}"
            if it.get("story"):
                if in_play:
                    continue
                s = by_name.get(it["story"])
                if s is None:
                    self.add(check(cid, "UNKNOWN", f"story {it['story']!r} not in plan"))
                    continue
                code = fill(FOCUS_STORY, module=luau_long(s.get("module") or ""),
                            fixture=luau_value(s.get("fixture")), id=luau_long(fid))
                text, err = self.edit(code, cid)
            else:
                if not in_play:
                    continue
                code = fill(FOCUS_PANEL, panel=luau_long(it.get("panel") or ""), id=luau_long(fid))
                text, err = self.vm("client", code, cid)
            res, detail = lines_result(text, "PASS: FOCUS", err)
            self.add(check(cid, res, detail))

    # ---- playtest ---------------------------------------------------------------
    def start_playtest(self, wait: float = 90.0, cid: str = "host.playtest", sink=None) -> bool:
        """Start a solo playtest and wait for a player + SETTLE_S. Any start that was
        issued and did not end with a player is stopped again here (M1)."""
        add = (sink.append if sink is not None else self.add)
        try:
            r = self.community.call_tool("solo_playtest", {"action": "start", "mode": "play"},
                                         timeout=self.call_timeout)
            self.raw("solo_playtest start", "{}", r["text"])
            # Studio still leaving the previous playtest: "Wait for Studio to finish its current
            # playtest transition" (UI-02a, 2026-10-20). Nothing started; retry for ~30 s.
            for _ in range(10):
                if not (r["is_error"] or "not ready" in r["text"]) or "transition" not in r["text"]:
                    break
                time.sleep(3.0)
                r = self.community.call_tool("solo_playtest", {"action": "start", "mode": "play"},
                                             timeout=self.call_timeout)
                self.raw("solo_playtest start (retry)", "{}", r["text"])
            self._playing = True
            if r["is_error"] or ('"success":false' in r["text"].replace(" ", "")
                                 and "transition" in r["text"]):
                add(check(cid, "UNKNOWN", "solo_playtest start: " + r["text"][:200]))
                self.stop_playtest()
                return False
        except M.McpError as e:
            add(check(cid, "UNKNOWN", str(e)))
            if self._playing or "timed out" in str(e):
                self._playing = True
                self.stop_playtest()
            return False
        self.status("playtest", "waiting for a player")
        end = time.monotonic() + wait
        while time.monotonic() < end:
            text, err = self.vm("server", 'return #game:GetService("Players"):GetPlayers()', "player?", 15)
            m = re.search(r"\b(\d+)\b", text or "")
            if not err and m and int(m.group(1)) >= 1:
                time.sleep(SETTLE_S)
                add(check(cid, "PASS", "solo playtest running, player in world"))
                return True
            time.sleep(1.0)
        add(check(cid, "UNKNOWN", "no player in world after start (playtest stopped again)"))
        self.stop_playtest()
        return False

    def stop_playtest(self):
        with contextlib.suppress(M.McpError):
            r = self.community.call_tool("solo_playtest", {"action": "stop"}, timeout=self.call_timeout)
            self.raw("solo_playtest stop", "{}", r["text"])
        self._playing = False

    # ---- regression CALL plan ---------------------------------------------------
    def plan(self, selection: str) -> dict:
        code = (FRESH_GATE + 'return __freshGate()'
                f".run({luau_long(selection)})")
        text, err = self.edit(code, "GateRunner.run")
        p = parse_plan("" if err else text)
        if err:
            p["error"] = "GateRunner.run tool error: " + (text or "")[:200]
        return p

    @staticmethod
    def _is_edit_call(c: dict) -> bool:
        return c["kind"] == "edit" or (c["kind"] == "bridge" and c.get("vm") == "edit")

    @staticmethod
    def _needs_play(c: dict) -> bool:
        return c["kind"] in ("server", "client") or (c["kind"] == "bridge" and c.get("vm") != "edit")

    def suite_groups(self, mapping: dict, calls: dict, entries: list) -> list:
        """Run units, in order: every suite in its own fresh playtest unless it opts out
        with isolation = "shared" (consecutive shared suites share one); bridge suites are
        ALWAYS alone (their SL:Boot() re-boot hijacks the RemoteFunctions of every later
        suite; triage 2026-10-20). RUN_LAST / runLast suites go last; an entry with
        rerunAfter = {"S48"} runs again (fresh) after S48 when it first ran before it."""
        by_entry = collections.defaultdict(list)
        for n, (e, si) in sorted(mapping.items()):
            by_entry[e.get("id")].append((n, si))
        ordered = ([e for e in entries if not (e.get("id") in RUN_LAST or e.get("runLast") is True)]
                   + [e for e in entries if e.get("id") in RUN_LAST or e.get("runLast") is True])

        def unit(es, rerun=None):
            ns = [n for e in es for n, _ in by_entry[e.get("id")]]
            waits = {}
            for e in es:
                w = entry_waits(e)
                for n, si in by_entry[e.get("id")]:
                    if si in w:
                        waits[n] = w[si]
            return {"label": "+".join(str(e.get("id")) for e in es) + (f"@rerun-after-{rerun}" if rerun else ""),
                    "entries": es, "calls": [calls[n] for n in ns], "waits": waits, "rerun": rerun}
        groups, shared = [], []
        for e in ordered:
            if e.get("isolation") == "shared" and not is_bridge_entry(e):
                shared.append(e)
                continue
            if shared:
                groups.append(unit(shared))
                shared = []
            groups.append(unit([e]))
        if shared:
            groups.append(unit(shared))
        out, pos = [], {}
        for g in groups:
            out.append(g)
            for e in g["entries"]:
                pos[e.get("id")] = len(out) - 1
        final = []
        for i, g in enumerate(out):
            final.append(g)
            for e in entries:                       # reruns triggered by this group's suites
                for trig in e.get("rerunAfter") or []:
                    if trig in [x.get("id") for x in g["entries"]] and e.get("id") in pos \
                            and pos[e.get("id")] < i:
                        final.append(unit([e], rerun=trig))
        return final

    def _exec_calls(self, calls: list, g: dict, att: dict, entry_of: dict):
        res, chk, tool_err = att["res"], att["chk"], att["tool_err"]
        for i, c in enumerate(calls, 1):
            n, kind = c["n"], c["kind"]
            if not c.get("tight"):           # nothing between a tight pair (M6)
                self.status("regression", f"{g['label']} {i - 1}/{len(calls)}")
            if kind == "skip":
                res[n] = "SKIP offstudio — recorded as SKIP, never as PASS"
                self.raw(f"CALL {n} skip", c["line"], res[n])
                continue
            if kind == "manual":
                self.raw(f"CALL {n} manual", c["line"], "not executable by the runner (no evidence)")
                chk.append(check(f"regression.call{n}", "UNKNOWN",
                                 "MANUAL call: not executable by the runner, no evidence"))
                continue
            if g["waits"].get(n):
                time.sleep(g["waits"][n])
            t0 = time.monotonic()
            if kind == "bridge":
                text, err = self.run_bridge(c)
            else:
                payload = c["payload"]
                if c["dynarg"]:
                    mine = entry_of.get(n)
                    earlier = [res[k] for k in att["order"]
                               if k not in tool_err and (mine is None or entry_of.get(k) == mine)]
                    payload, missing = resolve_dynarg(payload, c["tokens"], earlier)
                    if missing:
                        others = [t for k, t in self._all_results.items() if entry_of.get(k) != mine]
                        _, still = resolve_dynarg(c["payload"], missing, others)
                        why = (" (found only in another suite's output)" if mine is not None
                               and len(still) < len(missing) else "")
                        res[n] = f"FAIL: GATE dynarg CALL {n} — unresolved <{'>, <'.join(missing)}>{why}"
                        self.raw(f"CALL {n} dynarg", c["payload"], res[n])
                        chk.append(check(f"regression.call{n}", "UNKNOWN" if why else "FAIL", res[n]))
                        att["order"].append(n)
                        continue
                text, err = self.vm(kind, payload, f"CALL {n}" + (" tight" if c["tight"] else ""))
            if c.get("tight") and self._last_end is not None:
                gap = 1000 * (t0 - self._last_end)
                if gap > TIGHT_GAP_MS:
                    chk.append(check(f"regression.tight{n}", "UNKNOWN",
                                     f"tightBefore CALL {n} issued {gap:.0f}ms after CALL {n - 1}"))
            self._last_end = time.monotonic()
            if err:
                # never hand a failed call's printed lines to the tally as if it ran (H2)
                res[n] = f"FAIL: GATE call {n} error — {one_line(text)}"
                tool_err.append(n)
                chk.append(check(f"regression.call{n}", "UNKNOWN", "tool error/timeout: " + one_line(text)))
            else:
                text = flatten_value(text)
                if kind == "bridge" and not text.strip():
                    res[n] = f"FAIL: GATE call {n} empty reply"
                    tool_err.append(n)
                    chk.append(check(f"regression.call{n}", "FAIL", res[n]))
                else:
                    res[n] = text
            att["order"].append(n)

    def _run_group(self, g: dict, boot: list, entry_of: dict) -> dict:
        att = {"res": {}, "chk": [], "tool_err": [], "order": [], "boot_bad": False, "boot": {}}
        ins = [c["n"] for c in g["calls"]]
        for c in g["calls"]:
            if c.get("tight") and c["n"] - 1 in ins:
                prev = next(x for x in g["calls"] if x["n"] == c["n"] - 1)
                if self._is_edit_call(prev) != self._is_edit_call(c):
                    att["chk"].append(check(f"regression.tight{c['n']}", "UNKNOWN",
                                            f"tight pair CALL {c['n'] - 1}/{c['n']} spans the Edit/play split"))
        edit = [c for c in g["calls"] if self._is_edit_call(c)]
        play = [c for c in g["calls"] if not self._is_edit_call(c)]
        self._exec_calls(edit, g, att, entry_of)          # Studio refuses Edit calls during play
        if not any(self._needs_play(c) for c in play):
            self._exec_calls(play, g, att, entry_of)
            return att
        self.status("playtest", g["label"])
        if not self.start_playtest(cid=f"regression.playtest.{g['label']}", sink=att["chk"]):
            for c in play:
                if self._needs_play(c):
                    att["res"][c["n"]] = f"FAIL: GATE call {c['n']} error — no playtest"
                    att["tool_err"].append(c["n"])
            return att
        try:
            for b in boot:                                  # boot gate per playtest (H1)
                text, err = self.vm(b["kind"], b["payload"], f"CALL {b['n']} boot [{g['label']}]")
                ok, why = boot_ok(text, err)
                att["boot"][b["n"]] = flatten_value(text) if ok else f"FAIL: GATE boot CALL {b['n']} — {why}"
                if not ok:
                    att["boot_bad"] = True
                    att["chk"].append(check("regression.boot", "UNKNOWN" if err else "FAIL",
                                            f"CALL {b['n']} in the playtest for {g['label']}: {why}"))
                    return att
            for e in g["entries"]:                          # per-entry setup steps
                for k, step in enumerate(e.get("setup") or [], 1):
                    sp = step_payload(e, str(step))
                    cid = f"regression.{e.get('id')}.setup{k}"
                    if sp is None:
                        att["chk"].append(check(cid, "UNKNOWN", f"setup step {step!r}: no runnable VM"))
                        continue
                    text, err = self.vm(sp[0], sp[1], f"setup {e.get('id')} {k}")
                    res, detail = lines_result(flatten_value(text) + "\nPASS: setup ran", "PASS:", err)
                    att["chk"].append(check(cid, res, detail if res != "PASS" else one_line(text) or "ok"))
                    if err:
                        att["tool_err"].append(f"setup{k}")
            self._exec_calls(play, g, att, entry_of)
        finally:
            self.stop_playtest()
        return att

    @staticmethod
    def _assert_fail(att: dict) -> bool:
        for n, t in att["res"].items():
            if n in att["tool_err"]:
                continue
            for ln in str(t).splitlines():          # runner-made FAIL: GATE lines are consequences
                if ln.startswith("FAIL: ") and not ln.startswith("FAIL: GATE "):
                    return True
        return any(c["result"] == "FAIL" and ".setup" in c["id"] for c in att["chk"])

    def regression_lane(self, selection: str) -> None:
        """Plan (Edit) -> per suite group: its Edit steps, a fresh playtest, the boot gate
        CALL 1-2 (must PASS, else the line stops), setup steps, its play steps, stop ->
        GateRunner.tally over the ORIGINAL CALL numbers."""
        self.regression = {"selection": selection, "suites": {}, "tally": ""}
        try:
            self.edit(SET_DESKTOP_DEVICE, "regression viewport AU_1920x1080")
        except Exception as e:                                    # noqa: BLE001
            self.raw("regression viewport", "", f"could not set AU_1920x1080: {e}")
        plan = self.plan(selection)
        if plan["error"]:
            self.add(check("regression.plan", "FAIL" if "FAIL:" in plan["error"] else "UNKNOWN",
                           plan["error"]))
            return
        calls = {c["n"]: c for c in plan["calls"]}
        boot = [calls[n] for n in (1, 2) if n in calls and BOOT_RE.search(calls[n]["payload"])
                and calls[n]["kind"] in ("server", "client")]
        if len(boot) != 2:
            self.add(check("regression.boot", "UNKNOWN", "CALL 1-2 are not the boot gate"))
            return
        index = self.index if self.index is not None else load_index(self.repo, self.head or "HEAD")
        mapping = map_calls(plan, index, selection)
        if mapping is None:
            self.add(check("regression.mapping", "UNKNOWN",
                           "the CALL plan does not match tests/RegressionIndex.lua at HEAD: suites ran "
                           "in ONE shared playtest without isolation"))
            rest = [c for n, c in sorted(calls.items()) if n not in (1, 2)]
            groups = [{"label": "all", "entries": [], "calls": rest, "waits": {}, "rerun": None}]
            entry_of = {}
        else:
            groups = self.suite_groups(mapping, calls, select_entries(index, selection))
            entry_of = {n: e.get("id") for n, (e, _) in mapping.items()}
        results, boots, played = {}, {}, 0
        self._all_results = results
        for gi, g in enumerate(groups):
            att = self._run_group(g, boot, entry_of)
            if att["tool_err"] and not att["boot_bad"] and not self._assert_fail(att) and mapping:
                self.raw(f"RETRY {g['label']}", "",
                         f"first run had tool errors/timeouts at {att['tool_err']} and no assertion "
                         f"FAIL: re-running once in a fresh playtest (both runs are in this log)")
                first = att
                att = self._run_group(g, boot, entry_of)
                att["chk"].append(check(f"regression.{g['label']}.retry", "SKIP",
                                        f"isolation-sensitive: first run tool errors at {first['tool_err']}; "
                                        f"second run used", required=False))
            played += bool(att["boot"])
            for n, t in att["boot"].items():
                if n not in boots or not boots[n].startswith("FAIL"):
                    boots[n] = t
            self.checks.extend(att["chk"])
            if g.get("rerun"):
                e = g["entries"][0]
                texts = [att["res"][c["n"]] for c in g["calls"] if c["n"] in att["res"]]
                v, why = mirror_verdict(e, texts, offstudio=all(c["kind"] == "skip" for c in g["calls"]))
                if att["tool_err"]:
                    v, why = "UNKNOWN", f"tool errors at {att['tool_err']}"
                self.add(check(f"regression.{e.get('id')}.rerun_after_{g['rerun']}", v, why))
            else:
                results.update(att["res"])
            if att["boot_bad"]:
                left = [x["label"] for x in groups[gi + 1:]]
                if left:
                    self.add(check("regression.stopped", "FAIL", "boot gate failed: not run: "
                                   + ", ".join(left)))
                break
        results.update(boots)
        if played and not any(c["id"] == "regression.boot" for c in self.checks):
            self.add(check("regression.boot", "PASS", f"boot gate PASS in {played} playtest(s)"))
        self.status("tally", "")
        _record_tally(self, selection, self.tally(selection, results))

    def run_bridge(self, c: dict) -> tuple:
        f = c.get("file")
        if not f:
            return "UNKNOWN: bridge file not named in the CALL line", True
        if c["vm"] == "client":
            return "UNKNOWN: bridge suites in the client VM are not runner-executable (no HttpService)", True
        port = self.bridge.start()
        stem = os.path.splitext(os.path.basename(f))[0]
        code = fill(BRIDGE, file=luau_long(f), port=str(port),
                    reboot="false" if stem in NO_REBOOT_BRIDGE or c["vm"] == "edit" else "true")
        label = f"CALL {c['n']} bridge {f}"
        if c["vm"] == "edit":
            return self.edit(code, label, self.call_timeout * 3)
        before = self.console_lines()
        text, err = self._call(label, code, lambda: unwrap_community(self.community.call_tool(
            "execute_luau", {"code": code, "target": "server"}, timeout=self.call_timeout * 3)))
        if not err and not flatten_value(text).strip():
            # a suite that print()s instead of returning: its lines only reach the console
            got = []
            for ln in console_delta(before, self.console_lines()):
                m = re.search(r"((?:PASS|FAIL): .*)$", ln)
                if m:
                    got.append(m.group(1))
            if got:
                text = "\n".join(got)
                self.raw(f"{label} console fallback", "get_console_output delta", text)
        return text, err

    def tally(self, selection: str, results: dict) -> dict:
        code = (FRESH_GATE + 'return __freshGate()'
                f".tally({luau_long(selection)}, {luau_value({int(k): v for k, v in results.items()})})")
        text, err = self.edit(code, "GateRunner.tally", self.call_timeout * 2)
        t = parse_tally("" if err else text)
        t["raw"] = text
        return t

    # ---- console ----------------------------------------------------------------
    def console_lines(self) -> Optional[list]:
        try:
            r = self.official.call("get_console_output", {}, timeout=self.call_timeout)
        except M.McpError:
            return None
        return None if r["is_error"] else r["text"].splitlines()

    def console_check(self):
        end = self.console_lines()
        if end is None:
            self.add(check("console.errors", "UNKNOWN", "get_console_output unavailable"))
            return
        errs = collections.Counter(ln.strip() for ln in end if CONSOLE_ERR_RE.search(ln))
        base = collections.Counter(ln.strip() for ln in (self.console_base or []) if CONSOLE_ERR_RE.search(ln))
        new = errs - base
        n = sum(new.values())
        self.add(check("console.errors", "PASS" if n == 0 else "FAIL",
                       "no new console errors" if n == 0 else f"{n} new: " + " | ".join(list(new)[:3])))

    # ---- verdict ----------------------------------------------------------------
    def finish(self, error: Optional[str] = None, *, evidence: bool = False, meta: str = "") -> dict:
        """<head12>.<mode>.json (a stories/regression run can never overwrite or stand in
        for the sprint verdict; H4), latest.json + latest/<mode>.json, the raw log."""
        result = overall(self.checks, error)
        head = self.head or "0" * 40
        v = {"schema": 1, "sprint": self.sprint, "head": head, "mode": self.mode,
             "run_id": self.run_id, "started": self.started, "finished": time.time(), "result": result,
             "checks": self.checks, "captures": self.captures,
             "regression": self.regression, "summary_line": summary_line(self.sprint, result),
             "error": error}
        path = os.path.join(self.gate_dir, verdict_name(head, self.mode))
        self.verdict_path = path
        self.raw_path = os.path.join(self.gate_dir, f"{head[:12]}.{self.mode}.raw.log")
        with open(self.raw_path, "a", encoding="utf-8") as f:
            f.write("".join(self._raw))
        latest = [os.path.join(self.gate_dir, "latest.json"),
                  os.path.join(self.gate_dir, "latest", f"{self.mode}.json")]
        write_json(path, v)
        for p in latest:
            write_json(p, v)
        if v.get("captures"):
            try:                         # the review sheet is a convenience: never a verdict
                from . import review_sheet
                v["review_sheet"] = rel(self.repo, review_sheet.build_sheet(self.repo, path))
                write_json(path, v)
                for p in latest:
                    write_json(p, v)
            except Exception as e:                                # noqa: BLE001
                self._raw.append(f"REVIEW SHEET FAILED {type(e).__name__}: {e}\n")
        if evidence:
            append_evidence(self.repo, self.sprint, v, rel(self.repo, path), meta)
        self.status("done", "")
        return v


def verdict_name(head: str, mode: str) -> str:
    return f"{(head or '0' * 40)[:12]}.{mode}.json"


# ---- shared output helpers -------------------------------------------------------
def summary_line(sprint: str, result: str) -> str:
    return f"GATE {sprint} {'PASS' if result == 'PASS' else 'FAIL'}"


def rel(repo: str, path: str) -> str:
    try:
        return os.path.relpath(path, repo).replace("\\", "/")
    except ValueError:
        return path


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)
    for _ in range(20):                    # a reader holding the file open on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def evidence_block(sprint: str, v: dict, verdict_rel: str, meta: str = "") -> str:
    """The block adapter._log_evidence and tools/gate.ps1 -Finish read (H6):
    GATE RUN ..., `PASS: GATE HEAD <sha>` (only when host.head passed), the raw check
    lines, `=== GATE FINISH ===`, one `VERDICT PASS|FAIL: GATE <id>` per required check
    (UNKNOWN/SKIP -> FAIL), then `GATE <ID> PASS|FAIL`. Every Studio-sourced text is
    folded onto its own line so it can never forge a marker or a verdict line."""
    head = str(v.get("head") or "")
    checks = [c for c in v.get("checks") or [] if isinstance(c, dict)]
    lines = [f"=== GATE studio_gate ({v.get('mode')}) ===",
             one_line(f"GATE RUN sprint={sprint} mode={v.get('mode')} head={head[:12]} "
                      f"run_id={v.get('run_id') or '-'} {meta}", 2000)]
    if re.fullmatch(r"[0-9a-f]{40}", head) and any(
            c.get("id") == "host.head" and c.get("result") == "PASS" for c in checks):
        lines.append(f"PASS: GATE HEAD {head}")
    for c in checks:
        tail = "" if c.get("required") else " (not required)"
        det = f" — {one_line(c.get('detail'), 400)}" if c.get("detail") else ""
        lines.append(f"{c.get('result')}: GATE {one_line(c.get('id'), 120)}{det}{tail}")
    if v.get("error"):
        lines.append(f"FAIL: GATE runner error — {one_line(v['error'], 400)}")
    lines.append("=== GATE FINISH ===")
    for c in checks:
        if c.get("required"):
            ok = c.get("result") == "PASS"
            lines.append(f"VERDICT {'PASS' if ok else 'FAIL'}: GATE {one_line(c.get('id'), 120)}")
    if not any(c.get("required") for c in checks):
        lines.append("VERDICT FAIL: no required gate checks")
    if v.get("error"):
        lines.append("VERDICT FAIL: GATE runner error")
    lines.append(f"VERDICT FILE {verdict_rel}")
    lines.append(summary_line(sprint, v.get("result")))
    return "\n".join(lines) + "\n"


def append_evidence(repo: str, sprint: str, v: dict, verdict_rel: str, meta: str = "") -> str:
    path = os.path.join(repo, "docs", "progress", f"{sprint}_EVIDENCE_RAW.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = evidence_block(sprint, v, verdict_rel, meta)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(block)
    return path


def project_name(repo: str) -> Optional[str]:
    try:
        with open(os.path.join(repo, "default.project.json"), encoding="utf-8") as f:
            return json.load(f).get("name")
    except (OSError, ValueError):
        return None


def rojo_port(repo: str) -> int:
    try:
        with open(os.path.join(repo, "default.project.json"), encoding="utf-8") as f:
            return int(json.load(f).get("servePort") or DEFAULT_ROJO_PORT)
    except (OSError, ValueError, TypeError):
        return DEFAULT_ROJO_PORT


def show_at(repo: str, rev: str, path: str) -> Optional[str]:
    """`git show <rev>:<path>` text, or None when the file is not in that commit."""
    try:
        p = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True,
                           timeout=60, **_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.decode("utf-8-sig", "replace") if p.returncode == 0 else None


def plan_rel(sprint: str) -> str:
    return f"docs/progress/{sprint}_gate.json"


def load_plan(repo: str, sprint: str, rev: str = "HEAD") -> tuple:
    """(plan dict | None, problem | None), read from the COMMIT (git show), never the
    working tree: the implementer can't turn the gate into a no-op with an uncommitted
    plan edit (H5)."""
    rp = plan_rel(sprint)
    text = show_at(repo, rev, rp)
    if text is None:
        there = os.path.exists(os.path.join(repo, *rp.split("/")))
        return None, f"{rp} is not committed at {rev}" + (" (it exists only in the working tree)"
                                                          if there else "")
    try:
        plan = json.loads(text)
    except ValueError as e:
        return None, f"{rp} at {rev} is not valid JSON: {e}"
    if not isinstance(plan, dict):
        return None, f"{rp} at {rev} is not a JSON object"
    return plan, None


def load_index(repo: str, rev: str = "HEAD") -> dict:
    text = show_at(repo, rev, "tests/RegressionIndex.lua")
    if text is None:
        return {"entries": [], "subsets": {}}
    try:
        return parse_regression_index(text)
    except (ValueError, IndexError):
        return {"entries": [], "subsets": {}}


def project_at(repo: str, rev: str = "HEAD") -> Optional[dict]:
    text = show_at(repo, rev, "default.project.json")
    try:
        return json.loads(text) if text is not None else None
    except ValueError:
        return None


def suite_reader(repo: str, rev: str = "HEAD") -> Callable:
    cache = {}

    def read(file: str) -> str:
        if file not in cache:
            cache[file] = show_at(repo, rev, f"tests/{file}.lua") or ""
        return cache[file]
    return read


def plan_checks(plan: dict, files: list, explicit_selection: Optional[str], affected: str) -> list:
    """Sprint-plan sanity (H5/M3): UI screens under test need stories; an explicit empty
    selection may not skip suites the changed files map to."""
    out = []
    ui = [f for f in files if f.replace("\\", "/").startswith("src/client/UI/")]
    if ui and not (plan or {}).get("stories"):
        out.append(check("plan.stories", "FAIL", "UI files under test but the plan has no stories: "
                         + ", ".join(ui[:5])))
    if explicit_selection is not None and not str(explicit_selection).strip() and affected:
        out.append(check("plan.selection", "FAIL", f"selection is empty but the files under test map "
                         f"to {affected!r}; that regression runs instead"))
    return out


def studio_mode(text: str) -> str:
    """get_studio_state text -> edit | play | unknown."""
    data = extract_json(text)
    blob = json.dumps(data).lower() if data is not None else (text or "").lower()
    if re.search(r'"(is_?playing|is_?running|playtest(ing)?)"\s*:\s*true', blob) or re.search(
            r"\b(play mode|playing|running|run mode|simulating)\b", blob):
        return "play"
    if re.search(r"\bedit\b", blob):
        return "edit"
    return "unknown"


# ---- modes -----------------------------------------------------------------------
def run_doctor(repo: str, place: Optional[str], out=print, official=None, community=None,
               timeout: float = 60.0) -> int:
    g = Gate(repo, "doctor", "doctor", place=place, official=official, community=community,
             call_timeout=timeout, out=out)
    fixes = []

    def need(cid, ok, detail, fix):
        g.add(check(cid, "PASS" if ok is True else ("UNKNOWN" if ok is None else "FAIL"), detail))
        if ok is not True:
            fixes.append(f"{cid}: {fix}")

    try:
        try:
            names = g.official.client.tool_names()
            need("doctor.official_mcp", "execute_luau" in names, f"{len(names)} tools",
                 r"open Roblox Studio with the MCP plugin enabled; %LOCALAPPDATA%\Roblox\mcp.bat must exist")
        except M.McpError as e:
            need("doctor.official_mcp", False, str(e), r"open Roblox Studio; check %LOCALAPPDATA%\Roblox\mcp.bat")
        try:
            names = g.community.tool_names()
            need("doctor.community_mcp", "capture_screenshot" in names, f"{len(names)} tools",
                 f"install Node.js; run npx -y {M.COMMUNITY_PACKAGE} once; enable its Studio plugin")
        except M.McpError as e:
            need("doctor.community_mcp", False, str(e), f"install Node.js and {M.COMMUNITY_PACKAGE}")
        studio_ok = False
        try:
            g.official.resolve()
            studio_ok = True
            need("doctor.studio", True, f"{g.official.studio_name} ({g.official.studio_id})", "")
        except M.McpError as e:
            need("doctor.studio", False, str(e),
                 f"open exactly one Studio on the {g.place or 'AU'} place (or pass --place NAME)")
        try:
            com = g.community
            if isinstance(com, M.CommunityStudio):
                com.resolve()
                need("doctor.community_studio", True,
                     f"{com.place_name or 'one instance'} (instance {com.instance_id or '-'})", "")
        except M.McpError as e:
            need("doctor.community_studio", False, str(e),
                 "the community plugin must be connected to the same single Studio (or pass --place NAME)")
        if studio_ok:
            try:
                st = g.official.call("get_studio_state", {}, timeout=timeout)
                mode = studio_mode(st["text"])
                need("doctor.edit_mode", True if mode == "edit" else (None if mode == "unknown" else False),
                     f"mode={mode}: {st['text'][:120]}", "stop the playtest (Studio must be in Edit mode)")
            except M.McpError as e:
                need("doctor.edit_mode", None, str(e), "check get_studio_state")
            code = fill(DOCTOR, devices=luau_value([[d, w, h] for d, w, h in DEVICES]))
            text, err = g.edit(code, "doctor")
            d = None if err else extract_json(text)
            if not isinstance(d, dict):
                need("doctor.datamodel", None, (text or "")[:200], "execute_luau (Edit) failed; see raw log")
            else:
                need("doctor.show_dev_gui", d.get("showDev") is True, str(d.get("showDev")),
                     "set StarterGui.ShowDevelopmentGui = true")
                need("doctor.http_enabled", d.get("http") is True, str(d.get("http")),
                     "Game Settings > Security > Allow HTTP Requests = on (HttpService.HttpEnabled)")
                need("doctor.story_harness", d.get("harness") is True, str(d.get("harness")),
                     "commit tests/lib/StoryHarness.lua and let Rojo sync (ReplicatedStorage.TestLib)")
                need("doctor.gate_runner", d.get("gateRunner") is True and d.get("regressionIndex") is True,
                     f"GateRunner={d.get('gateRunner')} RegressionIndex={d.get('regressionIndex')}",
                     "Rojo sync ServerScriptService.GateRunner / RegressionIndex")
                devs = d.get("devices") or {}
                missing = [n for n, _, _ in DEVICES if devs.get(n) is not True]
                need("doctor.devices", not missing,
                     ("created " + ",".join(d.get("created") or []) if d.get("created") else "all present")
                     if not missing else "missing " + ",".join(missing) + " " + str(d.get("deviceError") or ""),
                     "create the AU_* devices in the Device Emulator (Name, Width, Height, PixelDensity 96)")
        port = rojo_port(repo)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rojo", timeout=3) as r:
                need("doctor.rojo", r.status == 200, f"rojo serve on {port}", "")
        except OSError as e:
            need("doctor.rojo", False, f"port {port}: {e}",
                 f"start the one rojo serve on port {port} (docs/ENVIRONMENT.md) and connect the plugin")
        lune = shutil.which("lune")
        need("doctor.lune", bool(lune), lune or "not on PATH", "run rokit install in the AU repo")
    finally:
        g.close()
    for c in g.checks:
        out(f"{c['result']}: {c['id']} — {c['detail']}")
    for f in fixes:
        out(f"FIX {f}")
    ok = overall(g.checks) == "PASS"
    out(f"DOCTOR {'READY' if ok else 'NOT READY'}")
    return 0 if ok else 2


def _sprint_name(a) -> str:
    return a.sprint or (f"regression-{re.sub(r'[^A-Za-z0-9]+', '_', a.selection or 'x')}"[:64])


def run_gate(a, out=print, official=None, community=None, run_cmd=default_run_cmd) -> int:
    mode = a.cmd
    sprint = _sprint_name(a)
    run_id = getattr(a, "run_id", None) or os.environ.get("FOUNDRY_GATE_RUN_ID") or ""
    busy = acquire_lock(a.repo, sprint, mode)
    if busy:                      # never touch another runner's status / Studio session
        out(f"GATE {sprint} BUSY: another studio_gate runner is driving Studio ({busy})")
        return 2
    try:
        return _run_gate(a, mode, sprint, run_id, out, official, community, run_cmd)
    finally:
        release_lock(a.repo)


class _MockDone(Exception):
    """The mock lane finished the sprint branch early (not an error)."""


def _run_gate(a, mode, sprint, run_id, out, official, community, run_cmd) -> int:
    g = Gate(a.repo, sprint, mode, place=a.place, official=official, community=community,
             run_cmd=run_cmd, call_timeout=a.timeout, out=out, run_id=run_id)
    g.status("starting", "")
    error = None
    meta = ""
    try:
        if mode == "sprint":
            plan, problem = load_plan(g.repo, sprint)
            files = [f.strip() for f in (a.files or "").split(",") if f.strip()]
            files += [f for f in (plan or {}).get("files") or [] if isinstance(f, str) and f not in files]
            if plan_rel(sprint) not in files:
                files.append(plan_rel(sprint))       # the plan itself must be committed (clean tree)
            g.add(check("host.gate_plan", "FAIL" if problem else "PASS",
                        problem or f"{plan_rel(sprint)} read from HEAD"))
            plan = plan or {}
            expect = g.host_checks(files, a.tier)
            g.index = load_index(g.repo, g.head or "HEAD")
            affected = affected_selection(files, g.index, suite_text=suite_reader(g.repo, g.head or "HEAD"),
                                          comment_only=lambda f: comment_only_change(g.repo, f, a.base))
            explicit = a.selection if a.selection is not None else plan.get("selection")
            selection = affected if explicit is None or not str(explicit).strip() else str(explicit)
            for c in plan_checks(plan, files, explicit, affected):
                g.add(c)
            meta = f"files=[{','.join(files)}] selection={selection or 'none'} tier={a.tier}"
            if plan.get("lane") == "mock":
                # UI-D1-style design sprint: HTML mock frames only; no Studio, no playtest,
                # no regression (nothing under src/ or tests/ changes in a mock sprint)
                meta += " lane=mock"
                g.run_mock(plan.get("stories") or [])
                g.add(check("regression.selection", "SKIP", "mock lane: no Studio suites", required=False))
                raise _MockDone()
            g.preflight()
            g.console_base = g.console_lines()
            g.live_verify(expect)
            stories = plan.get("stories") or []
            if stories:
                g.run_stories(stories)
            g.run_focus(plan.get("focus"), stories, in_play=False)
            live = plan.get("wiring") or plan.get("touch") or any(
                not isinstance(x, dict) or not x.get("story") for x in plan.get("focus") or [])
            if live:
                g.status("playtest", "wiring")
                if g.start_playtest():
                    try:
                        g.run_snippets("wiring", plan.get("wiring"))
                        g.run_snippets("touch", plan.get("touch"))
                        g.run_focus(plan.get("focus"), stories, in_play=True)
                    finally:
                        g.stop_playtest()
            if selection:
                g.regression_lane(selection)
            else:
                g.add(check("regression.selection", "SKIP", "no affected suites", required=False))
            g.console_check()
        elif mode == "stories":
            rc, head = run_cmd(["git", "rev-parse", "HEAD"], g.repo, 30)
            g.head = head.strip() if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", head.strip()) else None
            plan, problem = load_plan(g.repo, sprint, g.head or "HEAD")
            if problem:
                g.add(check("host.gate_plan", "FAIL", problem))
            elif plan.get("lane") == "mock":
                g.run_mock(plan.get("stories") or [], a.story)
            else:
                g.preflight()
                g.console_base = g.console_lines()
                g.run_stories(plan.get("stories") or [], a.story)
                g.run_focus(plan.get("focus"), plan.get("stories") or [], in_play=False)
                g.console_check()
        elif mode == "regression":
            rc, head = run_cmd(["git", "rev-parse", "HEAD"], g.repo, 30)
            g.head = head.strip() if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", head.strip()) else None
            g.index = load_index(g.repo, g.head or "HEAD")
            g.preflight()
            g.console_base = g.console_lines()
            g.regression_lane(a.selection)
            g.console_check()
    except _MockDone:
        pass
    except Exception as e:                                   # tooling failure -> ERROR, exit 2
        error = f"{type(e).__name__}: {e}"
    finally:
        with contextlib.suppress(Exception):
            if g._playing:                                   # an interrupt mid-playtest
                g.stop_playtest()
        g.close()
    v = g.finish(error, evidence=(mode == "sprint"), meta=meta)
    for c in v["checks"]:
        if c["result"] != "PASS":
            out(f"{c['result']}: {c['id']} — {c['detail']}")
    if error:
        out(f"ERROR: {error}")
    out(f"VERDICT {rel(g.repo, g.verdict_path)}")
    out(v["summary_line"])
    return 2 if v["result"] == "ERROR" else (0 if v["result"] == "PASS" else 1)


def _record_tally(g: Gate, selection: str, t: dict):
    for sid, s in t["suites"].items():
        v = s["verdict"]
        g.add(check(f"regression.{sid}", v, s["line"], required=v != "SKIP"))
    raw = t.get("raw") or ""
    g.add(check("regression.verdict", t["verdict"],
                next((ln for ln in raw.splitlines()
                      if ln.startswith(("PASS: GATE regression", "FAIL: GATE regression"))), raw[:200])))
    g.regression = {"selection": selection, "suites": {k: v["verdict"] for k, v in t["suites"].items()},
                    "tally": raw}


# ---- background + status ---------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259                 # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lock_path(repo: str) -> str:
    return os.path.join(os.path.abspath(repo), ".plan", "tmp", "gate", LOCK_NAME)


def lock_holder(repo: str) -> Optional[str]:
    """Who holds the runner lock (a live pid other than ours), or None."""
    p = _lock_path(repo)
    cur = _read(p)
    if cur is None:
        try:                              # just created by another runner, not written yet
            if time.time() - os.path.getmtime(p) < 5:
                return "a runner that is starting"
        except OSError:
            pass
        return None
    pid = int(cur.get("pid") or 0) if isinstance(cur, dict) else 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return f"pid {pid}, {cur.get('mode')} --sprint {cur.get('sprint')}"
    return None


def acquire_lock(repo: str, sprint: str, mode: str) -> Optional[str]:
    """One runner drives Studio at a time (H4): None when taken, else the holder."""
    p = _lock_path(repo)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = lock_holder(repo)
            if holder:
                return holder
            with contextlib.suppress(OSError):
                os.remove(p)                   # stale (dead pid)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "sprint": sprint, "mode": mode, "started": time.time()}, f)
        return None
    return lock_holder(repo) or "the runner lock could not be taken"


def release_lock(repo: str) -> None:
    p = _lock_path(repo)
    cur = _read(p)
    if isinstance(cur, dict) and cur.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            os.remove(p)


def status_cmd(repo: str, sprint: str) -> str:
    return (f"py -m {MODULE} --repo {os.path.abspath(repo)} status --sprint {sprint} --wait {WAIT_DEFAULT_MINUTES:g}")


def start_background(argv: list, repo: str, sprint: str, out=print) -> int:
    child, skip = [], False
    for x in argv:                                  # absolute --repo first (child cwd differs)
        if skip:
            skip = False
        elif x == "--repo":
            skip = True
        elif x != "--background" and not x.startswith("--repo="):
            child.append(x)
    child = ["--repo", os.path.abspath(repo)] + child
    busy = lock_holder(repo)
    if busy:                     # never clear the status.json of a runner that is working
        out(f"GATE {sprint} BUSY: another studio_gate runner is driving Studio ({busy})")
        return 2
    gate_dir = os.path.join(os.path.abspath(repo), ".plan", "tmp", "gate", sprint)
    os.makedirs(gate_dir, exist_ok=True)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(gate_dir, "status.json"))
    log = open(os.path.join(gate_dir, "run.log"), "a", encoding="utf-8")
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"          # stdout is a file: never cp1252 (M9)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | CREATE_NO_WINDOW  # DETACHED | NEW_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen([sys.executable, "-m", MODULE] + child, cwd=pkg_root, env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kw)
    log.close()
    sp = os.path.join(gate_dir, "status.json")
    if not os.path.exists(sp):                      # the child may already own it (fast runs)
        write_json(sp, {"sprint": sprint, "pid": p.pid, "started": time.time(), "phase": "starting",
                        "progress": "", "updated": time.time(), "mode": "background"})
    out(f"GATE {sprint} STARTED pid={p.pid} (background; log .plan/tmp/gate/{sprint}/run.log)")
    out(f"NEXT: {status_cmd(repo, sprint)}")
    return 0


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_status(repo: str, sprint: str, wait: Optional[float], out=print, poll: Optional[float] = None) -> int:
    gate_dir = os.path.join(os.path.abspath(repo), ".plan", "tmp", "gate", sprint)
    poll = poll if poll is not None else float(os.environ.get("FOUNDRY_GATE_POLL_SECONDS") or 5.0)
    wait_s = max(0.0, min(float(wait or 0.0), WAIT_MAX_MINUTES)) * 60.0
    end = time.monotonic() + wait_s
    last = None
    while True:
        st = _read(os.path.join(gate_dir, "status.json"))
        mode = (st or {}).get("mode")
        latest = (_read(os.path.join(gate_dir, "latest", f"{mode}.json")) if mode else None) \
            or _read(os.path.join(gate_dir, "latest.json"))
        done = st is None or st.get("phase") == "done"
        if done and latest:
            for c in latest.get("checks") or []:
                if c.get("result") != "PASS":
                    out(f"{c['result']}: {c['id']} — {c.get('detail', '')}")
            out(f"RESULT {latest.get('result')} head={(latest.get('head') or '')[:12]} mode={latest.get('mode')}")
            out(latest.get("summary_line") or summary_line(sprint, latest.get("result")))
            return 0 if latest.get("result") == "PASS" else (2 if latest.get("result") == "ERROR" else 1)
        if st is None:
            out(f"GATE {sprint} NONE (no status.json or latest.json under .plan/tmp/gate/{sprint})")
            return 2
        cur = (st.get("phase"), st.get("progress"))
        if cur != last:
            out(f"PHASE {cur[0]} {cur[1] or ''}".rstrip())
            last = cur
        if not _pid_alive(int(st.get("pid") or 0)):
            # the runner wrote 'done' just before exiting? re-read once
            st2 = _read(os.path.join(gate_dir, "status.json"))
            if st2 and st2.get("phase") == "done":
                continue
            out(f"GATE {sprint} ERROR runner pid {st.get('pid')} is not running (phase {cur[0]})")
            return 2
        if time.monotonic() >= end:
            out(f"GATE {sprint} RUNNING phase={cur[0]} progress={cur[1] or '-'} — re-run: {status_cmd(repo, sprint)}")
            return 2
        time.sleep(min(poll, max(0.05, end - time.monotonic())))


def stop_playtest_cmd(repo: str, place: Optional[str], out=print, community=None) -> int:
    """Best-effort cleanup after a runner was killed mid-playtest (fast_lane._kill)."""
    busy = acquire_lock(repo, "stop-playtest", "stop-playtest")
    if busy:
        out(f"BUSY: another studio_gate runner is driving Studio ({busy})")
        return 2
    c = None
    try:
        c = community or M.connect("community")
        com = c if not isinstance(c, M.McpClient) else M.CommunityStudio(c, place=place)
        r = com.call_tool("solo_playtest", {"action": "stop"}, timeout=60)
        out(("STOP FAILED: " if r["is_error"] else "STOPPED: ") + one_line(r["text"]))
        return 1 if r["is_error"] else 0
    except M.McpError as e:
        out(f"STOP FAILED: {e}")
        return 2
    finally:
        if c is not None and community is None:
            with contextlib.suppress(Exception):
                c.close()
        release_lock(repo)


# ---- CLI -------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"py -m {MODULE}", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--place", default=None, help="Studio name substring (default: the single open Studio)")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per MCP tool call")
    ap.add_argument("--run-id", dest="run_id", default=None,
                    help="caller's run nonce, written into the verdict (default env FOUNDRY_GATE_RUN_ID)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("stop-playtest", help="stop a solo playtest a killed runner left running")
    s = sub.add_parser("stories")
    s.add_argument("--sprint", required=True)
    s.add_argument("--story", action="append")
    s.add_argument("--background", action="store_true")
    s = sub.add_parser("sprint")
    s.add_argument("--sprint", required=True)
    s.add_argument("--files", default="")
    s.add_argument("--selection", default=None)
    s.add_argument("--tier", choices=["A", "B", "C"], default="B")
    s.add_argument("--base", default="HEAD~1", help="base rev for the comment-only Remotes.lua check")
    s.add_argument("--background", action="store_true")
    s = sub.add_parser("regression")
    s.add_argument("--selection", required=True)
    s.add_argument("--sprint", default=None)
    s.add_argument("--background", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--sprint", required=True)
    s.add_argument("--wait", type=float, nargs="?", const=WAIT_DEFAULT_MINUTES, default=None,
                   help=f"block up to MINUTES (default {WAIT_DEFAULT_MINUTES:g}, max {WAIT_MAX_MINUTES:g})")
    return ap


def main(argv=None, out=print, **inject) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    a = build_parser().parse_args(argv)
    for k in ("story", "files", "selection", "tier", "base"):
        if not hasattr(a, k):
            setattr(a, k, None if k != "tier" else "B")
    if getattr(a, "sprint", None):
        safe_id(a.sprint)
    # --repo must be the absolute game-repo root. A POSIX shell turns C:\Roblox\X into the
    # drive-relative "C:RobloxX", and the runner then wrote evidence under a stray relative
    # folder inside the repo (UI-D1, 2026-10-20). Refuse instead of guessing.
    repo_arg = str(a.repo or "")
    if not (os.path.isabs(repo_arg) and os.path.exists(os.path.join(repo_arg, ".git"))
            and os.path.isfile(os.path.join(repo_arg, "default.project.json"))):
        out(f"ERROR: --repo {repo_arg!r} is not the absolute game repo root (a folder with .git "
            "and default.project.json). Use forward slashes, e.g. --repo C:/Roblox/AnimalUprising")
        return 2
    a.repo = os.path.abspath(repo_arg)
    if a.cmd == "doctor":
        busy = acquire_lock(a.repo, "doctor", "doctor")
        if busy:
            out(f"BUSY: another studio_gate runner is driving Studio ({busy})")
            out("DOCTOR NOT READY")
            return 2
        try:
            return run_doctor(a.repo, a.place, out=out, timeout=min(a.timeout, 60.0),
                              official=inject.get("official"), community=inject.get("community"))
        finally:
            release_lock(a.repo)
    if a.cmd == "status":
        return run_status(a.repo, a.sprint, a.wait, out=out, poll=inject.get("poll"))
    if a.cmd == "stop-playtest":
        return stop_playtest_cmd(a.repo, a.place, out=out, community=inject.get("community"))
    if getattr(a, "background", False):
        sprint = a.sprint or f"regression-{re.sub(r'[^A-Za-z0-9]+', '_', a.selection or 'x')}"[:64]
        if not a.sprint:
            argv += ["--sprint", sprint] if a.cmd == "regression" else []
        return start_background(argv, a.repo, sprint, out=out)
    return run_gate(a, out=out, **inject)


def _interrupt(sig, frame):
    raise GateInterrupted(f"interrupted by signal {sig}")


def cli() -> int:
    for stream in (sys.stdout, sys.stderr):                  # M9: Studio text is not cp1252
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    import signal
    for name in ("SIGTERM", "SIGBREAK"):                     # unwind: stop playtest, close MCP
        if hasattr(signal, name):
            with contextlib.suppress(Exception):
                signal.signal(getattr(signal, name), _interrupt)
    return main()


if __name__ == "__main__":
    sys.exit(cli())
