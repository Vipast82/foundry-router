"""Your own benchmark, built from work this router already did (v0.181.167).

A model scores well on public benchmarks and then writes Luau that does not
compile, or calls the wrong tool. What matters here is the work in front of it:
the conversations this router captured, the ones a gate or a review passed.

A **suite** is a set of cases cut from those conversations. Each case is the
conversation up to the point where the model had to act, the tools it was
offered, and what it actually did then (the reference). A **run** sends every
case to one model and scores what comes back:

    tools    did it reach for the same tools, in the same order?
    code     does the code it wrote pass this router's code checks?
    answer   did it answer at all, at a sensible length?

Nothing here is compared against a public leaderboard: the question is only
whether a model does *your* work, and whether a change to the router made it
better or worse. The scores of two runs sit side by side, so "we switched the
route" has an answer.

Suites live in `<data>/bench/<name>.jsonl`; runs and their per-case results are
in the database (`bench_runs`, `bench_results`), so a comparison survives a
restart. Judging by a frontier model comes later: everything here is measured.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

MAX_CASES = 500
MAX_PROMPT_TOKENS = 24_000
CASES_PER_CONVERSATION = 2     # one long session must not become the whole suite
MIN_CONTEXT = 1                # one user turn is context enough: ask, then act
_state: dict = {"state": "idle"}
_lock = threading.Lock()


# -- suites -------------------------------------------------------------------------------

def _dir(svc) -> Path:
    d = Path(svc.capture.data_dir) / "bench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def suites(svc) -> list:
    out = []
    for f in sorted(_dir(svc).glob("*.jsonl")):
        try:
            n = sum(1 for line in f.open(encoding="utf-8") if line.strip())
        except OSError:
            n = 0
        out.append({"name": f.stem, "cases": n, "bytes": f.stat().st_size,
                    "built": f.stat().st_mtime})
    return out


def cases(svc, name: str, limit: int = 0) -> list:
    f = _dir(svc) / f"{name}.jsonl"
    out = []
    try:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
                    if limit and len(out) >= limit:
                        break
    except OSError:
        return []
    return out


def _fit(msgs: list, upto: int) -> Optional[list]:
    """As much of the conversation before `upto` as fits in a prompt, kept whole:
    the system turn, then the newest turns, never starting in the middle of a
    tool exchange (a tool result whose call is not there confuses every model).

    Keeping the newest turns rather than the oldest is what lets a real session
    become a case at all: the sessions here run to hundreds of thousands of
    tokens, and before this only the atypically short ones ever fit (v0.181.172)."""
    from foundry_training import sessions
    head = [m for m in msgs[:upto] if m.get("role") == "system"][:1]
    body = [m for m in msgs[:upto] if m.get("role") != "system"]
    for start in range(len(body)):
        if body[start].get("role") not in ("user", "assistant"):
            continue                           # never open on an orphaned tool result
        if body[start].get("role") == "assistant" and body[start].get("tool_calls"):
            continue                           # ... nor on a call whose result follows
        trial = head + body[start:]
        if len(trial) < MIN_CONTEXT:
            return None
        n, _how = sessions._tokens(json.dumps(trial, default=str))
        if int(n) <= MAX_PROMPT_TOKENS:
            return trial
    return None


def _case_at(example: dict, at: int) -> Optional[dict]:
    """The case whose reference is the assistant turn at `at`."""
    msgs = [m for m in (example.get("messages") or []) if isinstance(m, dict)]
    if at <= 0 or at >= len(msgs):
        return None
    prefix = _fit(msgs, at)
    if not prefix or not any(m.get("role") == "user" for m in prefix):
        return None
    ref = msgs[at]
    meta = example.get("meta") or {}
    return {"id": f"{meta.get('task_id') or 'case'}-{meta.get('session') or ''}-{at}".strip("-"),
            "task_id": meta.get("task_id") or "", "tier": meta.get("tier") or "",
            "tags": meta.get("tags") or [], "models": meta.get("models") or [],
            "messages": prefix, "tools": example.get("tools") or [],
            "reference": {"content": (ref.get("content") or "")[:20000],
                          "tool_calls": [{"name": (tc.get("function") or tc).get("name") or "",
                                          "arguments": (tc.get("function") or tc).get("arguments")}
                                         for tc in (ref.get("tool_calls") or [])]}}


def cases_from(example: dict) -> list:
    """The cases this conversation can answer for, newest first. A turn where the
    assistant REACHED FOR A TOOL is worth most - what tool, in what order, is the
    half of the score that can actually be measured - so those come first, and the
    closing answer is used only to fill up. Before this a suite was cut only at the
    last turn, which is a prose summary, so every case scored as "no tool was
    needed" and the measurement was empty (v0.181.172)."""
    msgs = [m for m in (example.get("messages") or []) if isinstance(m, dict)]
    acted, said = [], []
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        (acted if m.get("tool_calls") else said).append(i)
    out, seen = [], set()
    for at in list(reversed(acted)) + list(reversed(said)):
        if len(out) >= CASES_PER_CONVERSATION:
            break
        c = _case_at(example, at)
        if c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def _case_from(example: dict) -> Optional[dict]:
    """One case from a keep-everything example: the conversation up to the last
    thing the assistant did, and that turn as the reference."""
    msgs = [m for m in (example.get("messages") or []) if isinstance(m, dict)]
    last = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant":
            last = i
            break
    if last is None or last == 0:
        return None
    prefix = msgs[:last]
    if not any(m.get("role") == "user" for m in prefix):
        return None
    ref = msgs[last]
    meta = example.get("meta") or {}
    return {"id": f"{meta.get('task_id') or 'case'}-{meta.get('session') or ''}-{last}".strip("-"),
            "task_id": meta.get("task_id") or "", "tier": meta.get("tier") or "",
            "tags": meta.get("tags") or [], "models": meta.get("models") or [],
            "messages": prefix, "tools": example.get("tools") or [],
            "reference": {"content": (ref.get("content") or "")[:20000],
                          "tool_calls": [{"name": (tc.get("function") or tc).get("name") or "",
                                          "arguments": (tc.get("function") or tc).get("arguments")}
                                         for tc in (ref.get("tool_calls") or [])]}}


def too_long(case: dict) -> bool:
    """A case whose conversation is too big to send. `_tokens` answers
    (count, how it counted) - comparing the pair itself is what broke the first
    real suite build (v0.181.168)."""
    from foundry_training import sessions
    n, _how = sessions._tokens(json.dumps(case.get("messages") or [], default=str))
    return int(n) > MAX_PROMPT_TOKENS


def build(svc, name: str, limit: int = 60, tiers=("verified",), tags=(), log=print) -> dict:
    """Cut a suite out of the prepared store. Newest conversations first, so a
    suite follows what the project is doing now."""
    from foundry_training import sessions
    from foundry_training.store import TrainingStore

    from . import training_prep
    root = training_prep._dirs(svc)["store"]
    if not (root / "normalized.sqlite").exists():
        raise FileNotFoundError("the prepared store is empty — let the router prepare its "
                                "dataset first (Logs → Training → Dataset)")
    store = TrainingStore(str(root), allow_in_repo=True)
    want_tiers, want_tags = {t for t in tiers if t}, {t for t in tags if t}
    picked, seen = [], set()
    for pid in store.projects():
        labels: dict = {}
        for e in store.events(pid, "event_type='router_outcome'"):
            if e.get("request_id"):
                labels.setdefault(e["request_id"], []).append((e.get("facts") or {}).get("label"))
        for _sid, calls in sessions._calls(store, pid):
            for last, inside in sessions.conversations(store, calls):
                ex = sessions.build_example(store, last, inside, labels)
                if not ex:
                    continue
                meta = ex.get("meta") or {}
                if want_tiers and meta.get("tier") not in want_tiers:
                    continue
                if want_tags and not (want_tags & set(meta.get("tags") or [])):
                    continue
                for c in cases_from(ex):
                    if c["id"] in seen or too_long(c):
                        continue
                    seen.add(c["id"])
                    picked.append(c)
    picked.sort(key=lambda c: c.get("task_id") or "", reverse=True)
    picked = picked[:max(1, min(limit, MAX_CASES))]
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for c in picked:
            fh.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    with_tools = sum(1 for c in picked if c["reference"]["tool_calls"])
    log(f"suite {name}: {len(picked)} case(s), {with_tools} of them cut where a tool "
        f"was reached for")
    return {"name": name, "cases": len(picked), "path": str(f),
            "with_tools": sum(1 for c in picked if c["reference"]["tool_calls"]),
            "tags": sorted({t for c in picked for t in c.get("tags") or []})}


# -- scoring ------------------------------------------------------------------------------

def tool_score(expected: list, called: list) -> float:
    """1.0 when the same tools were called in the same order; partial credit for
    the same tools in another order; 0 when none of them were."""
    e = [str(t.get("name") or "") for t in expected if t.get("name")]
    c = [str(t.get("name") or "") for t in called if t.get("name")]
    if not e:
        return 1.0 if not c else 0.5          # nothing was needed; calling anyway is not wrong
    if not c:
        return 0.0
    if e == c:
        return 1.0
    same = len(set(e) & set(c)) / len(set(e))
    order = 1.0 if e[:1] == c[:1] else 0.0     # the first move counts for most
    return round(0.7 * same + 0.3 * order, 3)


async def code_score(svc, text: str) -> tuple:
    """(1.0 / 0.0 / None, detail). None when there was no code to check."""
    from . import code_checks
    cfg = getattr(svc.config_store.config, "code_checks", None)
    if not getattr(cfg, "rules", None) or not code_checks.blocks(text or ""):
        return None, ""
    on = type("C", (), {"enabled": True, "rules": cfg.rules,
                        "max_fixes": 0, "skip_gated_tasks": False})()
    probs = await code_checks.check(on, text)
    return (0.0 if probs else 1.0), code_checks.summary(probs)


def answer_score(text: str, reference: str) -> float:
    t = (text or "").strip()
    if not t:
        return 0.0
    if len(t) < 20:
        return 0.3
    ref = len(reference or "")
    if ref and (len(t) < ref / 8 or len(t) > ref * 8):
        return 0.6                             # wildly shorter or longer than what was done
    return 1.0


def combine(tools: Optional[float], code: Optional[float], answer: Optional[float]) -> float:
    parts = [(tools, 0.5), (code, 0.3), (answer, 0.2)]
    have = [(v, w) for v, w in parts if v is not None]
    if not have:
        return 0.0
    return round(100 * sum(v * w for v, w in have) / sum(w for _v, w in have), 1)

# -- running a suite ----------------------------------------------------------------------

async def ask(svc, model: str, case: dict, max_tokens: int = 2048) -> dict:
    """One case through one model, with the tools that case was offered."""
    t0 = time.monotonic()
    try:
        result, backend = await svc.pool.chat(
            model, list(case.get("messages") or []),
            tools=list(case.get("tools") or []) or None, max_tokens=max_tokens)
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        return {"error": describe_exception(e), "seconds": round(time.monotonic() - t0, 2)}
    from .brain import prompts
    _think, clean = prompts.split_think(result.content or "")
    return {"content": clean.strip(), "backend": backend,
            "tool_calls": [{"name": tc.get("name"), "arguments": tc.get("arguments")}
                           for tc in (result.tool_calls or [])],
            "prompt_tokens": result.prompt_tokens or 0,
            "completion_tokens": result.completion_tokens or 0,
            "seconds": round(time.monotonic() - t0, 2)}


async def score_one(svc, case: dict, got: dict) -> dict:
    ref = case.get("reference") or {}
    if got.get("error"):
        return {"score": 0.0, "tools": 0.0, "code": None, "answer": 0.0,
                "code_detail": "", "error": got["error"]}
    tools = tool_score(ref.get("tool_calls") or [], got.get("tool_calls") or [])
    code, detail = await code_score(svc, got.get("content") or "")
    answer = (answer_score(got.get("content") or "", ref.get("content") or "")
              if not (ref.get("tool_calls") or []) else None)
    return {"score": combine(tools, code, answer), "tools": tools, "code": code,
            "answer": answer, "code_detail": detail, "error": ""}


def _row(db, sql, args=()):
    db.execute(sql, args)


async def _run(svc, suite: str, model: str, label: str, limit: int) -> dict:
    db = svc.db
    items = cases(svc, suite, limit)
    if not items:
        raise ValueError(f"the suite {suite!r} has no cases")
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status) "
               "VALUES (datetime('now'), ?, ?, ?, ?, 0, 'running')",
               (suite, model, label, len(items)))
    run_id = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    _state.update({"state": "running", "run_id": run_id, "suite": suite, "model": model,
                   "cases": len(items), "done": 0, "started": time.time(), "score": None})
    totals = {"score": 0.0, "tools": 0.0, "code": 0.0, "answer": 0.0, "seconds": 0.0,
              "prompt": 0, "completion": 0, "code_n": 0, "answer_n": 0, "errors": 0}
    for i, case in enumerate(items, start=1):
        if _state.get("stop"):
            break
        got = await ask(svc, model, case)
        s = await score_one(svc, case, got)
        db.execute(
            "INSERT INTO bench_results (run_id, case_id, task_id, tags, tools_expected, "
            "tools_called, tools_score, code_pass, code_detail, answer_chars, score, seconds, "
            "error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, case.get("id") or "", case.get("task_id") or "",
             json.dumps(case.get("tags") or []),
             json.dumps([t.get("name") for t in (case.get("reference") or {}).get("tool_calls") or []]),
             json.dumps([t.get("name") for t in got.get("tool_calls") or []]),
             s["tools"], None if s["code"] is None else int(s["code"] >= 1), s["code_detail"],
             len(got.get("content") or ""), s["score"], got.get("seconds") or 0, s["error"]))
        totals["score"] += s["score"]
        totals["tools"] += s["tools"]
        if s["code"] is not None:
            totals["code"] += s["code"]
            totals["code_n"] += 1
        if s["answer"] is not None:
            totals["answer"] += s["answer"]
            totals["answer_n"] += 1
        totals["seconds"] += got.get("seconds") or 0
        totals["prompt"] += got.get("prompt_tokens") or 0
        totals["completion"] += got.get("completion_tokens") or 0
        totals["errors"] += 1 if s["error"] else 0
        _state["done"] = i
        _state["score"] = round(totals["score"] / i, 1)
    n = max(1, _state["done"])
    out = {"run_id": run_id, "suite": suite, "model": model, "cases": len(items), "scored": _state["done"],
           "score": round(totals["score"] / n, 1), "tools": round(100 * totals["tools"] / n, 1),
           "code": round(100 * totals["code"] / totals["code_n"], 1) if totals["code_n"] else None,
           "answer": round(100 * totals["answer"] / totals["answer_n"], 1) if totals["answer_n"] else None,
           "seconds": round(totals["seconds"] / n, 2), "prompt_tokens": totals["prompt"],
           "completion_tokens": totals["completion"], "errors": totals["errors"]}
    db.execute("UPDATE bench_runs SET scored=?, tools_score=?, code_score=?, answer_score=?, "
               "score=?, seconds=?, prompt_tokens=?, completion_tokens=?, status=?, note=? "
               "WHERE id=?",
               (out["scored"], out["tools"], out["code"], out["answer"], out["score"],
                out["seconds"], out["prompt_tokens"], out["completion_tokens"],
                "stopped" if _state.get("stop") else "done",
                f"{out['errors']} call(s) failed" if out["errors"] else "", run_id))
    _state.update({"state": "done", "finished": time.time(), **out})
    return out


def start(svc, suite: str, model: str, label: str = "", limit: int = 0) -> dict:
    """Run a suite in the background. One at a time."""
    if _state.get("state") == "running":
        return dict(_state)
    if not _lock.acquire(blocking=False):
        return dict(_state)
    _state.clear()
    _state.update({"state": "running", "suite": suite, "model": model, "done": 0})

    def go():
        try:
            asyncio.run(_run(svc, suite, model, label, limit))
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            _state.update({"state": "failed", "error": describe_exception(e),
                           "finished": time.time()})
        finally:
            _lock.release()
    threading.Thread(target=go, name="foundry-bench", daemon=True).start()
    time.sleep(0.05)
    return dict(_state)


def stop() -> dict:
    if _state.get("state") == "running":
        _state["stop"] = True
    return dict(_state)


def status(svc) -> dict:
    return {"run": dict(_state), "suites": suites(svc)}


def runs(db, limit: int = 30) -> list:
    return db.query("SELECT * FROM bench_runs ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),))


def results(db, run_id: int, limit: int = 500) -> list:
    return db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id LIMIT ?",
                    (run_id, max(1, min(2000, limit))))


def compare(db, a: int, b: int) -> dict:
    """Two runs, case by case: what got better, what got worse."""
    ra = {r["case_id"]: r for r in results(db, a)}
    rb = {r["case_id"]: r for r in results(db, b)}
    rows = []
    for cid in sorted(set(ra) & set(rb)):
        x, y = ra[cid], rb[cid]
        rows.append({"case_id": cid, "task_id": x.get("task_id") or "",
                     "a": x.get("score") or 0, "b": y.get("score") or 0,
                     "change": round((y.get("score") or 0) - (x.get("score") or 0), 1)})
    rows.sort(key=lambda r: r["change"])
    head = {r["id"]: r for r in db.query(
        "SELECT * FROM bench_runs WHERE id IN (?,?)", (a, b))}
    return {"a": head.get(a), "b": head.get(b), "shared": len(rows),
            "better": sum(1 for r in rows if r["change"] > 0),
            "worse": sum(1 for r in rows if r["change"] < 0),
            "cases": rows[:40] + ([] if len(rows) <= 80 else []) + rows[-40:] if len(rows) > 80 else rows}
