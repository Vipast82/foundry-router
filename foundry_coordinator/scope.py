"""Typed scope requests and the harness allow-list (revamp B10).

A sprint's file list is written before the sprint. Some changes a legitimate
implementation forces are harness lines nobody can list in advance: the
GateRunner EXPECTED_BOOT / MOUNTED_TESTS rows, the sprint's gate manifest, the
Rojo mapping of a new file the plan lists. Before this module such a line was a
"scope violation" that blocked the sprint and asked a person (UI-02b: ~10 h on a
one-line EXPECTED_BOOT edit).

The implementer asks through a typed channel, in its final message:

    ```SCOPE_REQUEST
    {"path": "tests/GateRunner.lua", "reason": "EXPECTED_BOOT client 22 -> 23 for the new HUD"}
    ```

(one object or a list; `"scope_requests": [...]` inside the coordinator-report
works too). The coordinator then, deterministically:

  * a path the [automation] scope_auto_approve allow-list covers: adds it to the
    task's file list in the task source (the sprint plan), commits that with the
    task prefix, journals the grant and continues the SAME session;
  * a path no grant may ever cover (the Kilo config, git internals, hook folders,
    secrets): denied by rule at once, no decision;
  * anything else: opens a `scope_request` decision with a recommended answer
    ([automation] scope_unlisted_default) and the editable timeout (B11).

2026-10-27 rules streamline: the session is always told the answer in the same
session, with the reason (granted by rule X; denied by rule; denied by the owner;
denied after the decision timeout; could not be added), never left silently with a
deny. A grant of a docs/memory path keeps done_check's SCOPE row green; the memory
files stay the coordinator's (MEMORY_NOTE), never the session's to edit.

A grant may carry constraints on the change itself (`blocks`, `lines`,
`json_paths_in_plan`): verification checks the final diff of the granted file
against them (`grant_checks`), so an allow-listed harness file can only change
the way the rule says.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Optional

from .adapters.base import safe_rel
from .automation import get as auto_get

FENCE = "SCOPE_REQUEST"
_FENCE_RE = re.compile(r"```" + FENCE + r"\s*\n(.*?)```", re.S)
DECISION_KIND = "scope_request"
APPROVE, DENY = "approve", "deny"


# ------------------------------------------------------------------ parsing

def _items(d) -> list:
    if isinstance(d, dict):
        return [d]
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    return []


def parse_requests(text: str = "", report: Optional[dict] = None) -> list[dict]:
    """[{path, reason}] from SCOPE_REQUEST blocks in `text` and the report's
    `scope_requests`. Paths are workspace-relative (anything else is dropped)."""
    raw = []
    for m in _FENCE_RE.finditer(text or ""):
        try:
            raw += _items(json.loads(m.group(1)))
        except ValueError:
            continue
    raw += _items((report or {}).get("scope_requests"))
    out, seen = [], set()
    for r in raw:
        p = safe_rel(str(r.get("path") or ""))
        if not p or p in seen:
            continue
        seen.add(p)
        out.append({"path": p, "reason": str(r.get("reason") or "").strip()[:300]})
    return out[:10]


# ------------------------------------------------------------------ policy

def rule_pattern(r: dict, task_id: str) -> str:
    """An allow-list rule's path for this task: {task} = the id, {task_} = the id with
    "_" for "-" (check files: tests/checks/HK_STORE1_check.lua)."""
    return (str((r or {}).get("path") or "").replace("{task_}", task_id.replace("-", "_"))
            .replace("{task}", task_id))


def rule_for(P, task_id: str, path: str) -> Optional[dict]:
    """The allow-list rule that covers `path` for this task, or None."""
    for r in auto_get(P, "scope_auto_approve") or []:
        pat = rule_pattern(r, task_id)
        if pat and (fnmatch.fnmatchcase(path, pat) or path == pat):
            return r
    return None


# paths no scope grant may cover: the session's tool config, git internals, hooks and
# secrets (role_rules.CONFIG_FILES / the secret patterns deny them anyway)
NEVER_GRANT = ("kilo.jsonc", "kilo.json", ".kilo/*", ".git/*", "tools/hooks/*", ".githooks/*",
               ".husky/*", ".env", ".env.*", "*.env", "*_secrets*", "*secret*", "*credential*")


def never_grant(path: str) -> str:
    """The rule that forbids granting `path` ('' = none)."""
    low = path.lower()
    for pat in NEVER_GRANT:
        if fnmatch.fnmatchcase(low, pat) or fnmatch.fnmatchcase(low.rsplit("/", 1)[-1], pat):
            return pat
    return ""


def _is_memory(path: str) -> bool:
    from .memory.layout import MEMORY_DIR
    return path.startswith(MEMORY_DIR.rstrip("/") + "/")


def _why_denied(d: dict) -> str:
    actor = str(d.get("actor") or "")
    auth = str(d.get("authority") or "")
    if actor == "timeout_default" or auth == "timeout_default":
        return ("nobody answered within the decision timeout, so the default "
                "(scope_unlisted_default: deny) applied")
    if auth == "delegated_policy" or actor.startswith("policy"):
        return f"denied by policy ({actor or auth})"
    return f"denied by {actor or 'the operator'}"


def grants(t: dict) -> list[dict]:
    return list(((t or {}).get("data") or {}).get("scope_grants") or [])


# ------------------------------------------------------------------ the flow

def _edits_allow_listed(P, spec) -> bool:
    """Do this project's session rules deny edits by default (an allow-list built from the
    plan's file list when the session starts)? Then a granted file needs a fresh session."""
    try:
        rules = P.adapter.session_permissions(spec, "implement") or []
    except Exception:                                             # noqa: BLE001
        return False
    return any(r.get("permission") == "edit" and r.get("pattern") in ("*", "**")
               and r.get("action") == "deny" for r in rules if isinstance(r, dict))


def handle(c, P, t: dict, spec, reqs: list[dict]) -> Optional[str]:
    """Process the scope requests of an idle primary. Returns:
      "continued"  every request is granted (or already in scope); the same
                   session was prompted to continue
      "waiting"    a decision is open for at least one path
      None         nothing to do here (requests denied / ungrantable): the
                   caller handles the report as before"""
    pid, tid = P.id, t["task_id"]
    from .profile import matches_any
    granted, pending, denied, failed = [], [], [], []
    for r in reqs:
        path = r["path"]
        if matches_any(path, list(spec.scope)):
            granted.append({**r, "rule": "already in scope"})
            continue
        forbidden = never_grant(path)
        if forbidden:
            denied.append({**r, "decision_id": "", "why": f"denied by rule: {path} matches "
                           f"{forbidden!r}, which no scope grant may cover"})
            continue
        rule = rule_for(P, tid, path)
        if rule is not None:
            g = _grant(c, P, t, spec, path, r["reason"], rule.get("name") or rule["path"],
                       rule, actor="policy:scope_auto_approve")
            (granted if g else failed).append({**r, "rule": rule.get("name"),
                                               **({"commit": g} if g else {}),
                                               **({} if g else {"why": "the plan's file list "
                                                   "could not be edited"})})
            continue
        d = _decision(c, P, t, r)
        if d is None:
            failed.append({**r, "rule": "no decisions table",
                           "why": "no decision could be opened for it"})
        elif d["status"] == "open":
            pending.append({**r, "decision_id": d["decision_id"]})
        elif d["status"] == "answered" and _approved(d):
            g = _grant(c, P, t, spec, path, r["reason"], "decision " + d["decision_id"], {},
                       actor=str(d.get("actor") or "operator"))
            (granted if g else failed).append({**r, "rule": "decision", "decision_id":
                                               d["decision_id"],
                                               **({} if g else {"why": "approved, but the "
                                                   "plan's file list could not be edited"})})
        else:
            denied.append({**r, "decision_id": d["decision_id"], "why": _why_denied(d)})
    if pending:
        c._note(P, t, "waiting for scope decision(s): " + ", ".join(
            f"{x['decision_id']} ({x['path']})" for x in pending))
        return "waiting"
    if denied or failed or not granted:
        if denied or failed:
            # told once, with the reason; a repeat of the same request is then an
            # ordinary stop
            lines = [f"{x['path']}: {x.get('why') or 'denied'}"
                     + (f" ({x['decision_id']})" if x.get("decision_id") else "")
                     for x in denied + failed]
            ok = ("; ".join(f"{x['path']} ({x['rule']})" for x in granted) if granted else "")
            text = ("Answer to your scope request(s): NOT granted: " + "; ".join(lines) + "."
                    + (f" Granted: {ok}." if ok else "")
                    + " Do not edit the refused file(s). Finish the sprint inside the approved "
                      "file list, or report BLOCKED (`product`, with options) if the sprint "
                      "cannot be done without them.")
            if c._prompt_primary(P, t, text, "scope-denied:" + ",".join(
                    sorted(x["path"] for x in denied + failed))):
                c.store.event(pid, tid, "scope_denied", session_id=t.get("session_id"),
                              detail={"paths": [x["path"] for x in denied + failed],
                                      "why": [x.get("why") or "" for x in denied + failed]})
                return "continued"
        return None
    text = ("The coordinator added these paths to this sprint's file list (scope grant, "
            "committed in the plan): " + "; ".join(
                f"{x['path']} ({x['rule']})" for x in granted)
            + ". Continue the sprint; change them only as the request said "
              "(allow-listed harness files are checked against their rule at verification).")
    if any(_is_memory(x["path"]) for x in granted):
        text += (" docs/memory is the coordinator's project memory: the grant keeps "
                 "done_check's SCOPE row green, but never edit or revert those files; record "
                 "facts with MEMORY_NOTE lines in your final message.")
    key = "scope-grant:" + ",".join(sorted(x["path"] for x in granted))
    new_files = [x for x in granted if x.get("rule") != "already in scope"]
    sid = t.get("session_id") or ""
    if new_files and sid and callable(getattr(c, "_session_failed", None)) and \
            _edits_allow_listed(P, spec):
        # 2026-10-28 AS-P5: a session's edit permissions are fixed when it starts (from the
        # plan's file list), so a granted file stayed denied ("Session rule {permission:
        # edit, pattern: *, action: deny}") and the session reported BLOCKED. A fresh
        # session starts with the granted files; no attempt is charged.
        cur = c.store.task(pid, tid) or t
        phase = "closeout" if cur.get("state") == "CLOSING" else "implement"
        try:
            c._end_session(P, sid, "scope_grant")
        except Exception:                                         # noqa: BLE001
            pass
        c._session_failed(P, cur, text + " (A fresh session: the granted files are in its "
                          "edit permissions.)", phase, free=True)
        c.store.event(pid, tid, "scope_continued", session_id=sid,
                      detail={"granted": granted, "fresh_session": True})
        return "continued"
    if not c._prompt_primary(P, t, text, key):
        return None
    c.store.event(pid, tid, "scope_continued", session_id=t.get("session_id"),
                  detail={"granted": granted})
    return "continued"


def _approved(d: dict) -> bool:
    a = d.get("answer")
    if isinstance(a, dict):
        a = a.get("option") or a.get("answer") or a.get("decision") or ""
    return str(a or "").strip().lower().startswith(APPROVE)


def _decision(c, P, t: dict, r: dict) -> Optional[dict]:
    try:
        O = c.obligations
    except Exception:                                            # noqa: BLE001
        return None
    rec = auto_get(P, "scope_unlisted_default")
    q = (f"{t['task_id']} asks to change {r['path']}, which is outside the sprint's file list "
         f"and not on the harness allow-list. Reason given: {r['reason'] or '(none)'}")
    did = O.open_decision(
        P.id, t["task_id"], DECISION_KIND, q,
        evidence={"path": r["path"], "reason": r["reason"], "session_id": t.get("session_id")},
        options=[f"{APPROVE}: add {r['path']} to the file list",
                 f"{DENY}: keep the file list"],
        recommendation=f"{rec} (the [automation] scope_unlisted_default)",
        blocks=[f"scope:{r['path']}"])
    return O.decision(did)


def _grant(c, P, t: dict, spec, path: str, reason: str, rule_name: str, rule: dict,
           actor: str) -> str:
    """Add `path` to the task source's file list and commit it. Returns the
    commit sha ('' when the adapter can't, or the commit failed)."""
    pid, tid = P.id, t["task_id"]
    fn = getattr(P.adapter, "grant_scope", None)
    if not callable(fn):
        return ""
    try:
        rel = fn(spec, path, f"{reason or 'harness line'} (coordinator scope grant: "
                             f"{rule_name})")
    except Exception as e:                                       # noqa: BLE001
        c.store.event(pid, tid, "scope_grant_failed", detail={"path": path,
                                                              "error": f"{e}"[:300]})
        return ""
    if not rel:
        return ""
    prefix = P.profile.commit.get("prefix", "{task}").replace("{task}", tid)
    sep = "" if prefix.endswith((":", ": ")) else ":"
    from . import bookkeeping as BK
    try:
        # audit D: the project's hook env (AU_ALLOW_SCOPE) like every coordinator commit;
        # a refused commit is unstaged by commit_paths
        sha = P.ws.commit_paths([rel], f"{prefix}{sep} scope grant {path} ({rule_name})",
                                env=BK.commit_env(P, [rel]))
        if not sha:
            raise RuntimeError(f"nothing to commit for {rel}")
    except Exception as e:                                       # noqa: BLE001
        try:
            P.ws.run("checkout", "--", rel, check=False)
        except Exception:                                        # noqa: BLE001
            pass
        c.store.event(pid, tid, "scope_grant_failed", detail={"path": path, "file": rel,
                                                              "error": f"{e}"[:300]})
        return ""
    g = {"path": path, "reason": reason, "rule": rule_name, "actor": actor, "commit": sha,
         "file": rel, "ts": c.clock(),
         "constraints": {k: rule[k] for k in ("blocks", "lines", "json_paths_in_plan")
                         if rule.get(k)}}
    cur = c.store.task(pid, tid) or t
    c.store.merge_data(pid, tid, scope_grants=grants(cur) + [g])
    c.store.event(pid, tid, "scope_granted", session_id=t.get("session_id"), detail=g)
    return sha


# ------------------------------------------------------------------ verification

def auto_grant_changed(c, P, t: dict, spec, path: str) -> dict:
    """W2.5 / W1: a file the session changed WITHOUT asking that the allow-list
    covers is granted after the fact (the task source gets the path when the
    adapter can edit it; otherwise the grant is journaled alone), so the scope
    check never fails on it and grant_checks still verifies the change against
    its rule. Idempotent per path. Returns the grant record."""
    pid, tid = P.id, t["task_id"]
    for g in grants(c.store.task(pid, tid) or t):
        if g.get("path") == path:
            return g
    rule = rule_for(P, tid, path) or {}
    name = rule.get("name") or rule.get("path") or "allow-list"
    reason = "changed by the session; covered by the allow-list (granted at verification)"
    sha = _grant(c, P, t, spec, path, reason, name, rule, actor="policy:scope_auto_approve")
    cur = c.store.task(pid, tid) or t
    if any(g.get("path") == path for g in grants(cur)):
        return next(g for g in grants(cur) if g.get("path") == path)
    g = {"path": path, "reason": reason, "rule": name, "actor": "policy:scope_auto_approve",
         "commit": sha, "file": "", "ts": c.clock(),
         "constraints": {k: rule[k] for k in ("blocks", "lines", "json_paths_in_plan")
                         if rule.get(k)}}
    c.store.merge_data(pid, tid, scope_grants=grants(cur) + [g])
    c.store.event(pid, tid, "scope_granted", session_id=t.get("session_id"),
                  detail={**g, "note": "the task source was not edited (no JSON task file)"})
    return g


def grant_checks(P, t: dict, spec, base: str, head: str) -> list:
    """coordinator.scope_grant results: each granted file with constraints
    changed only the way its rule allows (base..head)."""
    from .adapters.base import FAIL, PASS, VerificationResult
    out = []
    for g in grants(t):
        cons = g.get("constraints") or {}
        if not cons:
            continue
        path = g["path"]
        try:
            ranges = _own_ranges(P, t, path, base, head)
            problems = []
            for a, b in ranges:
                problems += check_change(P.ws, path, a, b, cons, list(spec.scope))
        except Exception as e:                                   # noqa: BLE001
            problems = [f"could not read the change: {e}"]
        out.append(VerificationResult(
            check="coordinator.scope_grant", result=FAIL if problems else PASS, required=True,
            detail=(f"{path} changed outside its grant ({g.get('rule')}): "
                    + "; ".join(problems[:6])) if problems else
            f"{path} changed only as its grant allows ({g.get('rule')})",
            revision=head, evidence=f"git diff {base[:12]}..{head[:12]} -- {path}"))
    return out


def _own_ranges(P, t: dict, path: str, base: str, head: str) -> list:
    """The diff ranges a grant is judged on: base..head, unless another sprint (a track
    merge, a prep landing) also changed `path` in that range; then only this sprint's own
    commits that touched it, each as (commit^, commit). 2026-10-30 AS-P7: the pt and mv2-2
    merges added their check mounts to default.project.json after AS-P7's base, and its
    rojo_mapping grant failed on their lines ("tests/PT_1_check.lua is not a file the plan
    lists"), sending a finished sprint back until its session budget ran out."""
    from . import bookkeeping as BK
    tid = str(t.get("task_id") or "")
    try:
        subj = dict(P.ws.commits(base, head))
        per = P.ws.commit_files(base, head)
    except Exception:                                             # noqa: BLE001
        return [(base, head)]
    norm = path.replace("\\", "/")
    touching = [sha for sha, files in per
                if norm in {f.replace("\\", "/") for f in files}]
    foreign = [sha for sha in touching if BK.other_sprint(subj.get(sha, ""), tid)]
    if not foreign:
        return [(base, head)]
    return [(f"{sha}^", sha) for sha in touching if sha not in foreign]


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_lines(ws, path: str, base: str, head: str) -> list[tuple[str, int, str]]:
    """[(sign, line number in its version, text)] of `git diff -U0 base head -- path`."""
    diff = ws.run("diff", "-U0", "--no-color", base, head, "--", path, check=False) or ""
    out, old_no, new_no = [], 0, 0
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            continue
        if line.startswith(("---", "+++", "diff ", "index ")):
            continue
        if line.startswith("-"):
            out.append(("-", old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            out.append(("+", new_no, line[1:]))
            new_no += 1
    return out


def block_ranges(text: str, names: list[str]) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of `NAME = {...}` assignments (or one-line
    `NAME.x = v` / `NAME["x"] = v` assignments) for the given names."""
    lines = (text or "").splitlines()
    out = []
    for name in names:
        rx = re.compile(r"^\s*(?:local\s+)?(?:[A-Za-z_]\w*\.)*" + re.escape(name)
                        + r"(?:\.\w+|\[[^\]]+\])*\s*=")
        i = 0
        while i < len(lines):
            if rx.match(lines[i]):
                depth, j = 0, i
                while j < len(lines):
                    code = lines[j].split("--", 1)[0]
                    depth += code.count("{") - code.count("}")
                    if depth <= 0:
                        break
                    j += 1
                out.append((i + 1, min(j, len(lines) - 1) + 1))
                i = j + 1
            else:
                i += 1
    return out


_JSON_STRUCT = re.compile(r'^\s*(?:"[^"]+"\s*:\s*[\[{]\s*|[\]\[{},\s]*)$')
_JSON_PATH = re.compile(r'^\s*"\$path"\s*:\s*"([^"]+)"\s*,?\s*$')
_JSON_CLASS = re.compile(r'^\s*"\$className"\s*:\s*"[^"]+"\s*,?\s*$')


def check_change(ws, path: str, base: str, head: str, cons: dict,
                 plan_scope: list[str]) -> list[str]:
    """Problems with the change of a granted file against its constraints."""
    lines = changed_lines(ws, path, base, head)
    if not lines:
        return []
    problems = []
    if cons.get("blocks"):
        old = block_ranges(ws.show_at(base, path) or "", cons["blocks"])
        new = block_ranges(ws.show_at(head, path) or "", cons["blocks"])
        for sign, n, text in lines:
            ranges = old if sign == "-" else new
            if not any(a <= n <= b for a, b in ranges) and text.strip():
                problems.append(f"{sign}{n}: {text.strip()[:80]} (outside "
                                f"{'/'.join(cons['blocks'])})")
    if cons.get("lines"):
        rxs = [re.compile(x) for x in cons["lines"]]
        for sign, n, text in lines:
            if not any(r.match(text) for r in rxs):
                problems.append(f"{sign}{n}: {text.strip()[:80]} (no allowed line pattern)")
    if cons.get("json_paths_in_plan"):
        from .profile import matches_any
        scope = [s for s in plan_scope if s != path]
        for sign, n, text in lines:
            if _JSON_STRUCT.match(text) or _JSON_CLASS.match(text):
                continue
            m = _JSON_PATH.match(text)
            if m:
                target = m.group(1).replace("\\", "/").strip("/")
                if matches_any(target, scope) or any(s.startswith(target + "/") for s in scope):
                    continue
                problems.append(f"{sign}{n}: $path {target} is not a file the plan lists")
                continue
            problems.append(f"{sign}{n}: {text.strip()[:80]} (not a mapping for a plan file)")
    return problems
