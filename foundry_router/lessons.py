"""Lessons: the mistakes this project keeps making, turned into rules it is told.

Item 3 of the "useful without fine-tuning" plan, and the other half of recall
(`recall.py`). Recall carries the work that passed; this carries what went
wrong, because a reviewer's finding is only worth something the second time if
nobody has to find it again.

Every finding the work earned is collected from the prepared store - a check
that failed, a reviewer's text, a vision pass asking for changes, a human
rejecting the work - and they are **clustered by meaning**, not by wording.
Two reviewers writing "keyboard copy is shown at touch sizes" and "Settings
shows key bindings in the pad frames" are the same lesson twice, and no regular
expression will ever say so. The embedding endpoint the semantic cache already
uses says so.

A cluster that bit enough separate pieces of work becomes a **candidate**: how
often it happened, which tasks it bit, and the real findings behind it. A model
may draft the one line that would have prevented it. Nothing is ever applied on
its own: the operator reads the candidate, writes or edits the rule, and
approves it. Only then does it go into a prompt, and only for a persona that
asks for it.

    proposed  ->  active     the rule goes into the prompt
              ->  rejected   never offered again

Mining again updates how often a lesson has happened and never touches what the
operator decided about it.

Settings: `lessons.*`. Off until it is switched on and a rule is approved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Optional

from .db import utcnow
from .semcache import _normalize, _pack, _unpack

log = logging.getLogger(__name__)

MAX_FINDINGS = 20_000          # newest first; a hard ceiling whatever the setting says
# A "mistake" that covers a third of every finding ever written is not a rule anyone
# can follow, it is a category. Such a cluster is reported and left out (v0.181.176).
MAX_CLUSTER_SHARE = 0.15
MIN_CHARS = 40                 # shorter than this says nothing a rule could prevent
TEXT_CHARS = 1200              # of one finding kept and embedded
_state: dict = {"state": "idle"}
_task: dict = {"t": None}

# a model that must never word a rule, judge, or help (operator's standing rule)
NEVER_DRAFT = re.compile(r"(?i)\b(fable|mythos)\b")

DRAFT_PROMPT = """Below are findings a reviewer, a gate or a person wrote about one
recurring mistake in this project. Write the ONE line that, had the model been told it
beforehand, would have prevented the mistake.

Rules for your answer:
- one sentence, imperative, at most 200 characters;
- about what to do, not about what went wrong;
- concrete enough to act on, general enough to apply next time;
- no preamble, no quotes, no markdown - the sentence only.

The findings:
{findings}"""


# -- the findings ------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:TEXT_CHARS]


def _id(*parts) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()[:24]


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(ts or 0), timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _from_review(facts: dict) -> list:
    """A frontier review's findings, with their severity."""
    out = []
    res = facts.get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except (json.JSONDecodeError, TypeError):
            res = {}
    if not isinstance(res, dict):
        return out
    for key in ("findings", "blockers", "major", "minor", "info"):
        items = res.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                text = it.get("text") or it.get("finding") or it.get("detail") or ""
                sev = str(it.get("severity") or it.get("level") or key)
            else:
                text, sev = it, key
            text = _clean(text)
            if len(text) >= MIN_CHARS:
                out.append((text, sev))
    return out


def findings(store, pid: str) -> list:
    """Every finding of one project, oldest first. Each is what somebody said was
    wrong, and where it came from."""
    out = []

    def add(ev, text, src, sev=""):
        text = _clean(text)
        if len(text) < MIN_CHARS:
            return
        out.append({"id": _id(src, ev.get("task_id"), text), "text": text, "source": src,
                    "severity": str(sev or "")[:40], "task_id": ev.get("task_id") or "",
                    "ts": float(ev.get("timestamp") or 0)})

    for ev in store.events(pid, "event_type='verification' AND verification_status='FAIL'"):
        f = ev.get("facts") or {}
        add(ev, f"{f.get('check') or ''}: {f.get('detail') or ''}", "check")
    for ev in store.events(pid, "event_type='review'"):
        f = ev.get("facts") or {}
        if str(f.get("verdict") or "").upper() in ("APPROVED", "APPROVE", "PASS"):
            continue
        for text, sev in _from_review(f):
            add(ev, text, "review", sev)
    for ev in store.events(pid, "event_type='human_correction'"):
        f = ev.get("facts") or {}
        for item in (f.get("findings") or []):
            add(ev, item.get("text") if isinstance(item, dict) else item, "human")
    for ev in store.events(pid, "event_type='label'"):
        f = ev.get("facts") or {}
        if f.get("label") == "gate_verdict":
            for ch in (f.get("failed") or []):
                if isinstance(ch, dict):
                    add(ev, f"{ch.get('name') or ch.get('check') or ''}: "
                            f"{ch.get('detail') or ch.get('reason') or ''}", "gate")
                else:
                    add(ev, ch, "gate")
        elif f.get("label") in ("vision_verdict", "critique"):
            if str(f.get("verdict") or "").upper() in ("APPROVED", "APPROVE"):
                continue
            for item in (f.get("findings") or []):
                add(ev, item.get("text") if isinstance(item, dict) else item,
                    "vision" if f.get("label") == "vision_verdict" else "critique")
    out.sort(key=lambda r: r["ts"])
    return out


def collect(svc, limit: int, log_fn=print) -> list:
    """The newest findings across every project in the prepared store. Reads
    SQLite, so it is called in a thread."""
    from foundry_training.store import TrainingStore

    from . import training_prep
    root = training_prep._dirs(svc)["store"]
    if not (root / "normalized.sqlite").exists():
        raise FileNotFoundError("the prepared store is empty - let the router prepare its "
                                "dataset first (Logs → Training → Dataset)")
    store = TrainingStore(str(root), allow_in_repo=True)
    out, seen = [], set()
    for pid in store.projects():
        for f in findings(store, pid):
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            out.append(f)
    out.sort(key=lambda r: r["ts"], reverse=True)
    log_fn(f"lessons: {len(out)} finding(s) in the store")
    return out[:limit]


# -- clustering by meaning ---------------------------------------------------------------

def _cos(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def cluster(rows: list, floor: float) -> list:
    """Leader clustering on L2-normalized vectors: a finding joins the cluster whose
    ANCHOR it is closest to, if it is at least `floor` close to it, else it starts
    its own. Newest first on the way in, so a cluster is named after its newest
    finding.

    The anchor never moves, and that is the whole point. Averaging the members into
    a centre and comparing against that lets a cluster walk across the space - each
    addition tugs the centre a little further - until it absorbs everything nearby.
    On the first real run that put 3,577 of 8,000 findings in one "mistake" and
    7,606 of them in twelve, which is not a rulebook, it is a shrug (v0.181.176).
    Against a fixed anchor, "within 0.80" means the same thing for the first member
    and the thousandth."""
    out: list = []
    for r in rows:
        vec = r["vec"]
        best, best_sim = None, floor
        for c in out:
            sim = _cos(vec, c["anchor_vec"])
            if sim >= best_sim:
                best, best_sim = c, sim
        if best is None:
            out.append({"anchor": r["id"], "anchor_vec": list(vec), "members": [r]})
        else:
            best["members"].append(r)
    return out


# -- candidates --------------------------------------------------------------------------

def _cfg(svc):
    return getattr(svc.config_store.config, "lessons", None)


def _upsert(db, c: dict) -> str:
    """A cluster as a candidate. An existing one keeps whatever the operator
    decided and whatever wording it has; only what was measured is refreshed."""
    members = sorted(c["members"], key=lambda m: m["ts"])
    tasks = sorted({m["task_id"] for m in members if m["task_id"]})
    sources = sorted({m["source"] for m in members})
    examples = [m["text"] for m in members[-6:]]
    row = db.query_one("SELECT id, state FROM lessons WHERE anchor=?", (c["anchor"],))
    fields = (len(members), json.dumps(tasks), json.dumps(sources), json.dumps(examples),
              _iso(members[0]["ts"]), _iso(members[-1]["ts"]))
    if row:
        db.execute("UPDATE lessons SET seen=?, tasks=?, sources=?, examples=?, first_seen=?, "
                   "last_seen=? WHERE id=?", (*fields, row["id"]))
        return row["state"]
    db.execute("INSERT INTO lessons (ts, anchor, rule, draft, seen, tasks, sources, examples, "
               "first_seen, last_seen, state) VALUES (?,?,'','',?,?,?,?,?,?,'proposed')",
               (utcnow(), c["anchor"], *fields))
    return "new"


def status(svc) -> dict:
    cfg = _cfg(svc)
    by = {r["state"]: r["n"] for r in svc.db.query(
        "SELECT state, COUNT(*) AS n FROM lessons GROUP BY state")}
    cached = svc.db.query_one("SELECT COUNT(*) AS n FROM lesson_findings") or {}
    return {"by_state": by, "proposed": by.get("proposed", 0), "active": by.get("active", 0),
            "rejected": by.get("rejected", 0), "findings_cached": cached.get("n") or 0,
            "enabled": bool(getattr(cfg, "enabled", False)),
            "min_seen": int(getattr(cfg, "min_seen", 3) or 3),
            "min_tasks": int(getattr(cfg, "min_tasks", 2) or 2),
            "cluster_similarity": float(getattr(cfg, "cluster_similarity", 0.8) or 0),
            "max_findings": int(getattr(cfg, "max_findings", 8000) or 0),
            "max_chars": int(getattr(cfg, "max_chars", 1500) or 0),
            "hours": int(getattr(cfg, "mine_hours", 0) or 0),
            "draft_model": getattr(cfg, "draft_model", "") or "",
            "run": dict(_state)}


def running() -> bool:
    t = _task["t"]
    return t is not None and not t.done()


def due(svc, now: Optional[float] = None) -> bool:
    """A scheduled pass only ever adds candidates to the queue; it can neither
    approve nor change a rule, so leaving it on is safe."""
    cfg = _cfg(svc)
    if not getattr(cfg, "enabled", False) or running():
        return False
    hours = int(getattr(cfg, "mine_hours", 0) or 0)
    if hours <= 0 or _state.get("state") == "running":
        return False
    return (now or time.time()) - float(_state.get("finished") or 0) >= hours * 3600


def lessons(db, state: str = "", limit: int = 200) -> list:
    where, args = ("WHERE state=?", (state,)) if state else ("", ())
    out = []
    for r in db.query(f"SELECT * FROM lessons {where} ORDER BY seen DESC, last_seen DESC "
                      f"LIMIT {max(1, min(limit, 1000))}", args):
        for k in ("tasks", "sources", "examples"):
            try:
                r[k] = json.loads(r.get(k) or "[]")
            except (json.JSONDecodeError, TypeError):
                r[k] = []
        out.append(r)
    return out


async def mine(svc, log_fn=None) -> dict:
    """Collect the findings, embed the ones not embedded yet, cluster them, and
    write the candidates. Everything the operator decided is left alone."""
    say = log_fn or (lambda m: svc.db.log_event("info", "lessons", m))
    cfg = _cfg(svc)
    limit = max(10, min(int(getattr(cfg, "max_findings", 8000) or 8000), MAX_FINDINGS))
    floor = float(getattr(cfg, "cluster_similarity", 0.8) or 0.8)
    min_seen = max(1, int(getattr(cfg, "min_seen", 3) or 3))
    min_tasks = max(1, int(getattr(cfg, "min_tasks", 2) or 2))
    _state.update({"state": "running", "started": time.time(), "findings": 0, "embedded": 0,
                   "clusters": 0, "new": 0, "error": "", "stop": False})
    try:
        rows = await asyncio.to_thread(collect, svc, limit, say)
        _state["findings"] = len(rows)
        # A finding carries an envelope of its own: every vision finding opens with
        # the model and persona that wrote it and the path of the frame it looked at.
        # Embedding that makes thousands of unrelated findings look alike - the same
        # mistake recall made (v0.181.176), so the same machinery fixes it.
        from .recall import common_lines, gist
        common = common_lines([r["text"] for r in rows])
        for r in rows:
            r["gist"] = gist(r["text"], common)
        _state["boilerplate"] = len(common)
        say(f"lessons: {len(common)} boilerplate phrase(s) dropped before embedding")
        have = {r["finding_id"]: r for r in svc.db.query(
            "SELECT finding_id, embedding, gist FROM lesson_findings")}
        for r in rows:
            if _state.get("stop"):
                break
            hit = have.get(r["id"])
            if hit and (hit.get("gist") or "") == r["gist"]:
                r["vec"] = _unpack(hit["embedding"])
                continue            # same text, same stripping: the vector still stands
            vec = await svc.semcache.embed(r["gist"])
            if vec is None:
                if not _state["embedded"]:
                    raise RuntimeError("the embedding endpoint is not answering - set it up "
                                       "under Routing → Semantic cache first")
                continue
            r["vec"] = vec
            svc.db.execute(
                "INSERT OR REPLACE INTO lesson_findings (finding_id, ts, source, severity, "
                "task_id, text, gist, embedding, dim) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["id"], _iso(r["ts"]), r["source"], r["severity"], r["task_id"], r["text"],
                 r["gist"], _pack(vec), len(vec)))
            _state["embedded"] += 1
            if _state["embedded"] % 25 == 0:
                await asyncio.sleep(0)
        rows = [r for r in rows if r.get("vec")]
        clusters = await asyncio.to_thread(cluster, rows, floor)
        _state["clusters"] = len(clusters)
        new, huge = 0, 0
        ceiling = max(min_seen, int(len(rows) * MAX_CLUSTER_SHARE))
        for c in clusters:
            tasks = {m["task_id"] for m in c["members"] if m["task_id"]}
            if len(c["members"]) < min_seen or len(tasks) < min_tasks:
                continue
            if len(c["members"]) > ceiling:
                huge += 1
                continue            # a category, not a mistake: no rule could cover it
            if _upsert(svc.db, c) == "new":
                new += 1
        _state["too_broad"] = huge
        _state.update({"state": "done", "finished": time.time(), "new": new, "stop": False})
        say(f"lessons: {len(clusters)} cluster(s) from {len(rows)} finding(s), "
            f"{new} new candidate(s) waiting for you"
            + (f"; {huge} group(s) too broad to be a rule were left out" if huge else ""))
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        _state.update({"state": "failed", "finished": time.time(),
                       "error": describe_exception(e), "stop": False})
        say(f"lessons mining failed: {describe_exception(e)}")
    return dict(_state)


def start(svc) -> dict:
    if running():
        return dict(_state)
    _state["stop"] = False
    _task["t"] = asyncio.create_task(mine(svc))
    return {"state": "running", "started": time.time()}


def stop() -> dict:
    _state["stop"] = True
    return dict(_state)


# -- wording, and the operator's decision ------------------------------------------------

async def draft(svc, lesson_id: int, model: str = "") -> dict:
    """Ask a model for the one line that would have prevented this. The wording
    is a suggestion in its own field; the rule stays empty until the operator
    writes or accepts one."""
    cfg = _cfg(svc)
    model = (model or getattr(cfg, "draft_model", "") or "").strip()
    if not model:
        return {"ok": False, "error": "no drafting model is set (Lessons -> draft with)"}
    if NEVER_DRAFT.search(model):
        return {"ok": False, "error": f"{model} may not word a rule - a Fable-class model is "
                                      f"never used as a helper or a judge here"}
    row = svc.db.query_one("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not row:
        return {"ok": False, "error": "no such lesson"}
    try:
        examples = json.loads(row.get("examples") or "[]")
    except (json.JSONDecodeError, TypeError):
        examples = []
    if not examples:
        return {"ok": False, "error": "this lesson has no findings behind it"}
    text = DRAFT_PROMPT.format(findings="\n".join(f"- {e}" for e in examples[:6]))
    try:
        result, backend = await svc.pool.chat(model, [{"role": "user", "content": text}],
                                              max_tokens=300)
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        return {"ok": False, "error": describe_exception(e)}
    from .brain import prompts
    _think, clean = prompts.split_think(result.content or "")
    first = clean.strip().splitlines()[0] if clean.strip() else ""
    line = _clean(first.strip().strip('"'))[:300]
    if not line:
        return {"ok": False, "error": f"{model} returned nothing to use"}
    svc.db.execute("UPDATE lessons SET draft=? WHERE id=?", (line, lesson_id))
    return {"ok": True, "draft": line, "model": model, "backend": backend}


def decide(svc, lesson_id: int, state: str, rule: str = "", who: str = "operator") -> dict:
    """Approve (with the wording that goes into the prompt), reject, or put a
    lesson back. A rule is never active without wording."""
    if state not in ("active", "rejected", "proposed"):
        raise ValueError("a lesson is proposed, active or rejected")
    row = svc.db.query_one("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not row:
        raise ValueError("no such lesson")
    rule = _clean(rule or row.get("rule") or "")[:300]
    if state == "active" and not rule:
        raise ValueError("write the rule that goes into the prompt before approving it")
    svc.db.execute("UPDATE lessons SET state=?, rule=?, decided_by=?, decided_at=? WHERE id=?",
                   (state, rule, who, utcnow(), lesson_id))
    svc.db.log_event("info", "lessons", f"lesson {lesson_id} {state} by {who}"
                     + (f": {rule}" if state == "active" else ""))
    return {"ok": True, "id": lesson_id, "state": state, "rule": rule}


def forget(svc, lesson_id: int) -> int:
    return svc.db.execute_count("DELETE FROM lessons WHERE id=?", (lesson_id,))


# -- what reaches the model --------------------------------------------------------------

def on_for(cfg, persona: Optional[dict]) -> bool:
    if not getattr(cfg, "enabled", False):
        return False
    return bool((persona or {}).get("lessons", 0))


def block(db, max_chars: int = 1500) -> str:
    """The approved rulebook, most-seen first, as it goes into the prompt."""
    rows = db.query("SELECT rule, seen FROM lessons WHERE state='active' AND rule<>'' "
                    "ORDER BY seen DESC, id ASC")
    if not rows:
        return ""
    head = ("Rules this project learned the hard way. Each one is a mistake that was made "
            "here more than once and was approved as a rule by the operator - follow them.")
    parts, used = [head], len(head)
    for i, r in enumerate(rows, 1):
        line = f"{i}. {r['rule']}"
        if used + len(line) + 1 > max_chars:
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts)
