"""Pending repo changes, landed by the coordinator at a safe point (self-driving part 3).

Replaces the chat-session bash loop `au_safe_point.sh`: queued commits for a project's
main checkout (a patch file, a branch, or a script that edits files) wait here and land
when nothing is running there. Entries live in the coordinator store (table
`prep_queue`), are added from the router (Automation -> Repo changes, control actions
`prep_*`) or `python -m foundry_coordinator prep add`, and land in order:

  safe point   [prep_queue] enabled; project not paused; no workspace lock on the main
               checkout; no main-checkout task in a mid-sprint state (block_states);
               no open session of a main-checkout task; no background check, scripted
               Studio run or track merge gate in the main checkout; the checkout is on
               its publish branch with no tracked changes
  batch        the pending entries from the front of the queue whose condition holds
               (`after_task` reached `state`); an unmet condition or a failed entry
               stops the batch there, so nothing lands out of order
  landing      fast-forward pull, then per entry: `git apply --index --check`, apply,
               commit through the project's hooks with the adapter's commit env
               (patch already in the tree -> `already_applied`); all or nothing: any
               refusal resets hard to the start commit, marks the batch failed with the
               hook output and records a `prep_failed` event (alert class prep_failed)
  publish      one push for the whole batch (the profile's publish policy)
  after        each landed entry's after-actions, deduplicated in queue order:
               kilo_config_apply, reload_kilo (client request), resume:<task>,
               release_hold:<task>, copy:<src>=><dest>, move_aside:<rel>, run:<command>
  holds        an entry may hold tasks until it lands (owner `prep_queue:<id>`, an
               expiry); landing (or removing the entry) releases them

Holds use the self-heal hold shape (task data `hold = {owner, reason, expires_ts}`;
`Coordinator.hold(project, task, owner, reason, minutes)` / `release_hold(project, task,
owner)`). Until selfheal.py provides them, the helpers below write the same record and,
for a task that is only QUEUED or READY, pause it (and resume it on release).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Optional

from . import states as S

KINDS = ("patch", "branch", "script")
STATUSES = ("pending", "landed", "already_applied", "failed", "removed")
DONE = ("landed", "already_applied")
OWNER = "prep_queue"
TASK = "@prep_queue"                   # the pseudo task that holds the workspace lock
AFTER_SIMPLE = ("kilo_config_apply", "reload_kilo")
AFTER_PREFIX = ("resume:", "release_hold:", "copy:", "move_aside:", "run:")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_LOG = 200
MAX_MESSAGE = 300
MAX_BODY = 8000

SCHEMA = """
CREATE TABLE IF NOT EXISTS prep_queue (
  id TEXT PRIMARY KEY, project_id TEXT, ord INTEGER, kind TEXT, source TEXT, message TEXT,
  body TEXT, condition TEXT, after_actions TEXT, hold_tasks TEXT, hold_minutes INTEGER,
  paths TEXT, command TEXT, status TEXT, sha TEXT, log TEXT, batch_id TEXT, created_by TEXT,
  created_ts REAL, updated_ts REAL, landed_ts REAL);
CREATE INDEX IF NOT EXISTS idx_prep_project ON prep_queue(project_id, ord);
"""


class PrepError(ValueError):
    pass


# --------------------------------------------------------------------------- settings

def settings(P) -> dict:
    """The project's [prep_queue] table (defaults when absent or invalid)."""
    from foundry_automation.schema import PREP_QUEUE_DEFAULTS, prep_queue_table
    try:
        return prep_queue_table("profile", (getattr(P.profile, "raw", None) or {})
                                .get("prep_queue"))
    except ValueError:
        return dict(PREP_QUEUE_DEFAULTS)


# --------------------------------------------------------------------------- store

def ensure(store) -> None:
    if getattr(store, "_prep_queue_ready", False):
        return
    with store._lock:
        store.conn.executescript(SCHEMA)
    store._prep_queue_ready = True


def _uj(v, default):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _row(r: dict) -> dict:
    r = dict(r)
    for k, d in (("condition", {}), ("after_actions", []), ("hold_tasks", []), ("paths", []),
                 ("log", [])):
        r[k] = _uj(r.get(k), d)
    return r


def get(store, entry_id: str) -> Optional[dict]:
    ensure(store)
    r = store.q("SELECT * FROM prep_queue WHERE id=?", (entry_id,))
    return _row(r[0]) if r else None


def entries(store, project_id: str, include_removed: bool = False) -> list[dict]:
    ensure(store)
    rows = store.q("SELECT * FROM prep_queue WHERE project_id=?"
                   + ("" if include_removed else " AND status!='removed'")
                   + " ORDER BY ord, created_ts", (project_id,))
    return [_row(r) for r in rows]


def _set(store, entry_id: str, **fields) -> None:
    for k in ("condition", "after_actions", "hold_tasks", "paths", "log"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], default=str)
    fields["updated_ts"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    store.x(f"UPDATE prep_queue SET {cols} WHERE id=?", (*fields.values(), entry_id))


def _log(store, entry_id: str, msg: str, clock=time.time) -> None:
    e = get(store, entry_id)
    if not e:
        return
    log = (e["log"] + [{"ts": clock(), "msg": str(msg)[:4000]}])[-MAX_LOG:]
    _set(store, entry_id, log=log)


def _renumber(store, project_id: str, ids: list[str]) -> None:
    for i, eid in enumerate(ids, 1):
        store.x("UPDATE prep_queue SET ord=? WHERE id=?", (i, eid))


def _meta(store, key: str) -> dict:
    return _uj(store.meta(key), {}) or {}


def _set_meta(store, key: str, val: dict) -> None:
    store.set_meta(key, json.dumps(val, default=str))


def _k(kind: str, pid: str) -> str:
    return f"prep_queue:{kind}:{pid}"


# --------------------------------------------------------------------------- validation

def parse_after(actions) -> list[str]:
    out = []
    for a in actions or []:
        a = str(a or "").strip()
        if not a:
            continue
        if a in AFTER_SIMPLE:
            out.append(a)
            continue
        pre = next((p for p in AFTER_PREFIX if a.startswith(p)), "")
        arg = a[len(pre):].strip()
        if not pre or not arg:
            raise PrepError(f"after-action {a!r}: use {', '.join(AFTER_SIMPLE)} or "
                            f"{', '.join(p + '<arg>' for p in AFTER_PREFIX)}")
        if pre in ("resume:", "release_hold:") and not TASK_ID_RE.match(arg):
            raise PrepError(f"after-action {a!r}: not a task id")
        if pre == "copy:" and "=>" not in arg:
            raise PrepError(f"after-action {a!r}: copy:<source dir>=><workspace-relative dest>")
        if pre in ("copy:", "move_aside:"):
            rel = arg.split("=>", 1)[-1].strip() if pre == "copy:" else arg
            if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                raise PrepError(f"after-action {a!r}: the destination must be inside the "
                                f"workspace (a relative path)")
        out.append(pre + arg)
    return out


def parse_condition(after_task: str = "", state: str = "") -> dict:
    after_task = str(after_task or "").strip()
    if not after_task:
        return {}
    if not TASK_ID_RE.match(after_task):
        raise PrepError(f"condition: {after_task!r} is not a task id")
    state = (state or S.COMPLETE).strip().upper()
    if state not in S.ALL:
        raise PrepError(f"condition state must be one of {', '.join(S.ALL)}")
    return {"after_task": after_task, "state": state}


# --------------------------------------------------------------------------- editing

def add(c, project_id: str, kind: str, message: str, *, source: str = "", body: str = "",
        condition: Optional[dict] = None, after_actions=None, hold_tasks=None,
        hold_minutes: int = 0, paths=None, command: str = "", patch_text: str = "",
        position: int = 0, actor: str = "operator") -> dict:
    """Queue one change. A pasted patch (`patch_text`) is saved under the state dir and
    becomes the entry's source. Returns the entry."""
    store = c.store
    ensure(store)
    P = c.projects.get(project_id)
    if P is None:
        raise PrepError(f"unknown project {project_id!r}")
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise PrepError(f"kind must be one of {', '.join(KINDS)}")
    message = " ".join(str(message or "").split())
    if not message:
        raise PrepError("a commit message is required")
    if len(message) > MAX_MESSAGE:
        raise PrepError(f"the commit message is over {MAX_MESSAGE} characters (use the body)")
    body = str(body or "").strip()
    if len(body) > MAX_BODY:
        raise PrepError(f"the commit body is over {MAX_BODY} characters")
    eid = f"pq-{uuid.uuid4().hex[:10]}"
    source = str(source or "").strip()
    if kind == "patch" and patch_text:
        d = os.path.join(c.state_dir, "prep-patches")
        os.makedirs(d, exist_ok=True)
        source = os.path.join(d, f"{eid}.patch")
        with open(source, "w", encoding="utf-8", newline="") as f:
            f.write(patch_text if patch_text.endswith("\n") else patch_text + "\n")
    if kind in ("patch", "branch") and not source:
        raise PrepError(f"a {kind} entry needs a source ({'patch file path or pasted patch' if kind == 'patch' else 'branch or ref'})")
    if kind == "patch" and not os.path.isfile(source):
        raise PrepError(f"patch file not found: {source}")
    command = str(command or "").strip()
    if kind == "script" and not command:
        raise PrepError("a script entry needs a command")
    paths = [str(p).replace("\\", "/").strip() for p in (paths or []) if str(p).strip()]
    for p in paths:
        if os.path.isabs(p) or ".." in p.split("/"):
            raise PrepError(f"path {p!r}: workspace-relative paths only")
    after = parse_after(after_actions)
    holds = []
    for t in hold_tasks or []:
        t = str(t).strip()
        if t and not TASK_ID_RE.match(t):
            raise PrepError(f"hold: {t!r} is not a task id")
        if t:
            holds.append(t)
    cfg = settings(P)
    minutes = int(hold_minutes or cfg["hold_minutes"])
    if minutes < 1:
        raise PrepError("hold minutes must be at least 1")
    cond = dict(condition or {})
    if cond:
        cond = parse_condition(cond.get("after_task", ""), cond.get("state", ""))
    now = c.clock()
    active = [e["id"] for e in entries(store, project_id)]
    store.x("INSERT INTO prep_queue (id, project_id, ord, kind, source, message, body, condition, "
            "after_actions, hold_tasks, hold_minutes, paths, command, status, sha, log, batch_id, "
            "created_by, created_ts, updated_ts, landed_ts) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, project_id, len(active) + 1, kind, source, message, body, json.dumps(cond),
             json.dumps(after), json.dumps(holds), minutes, json.dumps(paths), command,
             "pending", "", json.dumps([{"ts": now, "msg": f"added by {actor}"}]), "",
             str(actor)[:120], now, now, None))
    if position and 1 <= int(position) <= len(active):
        active.insert(int(position) - 1, eid)
        _renumber(store, project_id, active)
    for t in holds:
        _log(store, eid, hold(c, project_id, t, f"{OWNER}:{eid}",
                              f"waits for prep commit {eid}: {message}"[:300], minutes),
             c.clock)
    store.event(project_id, "", "prep_added", detail={"id": eid, "kind": kind,
                                                      "message": message, "by": actor})
    return get(store, eid)


def remove(c, project_id: str, entry_id: str, actor: str = "operator") -> dict:
    e = get(c.store, entry_id)
    if not e or e["project_id"] != project_id or e["status"] == "removed":
        raise PrepError(f"no prep entry {entry_id!r} in {project_id}")
    if e["status"] == "landed":
        raise PrepError(f"{entry_id} already landed as {e['sha'][:12]}; it stays in the history")
    _set(c.store, entry_id, status="removed")
    _log(c.store, entry_id, f"removed by {actor}", c.clock)
    for t in e["hold_tasks"]:
        release_hold(c, project_id, t, f"{OWNER}:{entry_id}")
    _renumber(c.store, project_id, [x["id"] for x in entries(c.store, project_id)])
    c.store.event(project_id, "", "prep_removed", detail={"id": entry_id, "by": actor})
    return get(c.store, entry_id)


def move(c, project_id: str, entry_id: str, position: int, actor: str = "operator") -> dict:
    ids = [e["id"] for e in entries(c.store, project_id)]
    if entry_id not in ids:
        raise PrepError(f"no prep entry {entry_id!r} in {project_id}")
    e = get(c.store, entry_id)
    if e["status"] in DONE:
        raise PrepError(f"{entry_id} already landed; only pending or failed entries move")
    position = max(1, min(int(position), len(ids)))
    ids.remove(entry_id)
    ids.insert(position - 1, entry_id)
    _renumber(c.store, project_id, ids)
    _log(c.store, entry_id, f"moved to position {position} by {actor}", c.clock)
    return get(c.store, entry_id)


def run_now(c, project_id: str, actor: str = "operator") -> dict:
    """Failed entries go back to pending and the next tick checks for a safe point at
    once (the check interval is skipped). The safe point and conditions still apply."""
    if project_id not in c.projects:
        raise PrepError(f"unknown project {project_id!r}")
    reset = []
    for e in entries(c.store, project_id):
        if e["status"] == "failed":
            _set(c.store, e["id"], status="pending")
            _log(c.store, e["id"], f"retry requested by {actor}", c.clock)
            reset.append(e["id"])
    _set_meta(c.store, _k("run_now", project_id), {"by": actor, "ts": c.clock()})
    c.store.event(project_id, "", "prep_run_now", detail={"by": actor, "reset": reset})
    return {"reset": reset, "requested": True}


# --------------------------------------------------------------------------- holds

def hold(c, project_id: str, task_id: str, owner: str, reason: str, minutes: int) -> str:
    t = c.store.task(project_id, task_id)
    if t is None:
        return f"hold on {task_id}: no such task (nothing held)"
    fn = getattr(c, "hold", None)
    if callable(fn):
        fn(project_id, task_id, owner, reason, minutes)
        return f"hold on {task_id} ({minutes} min)"
    paused = False
    if t["state"] in (S.QUEUED, S.READY):
        c.pause(project_id, task_id, f"hold: {reason}"[:300], actor=owner)
        paused = True
    c.store.merge_data(project_id, task_id, hold={
        "owner": owner, "reason": reason, "expires_ts": c.clock() + int(minutes) * 60,
        "paused": paused})
    return f"hold on {task_id} ({minutes} min{', paused' if paused else ''})"


def release_hold(c, project_id: str, task_id: str, owner: str) -> str:
    t = c.store.task(project_id, task_id)
    h = ((t or {}).get("data") or {}).get("hold") or {}
    if not t or not isinstance(h, dict) or not str(h.get("owner") or "") == owner:
        return f"no hold of {owner} on {task_id}"     # never someone else's hold
    fn = getattr(c, "release_hold", None)
    if callable(fn):
        fn(project_id, task_id, owner)
        return f"released the hold on {task_id}"
    c.store.merge_data(project_id, task_id, hold=None)
    if h.get("paused") and t["state"] == S.PAUSED:
        c.resume(project_id, task_id, actor=owner, note="prep queue hold released")
        return f"released the hold on {task_id} and resumed it"
    return f"released the hold on {task_id}"


# --------------------------------------------------------------------------- git

def _git(ws: str, *args, env: Optional[dict] = None, timeout: int = 300,
         stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", **(env or {}))
    try:
        return subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=e,
                              input=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {' '.join(args)} timed out "
                                                          f"after {timeout}s")


def _out(r) -> str:
    return ((r.stdout or "") + ("\n" if r.stdout and r.stderr else "") + (r.stderr or "")).strip()


def _head(ws: str) -> str:
    return _git(ws, "rev-parse", "HEAD").stdout.strip()


def _branch_of(P) -> tuple[str, str]:
    pub = getattr(P.profile, "publish", None) or {}
    return (pub.get("remote") or P.profile.remote, pub.get("branch") or P.profile.branch)


# --------------------------------------------------------------------------- safe point

def _main_tasks(c, P) -> list[dict]:
    out = []
    for t in c.store.tasks(P.id):
        try:
            if c.tracks.project_for(P, t) is not P:
                continue
        except Exception:                                         # noqa: BLE001
            pass
        out.append(t)
    return out


def _paused_idle(c, P, t: dict) -> bool:
    from .selfheal import _paused_idle as idle
    return idle(c, P, t)


def _branch_own_diff(ws: str, ref: str):
    """The diff of the branch's OWN commits: those `git cherry HEAD ref` reports as not on
    HEAD (matched by patch content). 2026-10-29: a merge-base diff (HEAD...ref) of a branch
    whose base the coordinator's commit rewording had rewritten replayed hours of other
    work and failed. Falls back to HEAD...ref when cherry cannot say."""
    import types
    ch = _git(ws, "cherry", "HEAD", ref)
    if ch.returncode != 0:
        return _git(ws, "diff", "--binary", f"HEAD...{ref}")
    own = [l.split()[1] for l in (ch.stdout or "").splitlines() if l.startswith("+ ")]
    if not own:
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    first, last = own[0], own[-1]
    parent = _git(ws, "rev-parse", "--verify", "--quiet", first + "^")
    if parent.returncode != 0:
        return _git(ws, "diff", "--binary", f"HEAD...{ref}")
    # the own commits are consecutive at the branch's tip in the usual case (a branch cut
    # from master plus its commits); then their combined diff is parent(first)..last
    between = _git(ws, "rev-list", f"{first}^..{last}")
    if between.returncode == 0 and len(between.stdout.split()) == len(own):
        return _git(ws, "diff", "--binary", f"{first}^", last)
    return _git(ws, "diff", "--binary", f"HEAD...{ref}")


def safe_point(c, P, cfg: Optional[dict] = None) -> tuple[bool, str]:
    """(True, "") when the main checkout is quiet and clean, else (False, why)."""
    cfg = cfg or settings(P)
    pid = P.id
    proj = c.store.project(pid) or {}
    if proj.get("paused"):
        return False, "project paused"
    ws = P.profile.workspace
    wlock = f"workspace:{os.path.normcase(os.path.realpath(ws))}"
    block = set(cfg["block_states"])
    for lk in c.store.locks():
        if lk["resource"] == wlock and lk["task_id"] != TASK:
            # 2026-10-29: a sprint between sessions (CHANGES_REQUIRED / READY) keeps the
            # checkout lock; with the landing drain holding its next session, that lock
            # deadlocked the landing. Only a holder in a mid-sprint state blocks.
            holder = c.store.task(lk["project_id"], lk["task_id"]) or {}
            if holder.get("state") not in block:
                continue
            return False, f"main checkout in use by {lk['project_id']}/{lk['task_id']}"
    tasks = _main_tasks(c, P)
    for t in tasks:
        if t["state"] in block:
            return False, f"{t['task_id']} is {t['state']} (mid-sprint)"
    main_ids = {t["task_id"] for t in tasks}
    horizon = c.clock() - cfg["live_session_max_age_minutes"] * 60
    for s in c.store.q("SELECT session_id, task_id, role, status, created_ts FROM sessions "
                       "WHERE project_id=? AND ended_ts IS NULL AND created_ts>=?",
                       (pid, horizon)):
        if s["task_id"] in main_ids:
            t = next(x for x in tasks if x["task_id"] == s["task_id"])
            # 2026-10-29: a COMPLETE / CANCELLED sprint's leftover session (GF-FX5's helper,
            # never marked ended after its track merged) held every landing for 45 minutes
            if t.get("state") in ("COMPLETE", "CANCELLED"):
                continue
            # a paused task whose session finished its turn is not working in the
            # checkout (2026-10-28: paused GF-GATE1 stalled the queue this way)
            if _paused_idle(c, P, t):
                continue
            return False, f"session {s['session_id']} ({s['role'] or 'session'}) of " \
                          f"{s['task_id']} is open"
    try:
        for j in c.bg_checks.running():
            k = j.get("key") or ()
            if k and k[0] == pid:
                return False, f"background check running: {j.get('label')}"
    except Exception:                                             # noqa: BLE001
        pass
    for t in tasks:
        try:
            if c.studio_calls.active(P, t["task_id"]):
                return False, f"scripted Studio run of {t['task_id']}"
        except Exception:                                         # noqa: BLE001
            pass
    try:
        busy = c.tracks.merger.checks_busy(P)
        if busy:
            return False, f"track {busy}'s merge gate checks run in the main checkout"
    except Exception:                                             # noqa: BLE001
        pass
    _, branch = _branch_of(P)
    cur = _git(ws, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != branch:
        return False, f"main checkout is on {cur or '?'}, not {branch}"
    st = _git(ws, "status", "--porcelain", "--untracked-files=no")
    if st.returncode != 0:
        return False, f"git status failed: {_out(st)[:200]}"
    if st.stdout.strip():
        n = len(st.stdout.strip().splitlines())
        return False, f"main checkout has {n} uncommitted tracked change(s)"
    return True, ""


def _condition_met(c, pid: str, cond: dict) -> tuple[bool, str]:
    if not cond or not cond.get("after_task"):
        return True, ""
    t = c.store.task(pid, cond["after_task"])
    st = (t or {}).get("state") or "unknown"
    want = cond.get("state") or S.COMPLETE
    return (st == want), f"waits for {cond['after_task']} {want} (now {st})"


def next_batch(c, P) -> tuple[list[dict], str]:
    """The entries that may land now, in order, and why the batch stops where it does."""
    batch, why = [], ""
    for e in entries(c.store, P.id):
        if e["status"] in DONE:
            continue
        if e["status"] == "failed":
            why = f"{e['id']} failed: fix it, then retry (run now) or remove it"
            break
        ok, w = _condition_met(c, P.id, e["condition"])
        if not ok:
            why = f"{e['id']} {w}"
            break
        batch.append(e)
    return batch, why


# --------------------------------------------------------------------------- landing

def _commit(c, P, e: dict, cfg: dict) -> tuple[str, str]:
    """Commit what is staged. ('sha', out) | ('', 'empty') | (None, refusal output)."""
    from . import bookkeeping as BK
    ws = P.profile.workspace
    staged = [p for p in _git(ws, "diff", "--cached", "--name-only", "--no-renames")
              .stdout.splitlines() if p.strip()]
    if not staged:
        return "", "empty"
    msg = e["message"] + ("\n\n" + e["body"] if e["body"] else "") + "\n"
    fd, mf = tempfile.mkstemp(prefix="foundry-prep-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(msg)
        before = _head(ws)
        r = _git(ws, "commit", "-q", "-F", mf, env=BK.commit_env(P, staged),
                 timeout=cfg["commit_timeout_seconds"])
    finally:
        try:
            os.remove(mf)
        except OSError:
            pass
    after = _head(ws)
    if r.returncode != 0 or after == before:
        return None, f"commit refused (exit {r.returncode}): {_out(r)[-3000:]}"
    return after, _out(r)[-1000:]


def _apply_patch(c, P, e: dict, path: str, cfg: dict) -> tuple[Optional[str], str]:
    ws = P.profile.workspace
    chk = _git(ws, "apply", "--index", "--check", path)
    if chk.returncode != 0:
        rev = _git(ws, "apply", "--reverse", "--check", path)
        if rev.returncode == 0:
            return "", "already in the tree (the reverse patch applies); nothing to commit"
        return None, f"git apply --check failed at {_head(ws)[:12]}: {_out(chk)[-2000:]}"
    r = _git(ws, "apply", "--index", path)
    if r.returncode != 0:
        return None, f"git apply failed: {_out(r)[-2000:]}"
    return _commit(c, P, e, cfg)


def _apply_one(c, P, e: dict, cfg: dict, tmpdir: str) -> tuple[Optional[str], str]:
    """(sha, note) landed; ('', note) nothing to commit; (None, error) refused."""
    ws = P.profile.workspace
    if e["kind"] == "patch":
        if not os.path.isfile(e["source"]):
            return None, f"patch file not found: {e['source']}"
        return _apply_patch(c, P, e, e["source"], cfg)
    if e["kind"] == "branch":
        ref = e["source"]
        if _git(ws, "rev-parse", "--verify", "--quiet", ref + "^{commit}").returncode != 0:
            return None, f"branch or ref {ref!r} not found in the main checkout"
        d = _branch_own_diff(ws, ref)
        if d.returncode != 0:
            return None, f"git diff of {ref}'s own commits failed: {_out(d)[-1000:]}"
        if not d.stdout.strip():
            return "", f"{ref} has no changes beyond HEAD"
        p = os.path.join(tmpdir, f"{e['id']}.patch")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(d.stdout)
        return _apply_patch(c, P, e, p, cfg)
    # script: run the command in the workspace, then commit the named paths
    cmd = e["command"].replace("{python}", sys.executable).replace("{workspace}", ws)
    try:
        r = subprocess.run(cmd, shell=True, cwd=ws, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=cfg["script_timeout_seconds"],
                           env=_cmd_env())
    except subprocess.TimeoutExpired:
        return None, f"script timed out after {cfg['script_timeout_seconds']}s"
    out = _out(r)
    if r.returncode != 0:
        return None, f"script exited {r.returncode}: {out[-2000:]}"
    if e["paths"]:
        a = _git(ws, "add", "-A", "--", *e["paths"])
    else:
        a = _git(ws, "add", "-u")
    if a.returncode != 0:
        return None, f"git add failed: {_out(a)[-1000:]}"
    sha, note = _commit(c, P, e, cfg)
    if sha == "":
        return "", f"script made no change to commit ({out[-300:]})"
    return sha, (f"script: {out[-500:]}\n" if out else "") + (note or "")


def _cmd_env() -> dict:
    """A command's environment: UTF-8, and this coordinator's package importable (`-m
    foundry_client` / `-m foundry_coordinator` from the workspace cwd run this build)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pp = os.environ.get("PYTHONPATH", "")
    return dict(os.environ, PYTHONIOENCODING="utf-8",
                PYTHONPATH=root + (os.pathsep + pp if pp else ""))


def _rollback(ws: str, start: str) -> str:
    r = _git(ws, "reset", "-q", "--hard", start)
    return "reset to " + start[:12] + ("" if r.returncode == 0 else f" FAILED: {_out(r)[:300]}")


def _pull(P, cfg: dict) -> tuple[bool, str]:
    if not cfg["pull"]:
        return True, "pull off"
    ws = P.profile.workspace
    remote, branch = _branch_of(P)
    f = _git(ws, "fetch", "-q", remote, branch, timeout=180)
    if f.returncode != 0:
        return False, f"fetch {remote}/{branch} failed: {_out(f)[-300:]}"
    m = _git(ws, "merge", "-q", "--ff-only", "FETCH_HEAD")
    if m.returncode != 0:
        return False, f"{remote}/{branch} does not fast-forward: {_out(m)[-300:]}"
    return True, f"at {_head(ws)[:12]} after a fast-forward pull"


def _push(P, cfg: dict) -> tuple[bool, str]:
    if not cfg["push"]:
        return True, "not pushed ([prep_queue] push = false)"
    mode = (getattr(P.profile, "publish", None) or {}).get("mode", "none")
    if mode == "none":
        return True, "not pushed (publish mode none)"
    remote, branch = _branch_of(P)
    from .adapters.git import GitError
    try:
        P.ws.push(remote, branch)
    except (GitError, OSError) as e:
        return False, f"push to {remote}/{branch} failed: {str(e)[:400]}"
    return True, f"pushed {remote}/{branch} at {_head(P.profile.workspace)[:12]}"


def _after_action(c, P, a: str, cfg: dict, batch_id: str) -> str:
    pid, ws = P.id, P.profile.workspace
    if a == "kilo_config_apply":
        from .config_pull import ConfigPuller
        from .kilo_config import regenerate
        cached = ConfigPuller(c).cached(pid)
        rules = ((cached or {}).get("doc") or {}).get("role_rules")
        out = regenerate(P.profile, rules, apply=True, store=c.store)
        return f"kilo.jsonc: applied={out.get('applied')} changed={out.get('changed')}"
    if a == "reload_kilo":
        from . import client_requests
        rec = client_requests.request(c.state_dir, "reload_kilo", pid,
                                      f"prep queue batch {batch_id} landed", store=c.store,
                                      clock=c.clock)
        return f"reload_kilo requested ({rec['id']})"
    pre, _, arg = a.partition(":")
    arg = arg.strip()
    if pre == "resume":
        t = c.store.task(pid, arg)
        if not t:
            return f"resume {arg}: no such task"
        if t["state"] != S.PAUSED:
            return f"resume {arg}: it is {t['state']}, not PAUSED (left alone)"
        h = (t.get("data") or {}).get("hold") or {}
        if isinstance(h, dict) and str(h.get("owner") or "").startswith(OWNER + ":"):
            c.store.merge_data(pid, arg, hold=None)
        c.resume(pid, arg, actor=OWNER, note=f"prep queue batch {batch_id} landed")
        return f"resumed {arg}"
    if pre == "release_hold":
        t = c.store.task(pid, arg)
        h = ((t or {}).get("data") or {}).get("hold") or {}
        owner = str(h.get("owner") or "") if isinstance(h, dict) else ""
        if not owner.startswith(OWNER + ":"):
            return f"release_hold {arg}: no prep queue hold"
        return release_hold(c, pid, arg, owner)
    backup = os.path.join(c.state_dir, "prep-backups", batch_id)
    if pre == "copy":
        src, _, dest = arg.partition("=>")
        src, dest = src.strip(), dest.strip()
        if not os.path.isdir(src):
            raise PrepError(f"copy: {src} is not a folder")
        n = 0
        for root, _, files in os.walk(src):
            for fn in files:
                s = os.path.join(root, fn)
                rel = os.path.relpath(s, src)
                d = os.path.join(ws, dest, rel)
                if os.path.isfile(d):
                    b = os.path.join(backup, dest, rel)
                    os.makedirs(os.path.dirname(b), exist_ok=True)
                    shutil.copy2(d, b)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                n += 1
        return f"copied {n} file(s) {src} -> {dest} (overwritten files backed up in {backup})"
    if pre == "move_aside":
        p = os.path.join(ws, arg)
        if not os.path.exists(p):
            return f"move_aside {arg}: not there"
        b = os.path.join(backup, "moved-aside", arg)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.move(p, b)
        return f"moved {arg} aside to {b}"
    if pre == "run":
        cmd = arg.replace("{python}", sys.executable).replace("{workspace}", ws)
        try:
            r = subprocess.run(cmd, shell=True, cwd=ws, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=cfg["script_timeout_seconds"],
                               env=_cmd_env())
        except subprocess.TimeoutExpired:
            raise PrepError(f"run: timed out after {cfg['script_timeout_seconds']}s") from None
        if r.returncode != 0:
            raise PrepError(f"run: exit {r.returncode}: {_out(r)[-500:]}")
        return f"ran: {_out(r)[-300:]}"
    raise PrepError(f"unknown after-action {a!r}")


def _recover(c, P) -> None:
    """A batch a crashed coordinator left half done: back to its start commit."""
    key = _k("inflight", P.id)
    inf = _meta(c.store, key)
    if not inf:
        return
    ws = P.profile.workspace
    start = str(inf.get("start") or "")
    note = "interrupted batch (the coordinator stopped while landing it)"
    if start and _git(ws, "merge-base", "--is-ancestor", start, "HEAD").returncode == 0:
        note += "; " + _rollback(ws, start)
    for eid in inf.get("ids") or []:
        e = get(c.store, eid)
        if e and e["status"] == "pending":
            _log(c.store, eid, note, c.clock)
    c.store.release(P.id, TASK)
    c.store.x("DELETE FROM meta WHERE key=?", (key,))
    c.store.event(P.id, "", "prep_recovered", detail={"batch": inf.get("batch"), "note": note})


def land(c, P, batch: list[dict], cfg: Optional[dict] = None) -> dict:
    """Land `batch` all or nothing; the caller checked the safe point."""
    cfg = cfg or settings(P)
    pid, ws, store = P.id, P.profile.workspace, c.store
    bid = f"pb-{uuid.uuid4().hex[:8]}"
    wlock = f"workspace:{os.path.normcase(os.path.realpath(ws))}"
    held = store.acquire([wlock], pid, TASK)
    if held:
        return {"batch": bid, "result": "waiting",
                "detail": f"main checkout locked by {held['project_id']}/{held['task_id']}"}
    ok, why = _pull(P, cfg)
    if not ok:
        store.release(pid, TASK)
        return {"batch": bid, "result": "waiting", "detail": why}
    start = _head(ws)
    _set_meta(store, _k("inflight", pid), {"batch": bid, "start": start,
                                           "ids": [e["id"] for e in batch], "ts": c.clock()})
    done: list[tuple[dict, str, str]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="foundry-prep-") as tmp:
            for e in batch:
                try:
                    sha, note = _apply_one(c, P, e, cfg, tmp)
                except Exception as ex:                           # noqa: BLE001
                    sha, note = None, f"{type(ex).__name__}: {ex}"
                if sha is None:
                    rb = _rollback(ws, start)
                    for d, _, _ in done:
                        _set(store, d["id"], status="failed", batch_id=bid)
                        _log(store, d["id"], f"batch {bid} rolled back ({rb}) because "
                                             f"{e['id']} was refused", c.clock)
                    _set(store, e["id"], status="failed", batch_id=bid)
                    _log(store, e["id"], f"batch {bid}: {note}\n{rb}", c.clock)
                    for later in batch[batch.index(e) + 1:]:
                        _log(store, later["id"], f"batch {bid} stopped at {e['id']}; "
                                                 f"not tried", c.clock)
                    detail = {"batch": bid, "failed": e["id"], "message": e["message"],
                              "error": note[-3000:], "rolled_back": rb, "start": start,
                              "alert_class": "prep_failed",
                              "entries": [x["id"] for x in batch]}
                    store.event(pid, "", "prep_failed", state="failed", detail=detail)
                    c.log(f"{pid}: prep batch {bid} failed at {e['id']}: {note[:200]}")
                    return {"batch": bid, "result": "failed", "detail": note[-500:],
                            "failed": e["id"]}
                done.append((e, sha, note))
        now = c.clock()
        for e, sha, note in done:
            _set(store, e["id"], status="landed" if sha else "already_applied", sha=sha or "",
                 batch_id=bid, landed_ts=now)
            _log(store, e["id"], (f"landed as {sha[:12]}" if sha else "already applied")
                 + (f": {note[-800:]}" if note else ""), c.clock)
        pushed_ok, push_note = _push(P, cfg) if any(sha for _, sha, _ in done) else \
            (True, "nothing new to push")
        for e, _, _ in done:
            _log(store, e["id"], push_note, c.clock)
        if not pushed_ok:
            store.event(pid, "", "prep_push_failed", state="failed",
                        detail={"batch": bid, "error": push_note, "alert_class": "prep_failed"})
    finally:
        store.release(pid, TASK)
        store.x("DELETE FROM meta WHERE key=?", (_k("inflight", pid),))
    after_notes = []
    seen: list[str] = []
    for e, _, _ in done:
        for a in e["after_actions"]:
            if a not in seen:
                seen.append(a)
    for a in seen:
        try:
            note = _after_action(c, P, a, cfg, bid)
        except Exception as ex:                                   # noqa: BLE001
            note = f"{a} FAILED: {type(ex).__name__}: {ex}"[:600]
            store.event(pid, "", "prep_after_action_failed", detail={"batch": bid, "action": a,
                                                                     "error": note})
        after_notes.append(f"{a}: {note}"[:600])
        owners = [e["id"] for e, _, _ in done if a in e["after_actions"]]
        for eid in owners:
            _log(store, eid, f"after-action {a}: {note}", c.clock)
    for e, _, _ in done:
        for t in e["hold_tasks"]:
            _log(store, e["id"], release_hold(c, pid, t, f"{OWNER}:{e['id']}"), c.clock)
    landed = [{"id": e["id"], "sha": (sha or "")[:12], "message": e["message"]}
              for e, sha, _ in done]
    store.event(pid, "", "prep_landed", state="landed" if pushed_ok else "push_failed",
                detail={"batch": bid, "start": start[:12], "head": _head(ws)[:12],
                        "entries": landed, "push": push_note, "after": after_notes})
    c.log(f"{pid}: prep batch {bid} landed {len(landed)} entr(y/ies); {push_note}")
    return {"batch": bid, "result": "landed" if pushed_ok else "push_failed",
            "detail": push_note, "landed": landed, "after": after_notes}


# --------------------------------------------------------------------------- tick

def tick(c) -> None:
    """Engine hook (once per tick): land each project's ready batch at a safe point."""
    try:
        ensure(c.store)
    except Exception as e:                                        # noqa: BLE001
        c.log(f"prep queue: {type(e).__name__}: {e}")
        return
    for pid in sorted(c.projects):
        try:
            tick_project(c, c.projects[pid])
        except Exception as e:                                    # noqa: BLE001
            c.log(f"{pid}: prep queue tick failed: {type(e).__name__}: {e}")


def credit_landed(c, P) -> int:
    """Record every landed prep commit as a coordinator commit of each open main-checkout
    task (bookkeeping `coord_commits`, the SHA list scope and hygiene checks trust), so a
    sprint whose range spans a prep landing is never blamed for its files. 2026-10-28
    GF-GATE1 15:11: the 11 AU prep commits (.clinerules, AGENT_RULES.md, docs/...) failed
    its scope check and the repeat guard then stopped it. Idempotent; returns how many
    task lists changed."""
    from . import bookkeeping as BK
    from . import states as S
    shas = [r["sha"] for r in c.store.q(
        "SELECT sha FROM prep_queue WHERE project_id=? AND status='landed' AND sha<>'' "
        "ORDER BY landed_ts", (P.id,))]
    if not shas:
        return 0
    n = 0
    for t in _main_tasks(c, P):
        if t["state"] in S.TERMINAL or t["task_id"] == TASK:
            continue
        have = BK.commits(t)
        new = [s for s in shas if s not in have]
        if not new:
            continue
        kept = list((t.get("data") or {}).get(BK.KEY) or []) + new
        c.store.merge_data(P.id, t["task_id"], **{BK.KEY: kept[-BK.MAX_KEPT:]})
        n += 1
    return n


def tick_project(c, P, force: bool = False) -> Optional[dict]:
    cfg = settings(P)
    if not cfg["enabled"]:
        return None
    _recover(c, P)
    try:
        credit_landed(c, P)
    except Exception as e:                                        # noqa: BLE001
        c.log(f"{P.id}: prep queue: crediting landed commits failed: {type(e).__name__}: {e}")
    pid, store = P.id, c.store
    if not store.q("SELECT 1 FROM prep_queue WHERE project_id=? AND status='pending' LIMIT 1",
                   (pid,)):
        return None
    now = c.clock()
    rn = _meta(store, _k("run_now", pid))
    st = _meta(store, _k("state", pid))
    if not force and not rn and now - float(st.get("ts") or 0) < cfg["check_every_seconds"]:
        return None
    if rn:
        store.x("DELETE FROM meta WHERE key=?", (_k("run_now", pid),))
    batch, why = next_batch(c, P)
    state = {"ts": now, "safe": False, "reason": "", "batch": [e["id"] for e in batch],
             "stops_at": why}
    if not batch:
        state["reason"] = why or "nothing ready"
        _set_meta(store, _k("state", pid), state)
        return None
    ok, reason = safe_point(c, P, cfg)
    state.update(safe=ok, reason=reason)
    if not ok:
        _set_meta(store, _k("state", pid), state)
        return None
    res = land(c, P, batch, cfg)
    state.update(reason=res.get("detail", ""), result=res.get("result"))
    _set_meta(store, _k("state", pid), state)
    if res.get("result") != "waiting":
        _set_meta(store, _k("last", pid), {"ts": c.clock(), **res})
    return res


# --------------------------------------------------------------------------- views

def snapshot(c, P, log_lines: int = 20) -> dict:
    """The Repo changes view of one project (heartbeat `prep_queue` and the CLI)."""
    cfg = settings(P)
    rows = []
    for e in entries(c.store, P.id):
        rows.append({"id": e["id"], "order": e["ord"], "kind": e["kind"], "source": e["source"],
                     "message": e["message"], "body": e["body"][:1500],
                     "condition": e["condition"], "after_actions": e["after_actions"],
                     "hold_tasks": e["hold_tasks"], "hold_minutes": e["hold_minutes"],
                     "paths": e["paths"], "command": e["command"][:500], "status": e["status"],
                     "sha": (e["sha"] or "")[:12], "batch": e["batch_id"] or "",
                     "created_by": e["created_by"], "created_ts": e["created_ts"],
                     "landed_ts": e["landed_ts"],
                     "log": [{"ts": x.get("ts"), "msg": str(x.get("msg") or "")[:600]}
                             for x in e["log"][-log_lines:]]})
    return {"enabled": cfg["enabled"], "settings": cfg, "entries": rows,
            "state": _meta(c.store, _k("state", P.id)),
            "last": _meta(c.store, _k("last", P.id)),
            "pending": sum(1 for r in rows if r["status"] == "pending")}
