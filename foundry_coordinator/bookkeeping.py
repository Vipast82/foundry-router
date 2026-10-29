"""Deterministic bookkeeping the coordinator does itself, never a model session
(automation revamp B1/B4/B7, 2026-10-21).

UI-D1 and UI-02a spent ~20 implementer sessions on work with no product content:
committing the gate's captures and evidence log, ticking plan checklists, writing
closeout documents. Here the coordinator makes those commits itself and records
their SHAs in the task data (`coord_commits`), so its own checks can tell its
commits from the agent's: a hygiene or scope check fails a session only for a
file the agent changed. The SHA list is the authority, not the commit message,
which an agent could copy.
"""

from __future__ import annotations

import re
from typing import Optional

from .adapters.git import GitError
from .profile import matches_any

KEY = "coord_commits"            # task data: SHAs of commits the coordinator made
MAX_KEPT = 200


def message(P, tid: str, text: str) -> str:
    """`text` under the profile's commit prefix ("T1" -> "T1: text")."""
    prefix = str(P.profile.commit.get("prefix", "{task}") or "{task}").replace("{task}", tid)
    head = prefix.rstrip()
    if not head.endswith(":"):
        head += ":"
    return f"{head} {text}"


def commits(t: dict) -> set:
    return set((t.get("data") or {}).get(KEY) or [])


def commit_env(P, paths: list) -> Optional[dict]:
    """The project's environment for a coordinator commit of exactly `paths`
    (adapter.commit_env: AU_ALLOW_SCOPE for AU's hooks); None when it gives none."""
    fn = getattr(getattr(P, "adapter", None), "commit_env", None)
    if not callable(fn):
        return None
    try:
        env = fn(sorted(str(p) for p in paths))
    except Exception:                                             # noqa: BLE001
        return None
    return dict(env) if isinstance(env, dict) and env else None


def commit(c, P, t: dict, paths: list, text: str, kind: str) -> tuple[str, str]:
    """Commit exactly `paths` in the workspace as the coordinator. (sha, "") on a
    commit, ("", "") when there was nothing to commit, ("", error) on failure (the
    paths are unstaged again by commit_paths)."""
    tid = t["task_id"]
    msg = message(P, tid, text)
    try:
        sha = P.ws.commit_paths(sorted(paths), msg, env=commit_env(P, paths))
    except (GitError, OSError) as e:
        c.store.event(P.id, tid, "coordinator_commit_failed", detail={
            "kind": kind, "paths": sorted(paths)[:40], "error": str(e)[:400]})
        return "", str(e)[:400]
    if not sha:
        return "", ""
    cur = c.store.task(P.id, tid) or t
    kept = list((cur.get("data") or {}).get(KEY) or []) + [sha]
    c.store.merge_data(P.id, tid, **{KEY: kept[-MAX_KEPT:]})
    c.store.event(P.id, tid, "coordinator_commit", detail={
        "kind": kind, "sha": sha, "message": msg, "paths": sorted(paths)[:40],
        "count": len(paths)})
    return sha, ""


# a sprint id: capitalised, with a digit somewhere (UI-SKIN2, AS-V01, GF-GATE1, S57a, S24)
_PREFIX = re.compile(r"^([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9.]+)*):")


def other_sprint(subject: str, tid: str) -> bool:
    """A commit subject that starts with ANOTHER sprint's id prefix ("UI-SKIN2: ...") is
    that sprint's work. 2026-10-28 AS-P5: master moved under a long sprint (UI-SKIN2
    captures, coordinator commits for AS-V0x / AS-D13 docs); its scope check blamed it for
    them, it asked for scope grants on other sprints' files, and each grant restarted it."""
    m = _PREFIX.match(str(subject or ""))
    return bool(m) and any(ch.isdigit() for ch in m.group(1)) and m.group(1) != tid


def agent_files(P, t: dict, base: str, head: str) -> Optional[set]:
    """Files changed base..head by commits the coordinator did NOT make and no other
    sprint made, or None when git can't say (callers then treat every changed file as the
    agent's)."""
    mine = commits(t)
    tid = str(t.get("task_id") or "")
    try:
        others = {sha for sha, subj in P.ws.commits(base, head) if other_sprint(subj, tid)}
    except Exception:                                             # noqa: BLE001
        others = set()
    if not mine and not others:
        return None
    try:
        per = P.ws.commit_files(base, head)
    except Exception:                                             # noqa: BLE001
        return None
    out: set = set()
    for sha, files in per:
        if sha not in mine and sha not in others:
            out.update(f.replace("\\", "/") for f in files)
    return out


def only_coordinator(P, t: dict, base: str, head: str, files: list) -> list:
    """The subset of `files` that only coordinator commits touched in base..head."""
    agent = agent_files(P, t, base, head)
    if agent is None:
        return []
    return [f for f in files if f.replace("\\", "/") not in agent]


# ---- plan checklist ticks (B4) --------------------------------------------------------

_BOX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s*)\[( |~|-|/)\](\s*)(.*)$")
_DONE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[[xX]\]")


def tick_lines(text: str, verified: dict) -> tuple[str, list]:
    """Tick open checklist rows of `text` whose wording matches one of `verified`
    ({regex: evidence line}). The final CLOSE row (the last row) is never ticked:
    it belongs to closeout. CRLF files keep their line ends. Returns (new text,
    [ticked row texts])."""
    lines = text.split("\n")
    bare = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    rows = [i for i, ln in enumerate(bare) if _BOX.match(ln) or _DONE.match(ln)]
    if not rows:
        return text, []
    last = rows[-1]
    ticked = []
    for i in rows:
        m = _BOX.match(bare[i])
        if i == last or not m:
            continue
        body = m.group(4)
        for pat, ev in verified.items():
            if re.search(pat, body, re.I):
                end = lines[i][len(bare[i]):]
                lines[i] = (f"{m.group(1)}[x]{m.group(3)}{body} (verified by the coordinator: "
                            f"{ev}){end}")
                ticked.append(body[:160])
                break
    return "\n".join(lines), ticked


def runner_paths(dirty: list, patterns: list, exact: set) -> list:
    """Dirty paths that are the runner's own outputs (patterns or exact capture paths)."""
    return sorted({p.replace("\\", "/") for _, p in dirty
                   if matches_any(p, patterns) or p.replace("\\", "/") in exact})
