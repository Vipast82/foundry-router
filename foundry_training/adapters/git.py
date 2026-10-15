"""Git revisions -> commit events (subject, parents, changed files, capped
patch). Read-only git commands; the diff is redacted like everything else."""

from __future__ import annotations

import subprocess
from typing import Iterator

from ..schema import Event

ADAPTER = "git"
MAX_PATCH = 120_000


def _git(ws: str, *args) -> str:
    r = subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout


def commits(ws: str, project_id: str, shas: list[str]) -> Iterator[tuple[str, str, dict, dict]]:
    seen = set()
    for sha in shas:
        if not sha or sha in seen:
            continue
        seen.add(sha)
        try:
            meta = _git(ws, "show", "-s", "--format=%H%n%P%n%ct%n%s", sha).splitlines()
            files = [l for l in _git(ws, "show", "--name-status", "--format=", sha).splitlines()
                     if l.strip()]
            patch = _git(ws, "show", "--format=", "--patch", "--no-color", sha)
        except RuntimeError as e:
            yield (f"{project_id}:{sha}", project_id, {"sha": sha, "missing": str(e)},
                   {"adapter": ADAPTER, "sanitization": "none"})
            continue
        yield (f"{project_id}:{meta[0]}", project_id,
               {"sha": meta[0], "parents": meta[1].split(), "ts": int(meta[2]),
                "subject": meta[3] if len(meta) > 3 else "", "files": files,
                "patch": patch[:MAX_PATCH], "patch_truncated": len(patch) > MAX_PATCH},
               {"adapter": ADAPTER, "workspace": ws, "sanitization": "none"})


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    if p.get("missing"):
        return [Event(ADAPTER, f"{project_id}:{p['sha']}", "capture_gap", timestamp=0,
                      project_id=project_id, source_revision=p["sha"], capture_status="missing",
                      raw_ref=raw_ref, facts={"what": "commit", "error": p["missing"]})]
    return [Event(ADAPTER, f"{project_id}:{p['sha']}", "commit", timestamp=float(p["ts"]),
                  project_id=project_id, source_revision=p["sha"], raw_ref=raw_ref,
                  capture_status="truncated" if p.get("patch_truncated") else "complete",
                  redaction_status=redaction.get("status", "clean"),
                  facts={k: p[k] for k in ("parents", "subject", "files", "patch",
                                           "patch_truncated")})]


def diff(ws: str, base: str, head: str, cap: int = MAX_PATCH) -> tuple[str, bool]:
    try:
        out = _git(ws, "diff", "--no-color", f"{base}..{head}")
    except RuntimeError:
        return "", False
    return out[:cap], len(out) > cap
