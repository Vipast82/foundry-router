"""The coordinator rewords its own unpushed commit messages (W1.4, 2026-10-22).

`coordinator.commit_messages` requires every commit in base..head to start with
the task prefix ("<ID>: "). Until now the only way to satisfy it after the fact
was a history rewrite, which the session is forbidden to do: UI-02b stopped with
"may I rebase to reword 9ec01d3" and a lead repair session ran 6.7 hours. Now:

  * `revert:` / `Revert "..."` commits and merge commits are exempt from the check;
  * before anything is bound to a head (gate, review, approvals) the coordinator
    rewords the unprefixed commits of the UNPUSHED, linear range itself: every
    commit from the oldest bad one up to HEAD is rebuilt with `git commit-tree`
    (same tree, same author and committer, same body; only the subject gains the
    prefix) and the branch ref is moved with a compare-and-swap `update-ref`. No
    rebase, no editor, no index or working-tree change: a dirty tree is fine;
  * the old -> new SHAs are journaled (event `commits_reworded`, task data
    `reworded_commits`) and the coordinator's own commit list (`coord_commits`)
    is remapped, so its checks keep telling its commits from the agent's;
  * a commit already on the remote, or a range with a merge commit, is never
    rewritten: the check becomes advisory for it instead. The session is never
    asked to rebase.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Optional

from .adapters.git import GitError

EXEMPT = re.compile(r'^\s*(?:revert\b|Revert\s+"|Reapply\s+")', re.I)
KEY = "reworded_commits"


def prefix_of(P, task_id: str) -> str:
    return str(P.profile.commit.get("prefix", "{task}") or "{task}").replace("{task}", task_id)


def _subject_ok(subject: str, prefix: str) -> bool:
    return str(subject or "").startswith(prefix)


def unprefixed(P, commits: list, prefix: str) -> tuple[list, list]:
    """([(sha, subject)] lacking the prefix and not exempt, [(sha, subject)] exempt)
    for a base..head commit list (newest first). Merge commits are exempt."""
    bad, exempt = [], []
    for sha, subject in commits:
        if _subject_ok(subject, prefix):
            continue
        if EXEMPT.match(subject or "") or _is_merge(P, sha):
            exempt.append((sha, subject))
        else:
            bad.append((sha, subject))
    return bad, exempt


def _is_merge(P, sha: str) -> bool:
    try:
        line = P.ws.run("rev-list", "--parents", "-n", "1", sha).strip()
    except GitError:
        return False
    return len(line.split()) > 2


def _remote_ref(P) -> str:
    remote = str(getattr(P.profile, "remote", "") or P.profile.raw.get("workspace", {}).get(
        "remote") or "origin")
    branch = str(getattr(P.profile, "branch", "") or P.profile.raw.get("workspace", {}).get(
        "branch") or "")
    if not branch:
        try:
            branch = P.ws.branch()
        except GitError:
            return ""
    return f"refs/remotes/{remote}/{branch}"


def pushed(P, sha: str) -> bool:
    """Is `sha` reachable from the local remote-tracking ref? (Only the coordinator
    pushes from this workspace, so that ref is current after every publish.)"""
    ref = _remote_ref(P)
    if not ref:
        return False
    try:
        P.ws.run("rev-parse", "--verify", "-q", ref)
    except GitError:
        return False                     # no remote ref yet: nothing was pushed
    return P.ws.is_ancestor(sha, ref)


def rewordable(P, t: dict, shas: list) -> str:
    """'' when the coordinator may reword these commits itself, else why not
    (the check then stays advisory for them)."""
    if not shas:
        return ""
    for sha in shas:
        if pushed(P, sha):
            return f"{sha[:10]} is already pushed"
    base = (t.get("data") or {}).get("task_base") or t.get("base_sha") or ""
    try:
        head = P.ws.head()
        if base and P.ws.run("rev-list", "--merges", f"{base}..{head}").strip():
            return "a merge commit is in the range"
        if P.ws.run("symbolic-ref", "-q", "HEAD", check=False).strip() == "":
            return "HEAD is detached"
    except GitError as e:
        return f"git: {e}"[:120]
    return ""


def _meta(P, sha: str) -> dict:
    fmt = "%T%n%P%n%an%n%ae%n%aD%n%cn%n%ce%n%cD"
    head = P.ws.run("log", "-1", f"--format={fmt}", sha).splitlines()
    body = P.ws.run("log", "-1", "--format=%B", sha)
    keys = ("tree", "parents", "an", "ae", "ad", "cn", "ce", "cd")
    out = {k: (head[i] if i < len(head) else "") for i, k in enumerate(keys)}
    out["body"] = body
    return out


def reword(c, P, t: dict, spec) -> dict:
    """Reword the unprefixed, unpushed commits of task `t` (base..HEAD). Returns
    {"reworded": [{old, new, subject}], "head": new head} or {"skipped": why} /
    {} when nothing needed it. Journals an event either way when it did something."""
    pid, tid = P.id, t["task_id"]
    prefix = prefix_of(P, tid)
    base = (t.get("data") or {}).get("task_base") or t.get("base_sha") or ""
    if not base or not P.profile.commit.get("require_prefix", True):
        return {}
    try:
        head = P.ws.head()
        commits = P.ws.commits(base, head)
    except GitError:
        return {}
    bad, _ = unprefixed(P, commits, prefix)
    if not bad:
        return {}
    why = rewordable(P, t, [s for s, _ in bad])
    if why:
        c.store.event(pid, tid, "commits_not_reworded", detail={
            "why": why, "commits": [f"{s[:10]} {m[:60]}" for s, m in bad[:8]]})
        return {"skipped": why}
    try:
        branch = P.ws.run("symbolic-ref", "-q", "HEAD").strip()
    except GitError:
        return {"skipped": "HEAD is detached"}
    order = [s for s in P.ws.run("rev-list", "--reverse", f"{base}..{head}").split() if s]
    bad_set = {s for s, _ in bad}
    first = next((i for i, s in enumerate(order) if s in bad_set), None)
    if first is None:
        return {}
    parent = _meta(P, order[first])["parents"].split()[0] if first >= 0 else base
    mapping: dict = {}
    reworded = []
    sep = "" if prefix.rstrip().endswith(":") else ":"
    for sha in order[first:]:
        m = _meta(P, sha)
        body = m["body"]
        if sha in bad_set:
            lines = body.split("\n", 1)
            subject = lines[0].strip()
            new_subject = f"{prefix.rstrip()}{sep} {subject}".rstrip()
            body = new_subject + ("\n" + lines[1] if len(lines) > 1 else "\n")
            reworded.append({"old": sha, "subject": subject, "new_subject": new_subject})
        env = dict(os.environ, GIT_AUTHOR_NAME=m["an"], GIT_AUTHOR_EMAIL=m["ae"],
                   GIT_AUTHOR_DATE=m["ad"], GIT_COMMITTER_NAME=m["cn"],
                   GIT_COMMITTER_EMAIL=m["ce"], GIT_COMMITTER_DATE=m["cd"])
        fd, path = tempfile.mkstemp(prefix="foundry-msg-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body if body.endswith("\n") else body + "\n")
            new = _run_env(P, env, "commit-tree", m["tree"], "-p", parent, "-F", path).strip()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not new:
            return {"skipped": f"commit-tree failed for {sha[:10]}"}
        mapping[sha] = new
        parent = new
    try:
        P.ws.run("update-ref", "-m", "coordinator: reword commit messages (task prefix)",
                 branch, parent, head)
    except GitError as e:
        c.store.event(pid, tid, "commits_not_reworded", detail={"why": f"update-ref: {e}"[:300]})
        return {"skipped": f"update-ref: {e}"[:200]}
    for r in reworded:
        r["new"] = mapping[r["old"]]
    # the coordinator's own commits above the oldest bad one got new SHAs
    cur = c.store.task(pid, tid) or t
    data = cur.get("data") or {}
    coord = [mapping.get(s, s) for s in (data.get("coord_commits") or [])]
    kept = list(data.get(KEY) or []) + [{"old": r["old"], "new": r["new"],
                                        "subject": r["subject"][:120]} for r in reworded]
    upd = {KEY: kept[-100:], "commit_map": {**(data.get("commit_map") or {}),
                                           **{k[:12]: v for k, v in mapping.items()}}}
    if data.get("coord_commits"):
        upd["coord_commits"] = coord
    c.store.merge_data(pid, tid, **upd)
    c.store.event(pid, tid, "commits_reworded", detail={
        "old_head": head, "new_head": parent, "prefix": prefix,
        "reworded": [{"old": r["old"][:12], "new": r["new"][:12], "subject": r["subject"][:80]}
                     for r in reworded], "rebuilt": len(mapping)})
    return {"reworded": reworded, "head": parent}


def _run_env(P, env: dict, *args) -> str:
    import subprocess
    r = subprocess.run(["git", "-C", P.ws.path, *args], capture_output=True, text=True,
                       timeout=120, env=env, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def mapped(t: dict, sha: str) -> Optional[str]:
    """The current SHA of a commit that may have been reworded (by its 12-char prefix)."""
    m = ((t or {}).get("data") or {}).get("commit_map") or {}
    return m.get(str(sha or "")[:12])
