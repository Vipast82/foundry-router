"""A push too big for one go is sent in chunks (COORDINATOR_AUTOMATION_NEEDS item 20).

2026-11-17: master had grown by more than GitHub's 2 GB per-push limit (evidence frames), and
FIN-5a1's closeout push was refused. The operator pushed 13 intermediate commits to
`foundry-push/*` staging branches by hand, then the branch itself; the staging branches
stayed on GitHub.

  when      before a closeout push the outgoing size is measured (`git rev-list
            --disk-usage`, what HEAD has that the remote branch and the staging branches
            lack). Over `[automation] push_chunk_mb` (default 1500, 0 = never chunk), or
            after a plain push the remote refused for its size, the push is chunked.
  how       commits along the branch's first-parent line are grouped so each group is
            under the limit; each group's last commit is pushed to
            `refs/heads/foundry-push/<branch>-<n>`, then the branch itself, which by then
            only needs the last group. One commit bigger than the limit cannot be split:
            it is pushed on its own and the remote decides.
  where     in a background thread that only runs git. The tick thread never waits for it:
            the closeout stays in its push wait ("pushing in chunks: 3 of 13 sent") and
            finishes on the retry that finds the remote at the head.
  after     the staging branches are deleted. So is any older `foundry-push/*` branch whose
            commit is already part of the pushed head (the ones left from 11-17); one that
            is not part of it is left alone and named.

A restart loses nothing: the staging branches already on the remote are not sent again.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional

STAGING = "foundry-push"
CHUNK_TIMEOUT_S = 3600
MB = 1024 * 1024
# what a remote says when one push is too big (GitHub: "pack exceeds maximum allowed size")
SIZE_REFUSALS = re.compile(r"pack exceeds|exceeds maximum allowed size|HTTP 413|"
                           r"Request Entity Too Large|RPC failed; HTTP 5\d\d|unpack failed|"
                           r"timed out", re.I)
JOBS: dict = {}                  # (workspace path, remote, branch) -> job


def size_refusal(error: str) -> bool:
    return bool(SIZE_REFUSALS.search(error or ""))


def _known(ws, sha: str) -> bool:
    try:
        ws.run("cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except Exception:                                            # noqa: BLE001
        return False


def staging_refs(ws, remote: str) -> dict:
    """{ref: sha} of the remote's foundry-push/* branches ({} when it cannot be listed)."""
    try:
        out = ws.run("ls-remote", remote, f"refs/heads/{STAGING}/*", timeout=60)
    except Exception:                                            # noqa: BLE001
        return {}
    refs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            refs[parts[1]] = parts[0]
    return refs


def outgoing_bytes(ws, head: str, have: list) -> Optional[int]:
    """On-disk size of what `head` has that the commits in `have` lack; None = unknown."""
    try:
        out = ws.run("rev-list", "--objects", "--disk-usage", head,
                     *[f"^{h}" for h in have], timeout=300)
        return int(out.strip() or 0)
    except Exception:                                            # noqa: BLE001
        return None


def plan(ws, head: str, base: Optional[str], have: list, chunk_bytes: int) -> list:
    """The commits to push one after another, the last one `head`: each step adds at most
    `chunk_bytes` (more only when a single commit is bigger)."""
    args = ["rev-list", "--reverse", "--first-parent", head] + ([f"^{base}"] if base else [])
    commits = [l.strip() for l in ws.run(*args, timeout=300).splitlines() if l.strip()]
    if not commits:
        return [head]
    out, got, i = [], list(have), 0
    while i < len(commits):
        lo, hi, best = i, len(commits) - 1, i
        while lo <= hi:
            mid = (lo + hi) // 2
            size = outgoing_bytes(ws, commits[mid], got)
            if size is not None and size <= chunk_bytes:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        out.append(commits[best])
        got = got + [commits[best]]
        i = best + 1
    if out[-1] != commits[-1]:
        out.append(commits[-1])
    return out


def measure(ws, remote: str, head: str, remote_head: Optional[str]) -> dict:
    """{"bytes", "have", "base"}: what a push of `head` would send."""
    base = remote_head if remote_head and re.fullmatch(r"[0-9a-f]{40}", str(remote_head)) \
        and _known(ws, remote_head) else None
    have = ([base] if base else []) + [s for s in staging_refs(ws, remote).values()
                                       if _known(ws, s) and s != base]
    return {"bytes": outgoing_bytes(ws, head, have), "have": have, "base": base}


def status(ws, remote: str, branch: str) -> Optional[dict]:
    return JOBS.get((ws.path, remote, branch))


def clear(ws, remote: str, branch: str) -> None:
    JOBS.pop((ws.path, remote, branch), None)


def start(ws, remote: str, branch: str, head: str, m: dict, chunk_bytes: int,
          run_in_thread: bool = True) -> dict:
    """Start the chunked push of `head`. Returns the job: {"state": "running" | "done" |
    "failed", "head", "chunks", "sent", "error", "deleted", "kept", "started"}."""
    key = (ws.path, remote, branch)
    job = {"state": "running", "head": head, "chunks": 0, "sent": 0, "error": "",
           "deleted": [], "kept": [], "started": time.time(), "reported": False}
    JOBS[key] = job

    def work():
        try:
            steps = plan(ws, head, m.get("base"), list(m.get("have") or []), chunk_bytes)
            job["chunks"] = len(steps)
            mine = []
            for n, sha in enumerate(steps[:-1], 1):
                ref = f"refs/heads/{STAGING}/{branch}-{n}"
                ws.run("push", remote, f"+{sha}:{ref}", timeout=CHUNK_TIMEOUT_S)
                mine.append(ref)
                job["sent"] = n
            ws.run("push", remote, f"{head}:refs/heads/{branch}", timeout=CHUNK_TIMEOUT_S)
            job["sent"] = len(steps)
            deleted, kept = sweep(ws, remote, head)
            job.update(deleted=deleted, kept=kept, state="done")
        except Exception as e:                                   # noqa: BLE001
            job.update(state="failed", error=f"{type(e).__name__}: {e}"[:400])
        finally:
            job["ended"] = time.time()
    if run_in_thread:
        t = threading.Thread(target=work, daemon=True, name=f"push-{branch}")
        job["thread"] = t
        t.start()
    else:
        work()
    return job


def sweep(ws, remote: str, head: str) -> tuple:
    """Delete the remote's foundry-push/* branches whose commit is part of `head`.
    (deleted, kept): kept ones are not part of it, or could not be judged."""
    deleted, kept = [], []
    for ref, sha in sorted(staging_refs(ws, remote).items()):
        short = ref[len("refs/heads/"):]
        inside = False
        if _known(ws, sha):
            try:
                ws.run("merge-base", "--is-ancestor", sha, head)
                inside = True
            except Exception:                                    # noqa: BLE001
                inside = False
        if not inside:
            kept.append(short)
            continue
        try:
            ws.run("push", remote, "--delete", ref, timeout=120)
            deleted.append(short)
        except Exception:                                        # noqa: BLE001
            kept.append(short)
    return deleted, kept


def words(job: dict) -> str:
    if job["state"] == "running":
        return (f"pushing in chunks: {job['sent']} of {job['chunks']} sent" if job["chunks"]
                else "pushing in chunks: measuring")
    if job["state"] == "failed":
        return f"the chunked push failed after {job['sent']} of {job['chunks']}: {job['error']}"
    return (f"pushed in {job['chunks']} chunk(s)"
            + (f"; deleted {len(job['deleted'])} staging branch(es)" if job["deleted"] else "")
            + (f"; left {', '.join(job['kept'][:5])} (not part of this branch)"
               if job["kept"] else ""))
