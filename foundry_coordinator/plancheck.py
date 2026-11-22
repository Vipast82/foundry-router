"""A plan check when a sprint starts (COORDINATOR_AUTOMATION_NEEDS item 22).

Plans pointed at the wrong folder (`docs/evidence/FIN/map/` for `FIN-0/map/`), named sprints
that do not exist (FIN-5a for FIN-5a1, FIN-3b), and a new BAL plan copied the wrong
difficulty and seeds into its run steps. Each cost a session before anybody noticed.

Before a sprint's first session the plan is read once (again when the plan changes):

  paths      every repo path the plan names in backticks, outside its own file list and
             its own outputs, exists. A path with a placeholder (`<stamp>`, `*`, `{x}`) or
             outside the repository's top-level folders is not judged.
  sprints    every sprint its "Depends on" names is a sprint of the project: on the queue,
             known to the adapter, or with a plan file. A near miss is suggested
             ("FIN-5a: no such sprint; did you mean FIN-5a1?").
  run table  a table with an `Id` column and Difficulty / Seed / Length columns is compared
             with the plan's own step lines for those ids ("**CQb:** ... Full, Normal, seed b
             (737125272)") and with JSON blocks that name the id: a step or block that
             states another value than its row is reported.

`[automation] plan_check`: `warn` (default) writes the findings as an event and one notice,
and the sprint starts; `block` stops the sprint with the findings as its blocker, once per
version of the plan (fix the plan, or unblock to go on as it is); `off` skips it. It warns first because a plan may name a path on purpose
that a step creates: turn `block` on after a clean week.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from typing import Optional

from .automation import get as auto_get

PATH_RE = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*)`")
MAX_FINDINGS = 12


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------- paths

def check_paths(text: str, task_id: str, workspace: str, own_files: list) -> list:
    import fnmatch
    out, seen = [], set()
    try:
        tops = {d for d in os.listdir(workspace) if os.path.isdir(os.path.join(workspace, d))}
    except OSError:
        return out
    own = [str(f).replace("\\", "/").rstrip("/") for f in own_files or []]
    for m in PATH_RE.finditer(text):
        p = m.group(1).rstrip(".,;:")
        if p in seen or any(ch in p for ch in "<>*{}$") or p.startswith(("http", "//")):
            continue
        seen.add(p)
        top = p.split("/", 1)[0]
        if top not in tops:
            continue                                 # not a path of this repository
        if f"/{task_id}/" in f"/{p}/" or f"/{task_id}_" in f"/{p}" or \
                os.path.basename(p.rstrip("/")).startswith(task_id + "_"):
            continue                                 # its own outputs
        bare = p.rstrip("/")
        if any(bare == o or bare.startswith(o + "/") or o.startswith(bare + "/")
               or fnmatch.fnmatchcase(bare, o) for o in own):
            continue                                 # in its file list: it may create it
        if os.path.exists(os.path.join(workspace, *bare.split("/"))):
            continue
        # the first part of the path that is not there, and what is there instead
        parts = bare.split("/")
        k = 0
        while k < len(parts) and os.path.isdir(os.path.join(workspace, *parts[:k + 1])):
            k += 1
        anc, rest = parts[:k], parts[k + 1:]
        hint = ""
        try:
            there = os.listdir(os.path.join(workspace, *anc))
        except OSError:
            there = []
        near = difflib.get_close_matches(parts[k], there, 3, 0.5) if k < len(parts) else []
        for n in near:
            if os.path.exists(os.path.join(workspace, *anc, n, *rest)):
                hint = f"; {'/'.join(anc + [n] + rest)} exists"
                break
        else:
            if near:
                hint = f"; {'/'.join(anc)}/ has {near[0]}"
        out.append({"kind": "path", "what": p, "text": f"`{p}` does not exist{hint}"})
    return out


# ---------------------------------------------------------------------------- sprints

def check_sprints(c, P, text: str, task_id: str) -> list:
    from .tracks import analysis as AN
    out = []
    ws = P.profile.workspace
    known_fn = getattr(P.adapter, "is_known_task", None)
    plan_dir = os.path.join(ws, "docs", "progress")
    try:
        plans = sorted(n[:-len("_PLAN.md")] for n in os.listdir(plan_dir) if n.endswith("_PLAN.md"))
    except OSError:
        plans = []
    queued = [t["task_id"] for t in c.store.tasks(P.id)]
    for d in AN.plan_deps(text, task_id, has_plan=lambda i: i in plans):
        if d in queued or d in plans:
            continue
        try:
            if callable(known_fn) and known_fn(d):
                continue
        except Exception:                                        # noqa: BLE001
            continue
        if not plans and not callable(known_fn):
            continue                                 # nothing to judge against
        near = difflib.get_close_matches(d, sorted(set(plans) | set(queued)), 1, 0.7)
        out.append({"kind": "sprint", "what": d,
                    "text": f"{d}: no such sprint" + (f"; did you mean {near[0]}?" if near else "")})
    return out


# ---------------------------------------------------------------------------- run table

COLS = ("difficulty", "seed", "length")


def _tables(text: str) -> list:
    """[{"id": ..., "difficulty": ..., "seed": ..., "length": ...}] from every markdown table
    that has an Id column and at least one of the judged columns."""
    rows, lines = [], text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip().lower() for c in line.strip().strip("|").split("|")]
            idx = {h: k for k, h in enumerate(head)}
            cols = [c for c in COLS if c in idx]
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if "id" in idx and cols and len(cells) == len(head):
                    row = {"id": cells[idx["id"]].strip("` *")}
                    for c in cols:
                        row[c] = cells[idx[c]]
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,23}", row["id"]):
                        rows.append(row)
                j += 1
            i = j
            continue
        i += 1
    return rows


def _seed(v: str) -> str:
    m = re.search(r"\d{3,}", v or "")
    return m.group(0) if m else ""


def check_runs(text: str) -> list:
    rows = _tables(text)
    if not rows:
        return []
    out = []
    vocab = {c: {r[c].strip().lower() for r in rows if r.get(c) and re.fullmatch(
        r"[A-Za-z][A-Za-z ]{1,20}", r[c].strip())} for c in ("difficulty", "length")}
    steps = {}
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?\*\*([A-Za-z0-9][A-Za-z0-9_.-]{0,23})\b", line)
        if m:
            steps.setdefault(m.group(1), line)
    blocks = []
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.S):
        try:
            blocks.append(json.loads(m.group(1)))
        except ValueError:
            continue

    def objects(node, rid):
        if isinstance(node, dict):
            if str(node.get("id") or node.get("run") or "") == rid:
                yield node
            for k, v in node.items():
                if k == rid and isinstance(v, dict):
                    yield v
                yield from objects(v, rid)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v, rid)
    for r in rows:
        rid = r["id"]
        line = steps.get(rid)
        if line:
            body = line.split("**", 2)[-1]           # after the bold id
            for c in ("difficulty", "length"):
                want = (r.get(c) or "").strip().lower()
                if want not in vocab.get(c, ()):
                    continue
                said = [w for w in vocab[c] if re.search(rf"\b{re.escape(w)}\b", body, re.I)]
                if said and want not in said:
                    out.append({"kind": "run", "what": rid, "text":
                                f"{rid}: the table says {c} {r[c].strip()}, its step says "
                                f"{', '.join(sorted(said))}"})
            want = _seed(r.get("seed") or "")
            if want:
                said = re.findall(r"\bseed\b[^,;.]*?(\d{3,})", body, re.I)
                if said and want not in said:
                    out.append({"kind": "run", "what": rid, "text":
                                f"{rid}: the table says seed {want}, its step says {said[0]}"})
        for blk in blocks:
            for obj in objects(blk, rid):
                for c in COLS:
                    if c not in obj or not r.get(c):
                        continue
                    have = str(obj[c]).strip().lower()
                    want = _seed(r[c]) if c == "seed" else r[c].strip().lower()
                    if want and have != want:
                        out.append({"kind": "run", "what": rid, "text":
                                    f"{rid}: the table says {c} {want}, a JSON block says {have}"})
    return out


# ---------------------------------------------------------------------------- the hook

def check(c, P, task_id: str, text: str, own_files: Optional[list] = None) -> list:
    """Every finding for one plan text. Never raises."""
    out = []
    for fn in (lambda: check_paths(text, task_id, P.profile.workspace, own_files or []),
               lambda: check_sprints(c, P, text, task_id), lambda: check_runs(text)):
        try:
            out += fn()
        except Exception as e:                                   # noqa: BLE001
            try:
                c.log(f"{P.id}/{task_id}: plan check part failed: {type(e).__name__}: {e}")
            except Exception:                                    # noqa: BLE001
                pass
    return out[:MAX_FINDINGS * 3]


def plan_text(P, task_id: str) -> tuple:
    """(text, repo-relative path) of the sprint's plan file, or ('', '')."""
    rel = f"docs/progress/{task_id}_PLAN.md"
    text = _read(os.path.join(P.profile.workspace, *rel.split("/")))
    return (text, rel) if text else ("", "")


def at_start(c, P, t: dict, spec=None) -> str:
    """Run the check once per plan version before the sprint's session starts. Returns the
    blocker text when `plan_check` is "block" and something was found, else ''."""
    try:
        mode = str(auto_get(P, "plan_check") or "warn")
    except Exception:                                            # noqa: BLE001
        mode = "warn"
    if mode == "off":
        return ""
    tid = t["task_id"]
    try:
        text, rel = plan_text(P, tid)
        if not text:
            return ""
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        seen = (t.get("data") or {}).get("plan_check") or {}
        if seen.get("hash") == digest:
            found = seen.get("findings") or []
            if mode != "block" or not found or seen.get("blocked"):
                return ""            # nothing found, or it was stopped once and unblocked
            c.store.merge_data(P.id, tid, plan_check={**seen, "blocked": True})
            return _blocker(tid, rel, found)
        files = list(getattr(spec, "scope", None) or [])
        if not files:
            from .tracks import analysis as AN
            files = AN.plan_files(text)
        found = check(c, P, tid, text, files)
        c.store.merge_data(P.id, tid, plan_check={
            "hash": digest, "ts": c.clock(), "blocked": bool(found) and mode == "block",
            "findings": [f["text"] for f in found][:MAX_FINDINGS]})
        if not found:
            return ""
        texts = [f["text"] for f in found]
        c.store.event(P.id, tid, "plan_check", detail={
            "plan": rel, "mode": mode, "findings": texts[:MAX_FINDINGS], "count": len(texts),
            "digest": True,
            "text": f"{rel}: {len(texts)} thing(s) to check: " + "; ".join(texts[:4])})
        return _blocker(tid, rel, texts) if mode == "block" else ""
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"{P.id}/{tid}: plan check failed: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
        return ""


def _blocker(tid: str, rel: str, texts: list) -> str:
    return (f"plan check ({rel}): " + "; ".join(texts[:6])
            + (f"; and {len(texts) - 6} more" if len(texts) > 6 else "")
            + ". Fix the plan, or unblock to start with it as it is.")
