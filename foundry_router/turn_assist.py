"""Turn assist — rule-based help for coding-client sessions (Cline) in direct mode.

Everything here is deterministic Python over the conversation the client sent;
nothing calls a model (the frontier helpers live in frontier_assist.py). It is
configured per persona through the `assist` JSON column; every feature is off
unless that JSON turns it on.

CACHE RULE. llama.cpp keeps the previous turn's prompt cached, so a change to
an older message forces a re-read of everything after it (minutes at 150K).
Every transform below is therefore a pure function of the messages BEFORE the
one it changes: the same history always produces the same rewritten history,
turn after turn, so the prefix the backend cached last turn is reproduced
byte-for-byte and only the new tail is processed.

Features (persona `assist` keys):
  think_policy   client | persona | adaptive — adaptive picks the level per turn
                 from what just happened (read → routine, edit → after_edit,
                 failure / new user message / supervisor note → after_failure)
  dedupe_reads   a read_files result identical to an earlier one becomes a
                 pointer to that earlier copy
  roblox_cleanup strip Roblox Studio's Assistant-plugin stack prefix from Luau
                 errors (keeping the real error + line) and add a recovery hint
                 to "studio_id is not connected"
  image_keep     keep only the newest N screenshots in context (0 = keep all)
  prejudge       off | observe | enforce — risky-command rules + PowerShell
                 fixes applied to the model's tool calls before the client runs them
  supervisor     off | observe | nudge | escalate — loop / cycling / error-streak
                 detectors; nudge appends a note the MODEL reads to the tool
                 result where the problem showed
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

ASSIST_DEFAULTS: dict[str, Any] = {
    "think_policy": "persona",
    "think_levels": {"routine": "low", "after_edit": "medium", "after_failure": "high"},
    "dedupe_reads": False,
    "roblox_cleanup": False,
    "image_keep": 0,
    "soft_budget_tokens": 0,
    "prejudge": "off",
    "prejudge_max_edit_chars": 6000,
    "supervisor": "off",
    "auditor": "off",
    "auditor_min_tool_turns": 20,
    "auditor_max_rejections": 2,
    "advisor": False,
    "frontier_models": [],
    "frontier_max_calls": 3,
    "frontier_rounds": 4,
}

NOTE_TAG = "[Foundry"            # every note this module writes starts with it


def config(persona: Optional[dict]) -> dict:
    """The persona's assist settings merged over the defaults."""
    raw = (persona or {}).get("assist")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
    cfg = json.loads(json.dumps(ASSIST_DEFAULTS))
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "think_levels" and isinstance(v, dict):
                cfg[k].update(v)
            elif k in cfg:
                cfg[k] = v
    return cfg


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("dedupe_reads") or cfg.get("roblox_cleanup")
                or int(cfg.get("image_keep") or 0) > 0
                or cfg.get("think_policy") == "adaptive"
                or cfg.get("prejudge", "off") != "off"
                or cfg.get("supervisor", "off") != "off"
                or cfg.get("auditor", "off") != "off"
                or cfg.get("advisor"))


# ---- history parsing ----------------------------------------------------------

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) if isinstance(p, dict) else str(p)
                         for p in content)
    return "" if content is None else str(content)


def _args(tc: dict) -> dict:
    fn = tc.get("function") or tc
    a = fn.get("arguments")
    if isinstance(a, str):
        try:
            a = json.loads(a or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
    return a if isinstance(a, dict) else {}


def _name(tc: dict) -> str:
    return str((tc.get("function") or tc).get("name") or "")


@dataclass
class Event:
    """One tool call and its result."""
    name: str
    args: dict
    result: str
    ok: bool
    call_idx: int          # index of the assistant message that made the call
    result_idx: int        # index of the tool message holding the result (-1: none)

    @property
    def key(self) -> str:
        return self.name + "|" + json.dumps(self.args, sort_keys=True)[:4000]


_ERR_RE = re.compile(
    r"Command exited with code [1-9]|\"success\"\s*:\s*false|\"isError\"\s*:\s*true|"
    r"\"ok\"\s*:\s*false|could not be observed|could not be captured|"
    r"Editor operation failed|No replacement performed|is not recognized as (?:the )?name|"
    r"ParserError")
# Deliberately narrow: only markers the CLIENT / MCP server put on a failed
# call. Words like FAIL or Traceback also appear inside file contents and test
# output the model merely read.


def is_error(text: str) -> bool:
    return bool(_ERR_RE.search(text or ""))


def events(messages: list[dict]) -> list[Event]:
    """Tool calls paired with their results, in order. Results pair by
    tool_call_id, else by position after the calling assistant message."""
    out: list[Event] = []
    pending: dict[str, Event] = {}
    order: list[Event] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            order = []
            for tc in m["tool_calls"]:
                ev = Event(_name(tc), _args(tc), "", True, i, -1)
                out.append(ev)
                order.append(ev)
                if tc.get("id"):
                    pending[tc["id"]] = ev
        elif role == "tool":
            ev = pending.pop(m.get("tool_call_id"), None) if m.get("tool_call_id") else None
            if ev is None:
                ev = next((e for e in order if e.result_idx < 0), None)
            if ev is not None:
                ev.result = _text(m.get("content"))
                ev.ok = not is_error(ev.result)
                ev.result_idx = i
    return out


def tail_start(messages: list[dict]) -> int:
    """Index just after the last assistant message (the new part of this turn)."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return i + 1
    return 0


def _append(m: dict, note: str) -> dict:
    return {**m, "content": (_text(m.get("content")).rstrip() + "\n\n" + note).strip()}


# ---- D3: repeated-read dedupe -------------------------------------------------

_MIN_DEDUPE_CHARS = 400


def dedupe_reads(messages: list[dict]) -> tuple[list[dict], int]:
    """A read_files result whose text is identical to an earlier read_files
    result becomes a short pointer to it. Returns (messages, n replaced)."""
    evs = {e.result_idx: e for e in events(messages) if e.result_idx >= 0}
    seen: dict[str, int] = {}
    out, n = list(messages), 0
    for i, m in enumerate(messages):
        ev = evs.get(i)
        if not ev or ev.name != "read_files":
            continue
        body = _text(m.get("content"))
        if len(body) < _MIN_DEDUPE_CHARS or body.startswith(NOTE_TAG):
            continue
        h = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()
        if h in seen:
            files = ", ".join(str(f.get("path") if isinstance(f, dict) else f)
                              for f in (ev.args.get("files") or ev.args.get("paths") or []))
            out[i] = {**m, "content": (
                f"{NOTE_TAG}: identical to the read_files result at message {seen[h]} "
                f"({files or 'same files'}) — unchanged since then; use that earlier copy. "
                f"Re-read only after an edit.]")}
            n += 1
        else:
            seen[h] = i
    return out, n


# ---- D5: Roblox Studio result cleanup -----------------------------------------

_ASSIST_PREFIX = re.compile(r"(?:sa)?builtin_Assistant\.rbxm\.[\w.]+:\d+:\s*")
_ASSIST_CMD = re.compile(r"\bAssistantCommand:(\d+):")
_STALE = "is not connected"


def clean_roblox(messages: list[dict]) -> tuple[list[dict], int]:
    """Studio's execute_luau wraps errors in the Assistant plugin's own stack
    ("…ExecuteLuauTool:86: …CommandExecution:54: AssistantCommand:14: <err>").
    The error is real — it's in the submitted code — so keep it, drop the
    wrapper and name the line. A stale studio_id gets a one-line recovery hint."""
    out, n = list(messages), 0
    evs = {e.result_idx: e for e in events(messages) if e.result_idx >= 0}
    for i, m in enumerate(messages):
        ev = evs.get(i)
        if not ev or "__" not in ev.name or not ev.name.lower().startswith(("roblox", "robloxstudio")):
            continue
        body = _text(m.get("content"))
        new = body
        if "builtin_Assistant" in new:
            new = _ASSIST_PREFIX.sub("", new)
            new = _ASSIST_CMD.sub(r"your Luau code, line \1:", new)
        if _STALE in new and "studio_id" in new and NOTE_TAG not in new:
            new = (new.rstrip() + f"\n\n{NOTE_TAG}: call list_roblox_studios once and "
                   f"continue with the studio_id it returns.]")
        if new != body:
            out[i] = {**m, "content": new}
            n += 1
    return out, n


# ---- D4: screenshot pruning ---------------------------------------------------

def prune_images(messages: list[dict], keep: int) -> tuple[list[dict], int]:
    """Keep images only on the newest `keep` image-bearing messages."""
    if keep <= 0:
        return messages, 0
    idx = [i for i, m in enumerate(messages) if m.get("images")]
    drop = idx[:-keep] if len(idx) > keep else []
    if not drop:
        return messages, 0
    out = list(messages)
    for i in drop:
        m = {k: v for k, v in messages[i].items() if k != "images"}
        out[i] = _append(m, f"{NOTE_TAG}: screenshot removed from context; "
                            f"the newest {keep} are kept.]")
    return out, len(drop)


# ---- D1: adaptive thinking level ----------------------------------------------

_EDIT_TOOLS = ("editor", "write_to_file", "replace_in_file", "apply_diff", "multi_edit")


def adaptive_think(messages: list[dict], levels: dict) -> tuple[str, str]:
    """(level, why) for the next model turn, from this turn's new messages."""
    start = tail_start(messages)
    tail = messages[start:]
    if not tail:
        return levels.get("routine", "low"), "no new input"
    if any(m.get("role") == "user" and NOTE_TAG not in _text(m.get("content"))[:200]
           for m in tail):
        return levels.get("after_failure", "high"), "new user message"
    texts = [_text(m.get("content")) for m in tail if m.get("role") == "tool"]
    if any(SUPERVISOR_TAG in t for t in texts):
        return levels.get("after_failure", "high"), "supervisor note"
    if any(is_error(t) for t in texts):
        return levels.get("after_failure", "high"), "tool failure"
    prev = messages[start - 1] if start else {}
    names = [_name(tc) for tc in prev.get("tool_calls") or []]
    if any(n.split("__")[-1] in _EDIT_TOOLS for n in names):
        return levels.get("after_edit", "medium"), "after an edit"
    return levels.get("routine", "low"), "routine result"


# ---- D2: tool-call pre-judge + PowerShell fixer --------------------------------

@dataclass
class Judgement:
    calls: list                       # calls to hand to the client (possibly rewritten)
    blocked: list = field(default_factory=list)   # (call, rule, advice)
    fixes: list = field(default_factory=list)     # (before, after)


# Commands that must never run unasked. (pattern, rule, advice)
BLOCK_RULES = [
    (r"\bgit\s+push\b[^\n;|&]*\s(--force\b|-f\b|--force-with-lease\b)", "force push",
     "push without --force; if history must be rewritten, ask the user first"),
    (r"\bgit\s+push\b[^\n;|&]*\s--tags\b", "push all tags",
     "push the one tag you created: git push origin <tag>"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard",
     "use git stash or git restore <file>; ask the user before discarding work"),
    (r"--no-verify\b", "skipping git hooks",
     "run the commit normally and fix what the hook reports"),
    (r"\bgit\s+add\s+(-A\b|--all\b|\.(\s|$))", "staging everything",
     "stage the files you changed by name: git add <path> …"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean -f", "delete specific files instead, after asking"),
    (r"(Set-Content|Out-File|Add-Content)\b[^\n|]*\.(md|lua|luau|json)\b", "rewriting a tracked text file from the shell",
     "use the editor tool (PowerShell writes change the encoding / line endings)"),
    (r"-NoNewline\b", "-NoNewline write", "use the editor tool"),
    (r"\b(rm|Remove-Item)\b[^\n;|]*\s(-r\b|-rf\b|-Recurse\b)", "recursive delete outside tmp/",
     "delete specific files, or ask the user"),
    (r"(\.ssh[\\/]|id_rsa|\.aws[\\/]credentials|secrets\.json|\.env\b)", "reading credentials",
     "credentials are off limits; ask the user"),
]
_BLOCK = [(re.compile(p, re.I), r, a) for p, r, a in BLOCK_RULES]
_TMP_RX = re.compile(r"(?<![\w.-])tmp(?:[\\/]|\b)", re.I)   # scratch space is fair game

# Predictable PowerShell mistakes: (pattern, replacement, description). Only
# whole-command-safe rewrites; anything subtler is left alone.
FIX_RULES = [
    (r"(?<![\w.-])ls\s+-la?\b", "Get-ChildItem -Force", "ls -la → Get-ChildItem -Force"),
    (r"(?<![\w.-])tail\s+-n\s*(\d+)\b", r"Select-Object -Last \1", "tail -n N → Select-Object -Last N"),
    (r"(?<![\w.-])head\s+-n\s*(\d+)\b", r"Select-Object -First \1", "head -n N → Select-Object -First N"),
    (r"(?<![\w.\\/-])npx(?=\s)", "npx.cmd", "npx → npx.cmd (script execution policy)"),
    (r"(?<![\w.\\/-])npm(?=\s)", "npm.cmd", "npm → npm.cmd (script execution policy)"),
]
_FIX = [(re.compile(p), r, d) for p, r, d in FIX_RULES]
_CMD_KEYS = ("commands", "command", "cmd")


def _commands(args: dict) -> tuple[Optional[str], list]:
    for k in _CMD_KEYS:
        v = args.get(k)
        if isinstance(v, list):
            return k, [str(c) for c in v]
        if isinstance(v, str):
            return k, [v]
    return None, []


def prejudge(calls: list, cfg: dict) -> Judgement:
    """Apply the block and fix rules to one model turn's client tool calls.
    `calls` are Foundry-internal ({"id","name","arguments"}). In observe mode
    the verdicts are reported but nothing changes."""
    enforce = cfg.get("prejudge") == "enforce"
    max_edit = int(cfg.get("prejudge_max_edit_chars") or 0)
    j = Judgement(calls=[])
    for tc in calls:
        name = str(tc.get("name") or "")
        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
        key, cmds = _commands(args)
        hit = None
        for c in cmds:
            for rx, rule, advice in _BLOCK:
                if rx.search(c):
                    if rule.startswith("recursive delete") and _TMP_RX.search(c):
                        continue
                    hit = (rule, advice)
                    break
            if hit:
                break
        if not hit and name == "editor" and max_edit:
            nt = args.get("new_text") or args.get("content") or ""
            if isinstance(nt, str) and len(nt) > max_edit and args.get("old_text"):
                hit = (f"editor replacement of {len(nt):,} chars",
                       f"split it into edits under {max_edit:,} chars")
        if not hit and name == "read_files":
            for f in args.get("files") or []:
                p = f.get("path") if isinstance(f, dict) else f
                for rx, rule, advice in _BLOCK[-1:]:
                    if rx.search(str(p or "")):
                        hit = (rule, advice)
        if hit:
            j.blocked.append((tc, hit[0], hit[1]))
            if enforce:
                continue
        if key and cmds:
            new_cmds = []
            for c in cmds:
                nc = c
                for rx, rep, desc in _FIX:
                    nc2 = rx.sub(rep, nc)
                    if nc2 != nc:
                        j.fixes.append((nc, nc2, desc))
                        nc = nc2
                new_cmds.append(nc)
            if enforce and new_cmds != cmds:
                val = new_cmds if isinstance(args.get(key), list) else new_cmds[0]
                tc = {**tc, "arguments": {**args, key: val}}
        j.calls.append(tc)
    return j


def blocked_result(rule: str, advice: str) -> str:
    return (f"{NOTE_TAG} pre-judge] blocked before it ran: {rule}. Instead: {advice}. "
            f"Do not retry the same call.")


# ---- D6: turn supervisor -------------------------------------------------------

SUPERVISOR_TAG = "[Foundry supervisor]"
_PLAY_RX = re.compile(r"(solo_playtest|multiplayer_playtest|start_stop_play)$")


@dataclass
class Finding:
    at: int            # message index of the tool result where it showed
    kind: str
    detail: str


def _detect(evs: list[Event]) -> list[Finding]:
    """Walk the events once, in order, reporting each problem at the result
    where it first becomes true (re-armed after the pattern breaks)."""
    out: list[Finding] = []
    same_run: dict[str, int] = {}
    last_key = last_res = None
    run = 0
    err_streak = 0
    recent_ok: list[bool] = []
    edit_fail: dict[str, int] = {}
    play: list[int] = []
    sleeps = 0
    not_obs: dict[str, int] = {}
    last_streak = -99
    for n, e in enumerate(evs):
        if e.result_idx < 0:
            continue
        at = e.result_idx
        # 1. identical call + identical result, three times in a row
        res_h = hashlib.sha1(e.result.encode("utf-8", "replace")).hexdigest()
        if e.key == last_key and res_h == last_res:
            run += 1
        else:
            run = 1
        last_key, last_res = e.key, res_h
        if run == 3:
            out.append(Finding(at, "loop", f"you ran {e.name} with the same arguments 3 times "
                                            f"and got the same result each time"))
        # 2. error streak
        err_streak = 0 if e.ok else err_streak + 1
        recent_ok = (recent_ok + [e.ok])[-8:]
        if (err_streak == 5 or recent_ok.count(False) >= 6) and n - last_streak >= 8:
            last_streak = n
            out.append(Finding(at, "errors", f"{recent_ok.count(False)} of your last "
                                              f"{len(recent_ok)} tool calls failed"))
        # 3. the same file failing editor twice
        if e.name == "editor" and not e.ok:
            p = str(e.args.get("path") or "")
            edit_fail[p] = edit_fail.get(p, 0) + 1
            if edit_fail[p] == 2:
                out.append(Finding(at, "edit", f"two editor edits to {p} failed; re-read the "
                                               f"exact lines before the next edit"))
        elif e.name == "editor":
            edit_fail.pop(str(e.args.get("path") or ""), None)
        # 4. playtest start/stop cycling
        if _PLAY_RX.search(e.name):
            play.append(n)
        play = [x for x in play if x > n - 40]
        if len(play) == 8:
            out.append(Finding(at, "cycling", "8 playtest start/stop calls in the last 40 "
                                              "tool calls without a code change between "
                                              "explaining the failure"))
            play = []
        # 5. sleep-polling
        _, cmds = _commands(e.args)
        if any(re.search(r"Start-Sleep|\bsleep\s+\d", c, re.I) for c in cmds):
            sleeps += 1
            if sleeps == 4:
                out.append(Finding(at, "polling", "4 commands in a row wait with Start-Sleep; "
                                                  "redirect the job to a log file and read it"))
        elif cmds:
            sleeps = 0
        # 6. re-running a command whose output could not be captured
        if "could not be observed" in e.result or "could not be captured" in e.result:
            not_obs[e.key] = not_obs.get(e.key, 0) + 1
            if not_obs[e.key] == 2:
                out.append(Finding(at, "capture", "this command's output could not be captured "
                                                  "twice; redirect it to a log file and read "
                                                  "the log instead of re-running it"))
    return out


def _note_for(f: Finding, level: int) -> str:
    base = f"{SUPERVISOR_TAG} {f.detail}. Do not repeat it."
    if level >= 2:
        base += (" Stop and write a STUCK note in the plan: what you tried, what you saw, "
                 "and ONE different approach. Then do that approach.")
    return base


@dataclass
class Supervision:
    messages: list
    findings: list            # all findings in this conversation
    new: list                 # findings in this turn's new messages
    level: int                # escalation level for this turn (0 = nothing new)


def supervise(messages: list[dict], mode: str) -> Supervision:
    """Run the detectors over the whole history. In nudge / escalate mode each
    finding's note is appended to the tool result where it showed — the same
    notes at the same places every turn (cache rule). Escalation level counts
    findings since the last message the user typed."""
    if mode == "off":
        return Supervision(messages, [], [], 0)
    found = _detect(events(messages))
    start = tail_start(messages)
    last_user = max((i for i, m in enumerate(messages)
                     if m.get("role") == "user" and NOTE_TAG not in _text(m.get("content"))[:200]),
                    default=-1)
    out = list(messages)
    since_user = 0
    for f in found:
        since_user = since_user + 1 if f.at > last_user else 0
        if mode in ("nudge", "escalate") and SUPERVISOR_TAG not in _text(out[f.at].get("content")):
            out[f.at] = _append(out[f.at], _note_for(f, max(since_user, 1)))
    new = [f for f in found if f.at >= start]
    return Supervision(out, found, new, since_user if new else 0)


def blocked_reply(findings: list, tried: int) -> str:
    """The final text returned to the client when escalation is exhausted."""
    what = "; ".join(f.detail for f in findings[-3:]) or "repeated failures"
    return (f"BLOCKED by Foundry supervisor: {what}. Tried: {tried} supervisor notes "
            f"and the advisor. Needs: a decision from you — change the approach, "
            f"give a hint, or stop this task.")


# ---- one call for the request path ---------------------------------------------

@dataclass
class Prepared:
    messages: list
    think: Optional[str] = None
    think_why: str = ""
    notes: list = field(default_factory=list)
    supervision: Optional[Supervision] = None


def prepare(messages: list[dict], cfg: dict) -> Prepared:
    """All rule-based history transforms + the turn's think level."""
    notes: list[str] = []
    out = messages
    if cfg.get("roblox_cleanup"):
        out, n = clean_roblox(out)
    if cfg.get("dedupe_reads"):
        out, n = dedupe_reads(out)
        start = tail_start(out)
        if any(_text(m.get("content")).startswith(NOTE_TAG + ": identical") for m in out[start:]):
            notes.append("✂️ repeated read replaced by a pointer to the earlier copy")
    keep = int(cfg.get("image_keep") or 0)
    if keep:
        out, n = prune_images(out, keep)
    sup = supervise(out, cfg.get("supervisor") or "off")
    out = sup.messages
    for f in sup.new:
        notes.append(f"🛑 supervisor ({cfg.get('supervisor')}): {f.kind} — {f.detail}")
    p = Prepared(out, notes=notes, supervision=sup)
    if cfg.get("think_policy") == "adaptive":
        p.think, p.think_why = adaptive_think(out, cfg.get("think_levels") or {})
        if sup.new and cfg.get("supervisor") in ("nudge", "escalate"):
            p.think = (cfg.get("think_levels") or {}).get("after_failure", "high")
            p.think_why = "supervisor note"
    return p
