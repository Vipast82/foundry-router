"""Git revisions -> commit events (subject, parents, changed files, capped
patch). Read-only git commands; the diff is redacted like everything else.

Helper provenance the coordinator does not record itself:
  * a commit whose message carries `(helper:<unit>)` or
    `(helper:precode-harvest)` (the au-fast-lane skill commits a
    helper-drafted step on its own with that tag) -> an `authorship` event
    with block hashes over its WHOLE patch (never the capped copy);
  * files under docs/progress/*_PRECODE/ (harvested frontier pre-code
    models) -> an `authorship` event per file (helper:precode-harvest).
Both are project-wide: any step that contains their code is helper-authored.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from typing import Iterator

from ..schema import Event

ADAPTER = "git"
MAX_PATCH = 120_000
MAX_HELPER_PATCH = 50_000_000
MAX_RANGE = 2000
HELPER_TAG = re.compile(r"\(helper:([A-Za-z0-9_.\-]+)\)")
PRECODE_GLOB = ("docs", "progress", "*_PRECODE")


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
            body = _git(ws, "show", "-s", "--format=%B", sha)
        except RuntimeError as e:
            yield (f"{project_id}:{sha}", project_id, {"sha": sha, "missing": str(e)},
                   {"adapter": ADAPTER, "sanitization": "none"})
            continue
        payload = {"sha": meta[0], "parents": meta[1].split(), "ts": int(meta[2]),
                   "subject": meta[3] if len(meta) > 3 else "", "files": files,
                   "patch": patch[:MAX_PATCH], "patch_truncated": len(patch) > MAX_PATCH}
        tag = HELPER_TAG.search(body or "")
        if tag:
            # the whole patch, uncapped: a block past the cap must still match
            payload["helper"] = {"unit": tag.group(1), "patch": patch[:MAX_HELPER_PATCH]}
        yield (f"{project_id}:{meta[0]}", project_id, payload,
               {"adapter": ADAPTER, "workspace": ws, "sanitization": "none"})


def range_shas(ws: str, base: str, head: str) -> list[str]:
    """Commits in base..head (a session's intermediate commits, where helper
    -tagged steps live). Empty when git cannot resolve the range."""
    try:
        out = _git(ws, "rev-list", f"--max-count={MAX_RANGE}", f"{base}..{head}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def precode_records(ws: str, project_id: str) -> Iterator[tuple[str, str, dict, dict]]:
    """Files under docs/progress/*_PRECODE/ (harvested frontier models)."""
    root = os.path.realpath(ws)
    for d in sorted(glob.glob(os.path.join(ws, *PRECODE_GLOB))):
        for path in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
            full = os.path.realpath(path)
            if not os.path.isfile(full) or not full.startswith(root + os.sep):
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read(MAX_HELPER_PATCH)
            except OSError:
                continue
            yield (f"{project_id}:precode:{rel}", project_id,
                   {"precode": rel, "text": text},
                   {"adapter": ADAPTER, "workspace": ws, "sanitization": "none"})


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    from ..labels import index_facts
    if p.get("precode"):
        from ..schema import content_hash
        return [Event(ADAPTER, f"{project_id}:precode:{p['precode']}:"
                      f"{content_hash(p['text'])[:16]}", "authorship", timestamp=0,
                      project_id=project_id, actor_role="assistant",
                      actor_model="helper:precode-harvest", raw_ref=raw_ref,
                      redaction_status=redaction.get("status", "clean"),
                      facts={"author": "helper:precode-harvest", "basis": "precode-copy",
                             "unit": "precode-harvest", "patch": "", "paths": [p["precode"]],
                             **{k: v for k, v in index_facts(p["text"]).items()},
                             "note": "harvested frontier pre-code model"})]
    if p.get("missing"):
        return [Event(ADAPTER, f"{project_id}:{p['sha']}", "capture_gap", timestamp=0,
                      project_id=project_id, source_revision=p["sha"], capture_status="missing",
                      raw_ref=raw_ref, facts={"what": "commit", "error": p["missing"]})]
    out = []
    if p.get("helper"):
        unit = p["helper"]["unit"]
        out.append(Event(ADAPTER, f"{project_id}:{p['sha']}", "authorship", part="helper",
                         timestamp=float(p["ts"]), project_id=project_id,
                         source_revision=p["sha"], actor_role="assistant",
                         actor_model=f"helper:{unit}", raw_ref=raw_ref,
                         redaction_status=redaction.get("status", "clean"),
                         facts={"author": f"helper:{unit}", "basis": "helper-commit",
                                "unit": unit, "patch": "", "commit": p["sha"],
                                "paths": [l.split("	")[-1] for l in p.get("files") or []],
                                **index_facts(p["helper"]["patch"]),
                                "note": p.get("subject", "")[:400]}))
    return out + [Event(ADAPTER, f"{project_id}:{p['sha']}", "commit", timestamp=float(p["ts"]),
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
