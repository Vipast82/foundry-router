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
import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("foundry.bench")

MAX_CASES = 500
MAX_PROMPT_TOKENS = 24_000
CASES_PER_CONVERSATION = 2     # one long session must not become the whole suite
# A benchmark that cannot repeat itself cannot measure anything. Two runs of the same
# suite against the same model, with nothing changed, came back 65.5 and 75.0 - a spread
# of 9.5 points, nearly twice the 5.2 that turning recall on appeared to be worth. The
# cause was simply that nothing pinned the sampling down (v0.181.175).
SAMPLING = {"temperature": 0.0, "top_p": 1.0, "top_k": 1, "seed": 7, "repeat_penalty": 1.0}
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


def _examples(store, sessions, want_tiers: set):
    """Every conversation in the prepared store that meets the tier filter.

    Shared by `build` and `tag_counts`: a second copy of this walk would be a
    second thing to keep in step (v0.181.200)."""
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
                if want_tiers and (ex.get("meta") or {}).get("tier") not in want_tiers:
                    continue
                yield ex


_TAGS_CACHE: dict = {}


def tag_counts(svc, tiers=("verified",), refresh: bool = False) -> dict:
    """{tag: how many cases a suite filtered to it would hold}, so the console
    can offer what the corpus actually has instead of a free-text box.

    Cached against the store's mtime: this is the same full walk `build` does,
    which took over two minutes on the live corpus, and the answer only changes
    when the dataset is prepared again (v0.181.202)."""
    from foundry_training import sessions
    from foundry_training.store import TrainingStore

    from . import training_prep
    root = training_prep._dirs(svc)["store"]
    db_file = root / "normalized.sqlite"
    if not db_file.exists():
        return {"tags": [], "cases": 0,
                "note": "the prepared store is empty — prepare the dataset first"}
    try:
        key = (str(db_file), db_file.stat().st_mtime_ns, tuple(sorted(tiers or ())))
    except OSError:
        key = None
    if key and not refresh and key in _TAGS_CACHE:
        return {**_TAGS_CACHE[key], "cached": True}
    store = TrainingStore(str(root), allow_in_repo=True)
    counts: dict = {}
    seen, total = set(), 0
    for ex in _examples(store, sessions, {t for t in tiers if t}):
        fresh = [c for c in cases_from(ex)
                 if c["id"] not in seen and not too_long(c)]
        for c in fresh:
            seen.add(c["id"])
        if not fresh:
            continue
        total += len(fresh)
        for t in (ex.get("meta") or {}).get("tags") or []:
            counts[str(t)] = counts.get(str(t), 0) + len(fresh)
    out = {"cases": total,
           "tags": [{"tag": k, "cases": v}
                    for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]}
    if key:
        _TAGS_CACHE.clear()          # one dataset at a time; never a growing map
        _TAGS_CACHE[key] = out
    return {**out, "cached": False}


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
    for ex in _examples(store, sessions, want_tiers):
        meta = ex.get("meta") or {}
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


def _norm(s: str) -> str:
    """For comparing an answer with something it had to contain: case and runs of
    whitespace do not count, and neither does a thousands comma INSIDE a number -
    a model answering "481,902" has found 481902. Commas elsewhere are left
    alone, so prose is not silently joined into a different string."""
    import re as _re
    out = _re.sub(r"(?<=\d),(?=\d)", "", str(s or ""))
    return _re.sub(r"\s+", " ", out).strip().lower()


def exact_score(text: str, must_include) -> Optional[float]:
    """The share of the strings a case REQUIRES that are in the answer, or None
    when the case requires none (v0.181.199).

    This is what a retrieval case is scored on. `answer_score` cannot do it: it
    is a length heuristic and returns 1.0 for any answer over twenty characters.
    """
    want = [w for w in (must_include or []) if str(w or "").strip()]
    if not want:
        return None
    body = _norm(text)
    if not body:
        return 0.0
    hit = sum(1 for w in want if _contains(body, _norm(w)))
    return round(hit / len(want), 3)


def _contains(body: str, want: str) -> bool:
    """`want` in `body`, but not as part of a longer run of the same character
    class: 481902 is not found inside 4819021. A phrase with punctuation or
    spaces in it is matched plainly, since its own edges are the boundary."""
    import re as _re
    if not want:
        return False
    if want.isalnum():
        return bool(_re.search(rf"(?<![0-9a-z]){_re.escape(want)}(?![0-9a-z])", body))
    return want in body


def _literals_in(text: str) -> list:
    """Candidate Python literals inside an answer, longest first.

    A model may reply with the value alone or wrap it in a sentence; both are
    correct answers to "reply with the value"."""
    t = (text or "").strip()
    out = [t]
    # fenced or inline code first: a model often puts the value in backticks
    import re as _re
    for m in _re.finditer(r"`+([^`]+)`+", t):
        out.append(m.group(1).strip())
    # the longest balanced bracket run, and any quoted run
    for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
        i, j = t.find(opener), t.rfind(closer)
        if 0 <= i < j:
            out.append(t[i:j + 1])
    for m in _re.finditer(r"('[^']*'|\"[^\"]*\")", t):
        out.append(m.group(1))
    # a bare trailing token ("the value is 4.")
    tail = t.rstrip(".").split()
    if tail:
        out.append(tail[-1].strip(".,;:"))
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return sorted(uniq, key=len, reverse=True)


def value_score(text: str, want_repr: str) -> Optional[float]:
    """1.0 when the answer IS the expected Python value, however it is written.

    `[2,2,2]` and `[2, 2, 2]` are the same answer; so are `ababab` and
    `'ababab'` for a string. Comparing the repr by substring failed all three
    (v0.181.204)."""
    import ast
    if not want_repr:
        return None
    try:
        want = ast.literal_eval(want_repr)
    except (ValueError, SyntaxError):
        return None
    if not (text or "").strip():
        return 0.0
    for cand in _literals_in(text):
        try:
            got = ast.literal_eval(cand)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if type(got) is type(want) and got == want:
            return 1.0
        # True == 1 in Python; a benchmark must not accept one for the other
        if got == want and isinstance(got, bool) == isinstance(want, bool):
            return 1.0
    # a string answer may reasonably be written without its quotes
    if isinstance(want, str):
        body = _norm(text)
        if body == _norm(want) or _contains(body, _norm(want)):
            return 1.0
    return 0.0


def combine(tools: Optional[float], code: Optional[float], answer: Optional[float],
            exact: Optional[float] = None) -> float:
    """0-100 for one case.

    A case that REQUIRES certain strings is scored on those alone. Folding a
    retrieval test in with a tool score would hide the result behind a 0.5 the
    case never earned: a needle case expects no tool call, and `tool_score`
    answers 1.0 when none was needed and none was made, which would put the
    floor at 50 for a model that found nothing (v0.181.199).
    """
    if exact is not None:
        return round(100 * exact, 1)
    parts = [(tools, 0.5), (code, 0.3), (answer, 0.2)]
    have = [(v, w) for v, w in parts if v is not None]
    if not have:
        return 0.0
    return round(100 * sum(v * w for v, w in have) / sum(w for _v, w in have), 1)

# -- the needle suite ---------------------------------------------------------------
#
# A context-search benchmark, built from this project's own writing rather than
# synthetic filler: the haystack is the release notes and whatever Luau the
# repository holds, so the text has the shape of the work. The needle is a fact
# that cannot be guessed or recalled from training - a six-digit code tied to an
# invented sprint id - placed at a known DEPTH through a context of a known
# LENGTH, so the result says WHERE a model stops finding things instead of
# giving one number (v0.181.199).

NEEDLE_DEPTHS = (0, 25, 50, 75, 100)
NEEDLE_LENGTHS = (4000, 16000, 32000)      # characters of haystack, roughly 1k-8k tokens
_NEEDLE_SENTENCE = ("Operational note: the verification code for sprint {task} is "
                    "{code}. Quote it exactly when asked.")
_NEEDLE_QUESTION = ("Read the notes above. What is the verification code for sprint "
                    "{task}? Answer with the code and nothing else.")


def _haystack_text(svc) -> str:
    """Prose and code from this project, for filler that reads like the work."""
    import pathlib
    here = pathlib.Path(__file__).resolve().parent.parent
    parts: list = []
    for rel in ("docs/RELEASE_NOTES.md", "README.md"):
        p = here / rel
        try:
            if p.is_file():
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    # NOT a recursive glob: this repository lives under a synced OneDrive tree
    # with the whole AnimalUprising checkout beside it, and `**/*.luau` walks all
    # of it - it hung for over two minutes the first time (v0.181.199). Named
    # directories, shallow, capped.
    for rel in ("foundry_router", "foundry_coordinator", "docs"):
        d = here / rel
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py"))[:25] + sorted(d.glob("*.md"))[:15]:
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            if sum(len(x) for x in parts) > 400_000:
                break
    body = "\n\n".join(parts).strip()
    if len(body) < 20000:
        # never fail for want of filler: repeat what there is rather than invent
        body = (body + "\n\n") * (1 + 20000 // max(1, len(body)))
    return body


def _needle_case(hay: str, length: int, depth: int, seq: int) -> dict:
    """One case: the sentence placed `depth` percent through `length` of filler."""
    import random as _rnd
    rnd = _rnd.Random(f"needle-{length}-{depth}-{seq}")
    code = f"{rnd.randint(100000, 999999)}"
    task = f"{rnd.choice(('VR-AS', 'LOB', 'FIN', 'MV2', 'POL'))}-{rnd.randint(10, 99)}"
    sentence = _NEEDLE_SENTENCE.format(task=task, code=code)
    start = rnd.randrange(0, max(1, len(hay) - length - 1))
    filler = hay[start:start + length]
    at = int(len(filler) * min(100, max(0, depth)) / 100)
    # land on a line boundary so the sentence is not spliced into a word
    nl = filler.find("\n", at)
    at = nl + 1 if 0 <= nl <= at + 400 else at
    body = filler[:at] + "\n" + sentence + "\n" + filler[at:]
    return {
        "id": f"NEEDLE-{length//1000}k-d{depth}-{seq}",
        "task_id": task,
        "tags": ["needle", "context", f"depth:{depth}", f"chars:{length}"],
        "messages": [
            {"role": "system", "content": "You answer from the notes you are given."},
            {"role": "user", "content": body + "\n\n" +
             _NEEDLE_QUESTION.format(task=task)},
        ],
        # scored on this alone: see combine(). The code is unguessable, so a model
        # that did not find it cannot score by writing something plausible.
        "reference": {"content": code, "must_include": [code]},
    }


def build_needle(svc, name: str = "needle", per_cell: int = 3,
                 depths=NEEDLE_DEPTHS, lengths=NEEDLE_LENGTHS) -> dict:
    """Write a needle-in-a-haystack suite: `per_cell` cases at each depth and
    length. Returns {name, cases, path}."""
    hay = _haystack_text(svc)
    out = []
    for length in lengths:
        for depth in depths:
            for seq in range(int(per_cell)):
                out.append(_needle_case(hay, int(length), int(depth), seq))
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in out:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    try:
        svc.db.log_event("info", "bench",
                         f"built the {name} suite: {len(out)} cases, "
                         f"{len(lengths)} length(s) x {len(depths)} depth(s)")
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(out), "path": str(f)}


def needle_breakdown(db, run_id: int) -> list:
    """Score per depth and length for one run, which is the whole point of the
    suite: one number cannot say where a model stops finding things."""
    rows = db.query("SELECT tags, score FROM bench_results WHERE run_id=?", (run_id,))
    cells: dict = {}
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except (ValueError, TypeError):
            continue
        depth = next((t.split(":", 1)[1] for t in tags if t.startswith("depth:")), None)
        chars = next((t.split(":", 1)[1] for t in tags if t.startswith("chars:")), None)
        if depth is None:
            continue
        cell = cells.setdefault((int(chars or 0), int(depth)), {"n": 0, "hit": 0.0})
        cell["n"] += 1
        cell["hit"] += float(r["score"] or 0) / 100.0
    return [{"chars": c, "depth": d, "cases": v["n"],
             "score": round(100.0 * v["hit"] / v["n"], 1) if v["n"] else None}
            for (c, d), v in sorted(cells.items())]


# -- the tool-picking suite ----------------------------------------------------------

_PICK_SYSTEM = ("You choose tools. You are given every tool this system has, one "
                "per line as NAME - description. Answer with exactly one tool "
                "name, copied character for character, and nothing else.")
_PICK_QUESTION = ("Which single tool would you use to {need}?\n"
                  "Answer with the tool name only.")


def _tool_rows(svc) -> list:
    """(name, description) for every enabled tool the router offers."""
    try:
        rows = svc.tool_registry.status() or []
    except Exception:                                             # noqa: BLE001
        return []
    out = []
    for t in rows:
        if not isinstance(t, dict) or t.get("disabled"):
            continue
        name = str(t.get("name") or "").strip()
        desc = str(t.get("description") or "").strip()
        # the registry prefixes the server in brackets; the need reads better without
        if desc.startswith("[") and "]" in desc:
            desc = desc.split("]", 1)[1].strip()
        if name and desc:
            out.append((name, desc))
    return out


def _need_from(desc: str) -> str:
    """The description as something a person wants done."""
    d = desc.strip().rstrip(".")
    d = d[0].lower() + d[1:] if d else d
    for head in ("use this to ", "used to ", "this tool ", "tool to "):
        if d.startswith(head):
            d = d[len(head):]
    return d[:200]


def _fit_catalogue(rows: list) -> tuple:
    """(catalogue text, description cap). Every tool keeps a line; the
    descriptions shrink until the whole thing fits the prompt budget.

    Dropping tools instead would take the answer out of some cases and then
    score the model for not finding it (v0.181.200)."""
    # leave room for the system turn, the question and the answer
    budget = int(MAX_PROMPT_TOKENS * 0.6) * 4            # ~4 chars a token
    for cap in (160, 120, 90, 60, 40, 24):
        text = "\n".join(f"{n} - {(d or '')[:cap].rstrip()}" for n, d in rows)
        if len(text) <= budget:
            return text, cap
    # even at 24 characters it does not fit: names alone, which still names
    # every tool and is the most that can honestly be shown
    return "\n".join(n for n, _d in rows), 0


def build_toolpick(svc, name: str = "toolpick", limit: int = 40) -> dict:
    """Write a suite that asks which tool meets a stated need, with the whole
    catalogue in the prompt as the distractor set."""
    rows = _tool_rows(svc)
    if len(rows) < 5:
        raise ValueError("the tool registry has too few tools to build a suite "
                         "(is the router connected to its MCP servers?)")
    catalogue, desc_cap = _fit_catalogue(rows)
    import random as _rnd
    rnd = _rnd.Random("toolpick-v1")
    order = list(rows)
    rnd.shuffle(order)
    out, skipped = [], 0
    for tool_name, desc in order:
        if len(out) >= max(1, int(limit)):
            break
        need = _need_from(desc)
        # a need that names its own answer measures nothing
        if not need or _contains(_norm(need), _norm(tool_name)):
            skipped += 1
            continue
        short = tool_name.rsplit("__", 1)[-1].replace("_", " ")
        if short and _contains(_norm(need), _norm(short)):
            skipped += 1
            continue
        out.append({
            "id": f"PICK-{len(out) + 1:03d}",
            "task_id": tool_name,
            "tags": ["toolpick", "tool_choice", "context", f"catalogue:{len(rows)}"],
            "messages": [
                {"role": "system", "content": _PICK_SYSTEM},
                {"role": "user", "content": catalogue + "\n\n" +
                 _PICK_QUESTION.format(need=need)},
            ],
            "reference": {"content": tool_name, "must_include": [tool_name]},
        })
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in out:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    try:
        svc.db.log_event("info", "bench",
                         f"built the {name} suite: {len(out)} cases over a "
                         f"{len(rows)}-tool catalogue ({skipped} skipped, the need "
                         f"named its own answer)")
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(out), "path": str(f),
            "catalogue": len(rows), "skipped": skipped,
            "description_chars": desc_cap, "prompt_chars": len(catalogue)}


# -- the finder suite: the ecosystem as it is actually run ---------------------------

TOOLFLOW_ROUNDS = 8            # searches + the call, before a case is given up on
_FLOW_SYSTEM = ("You are an assistant with tools. You cannot see the tools directly: "
                "use foundry_find_tools to search for what you need, then "
                "foundry_call_tool to use it. Do the task you are given.")


def _task_from(name: str, desc: str) -> str:
    """The tool's description as something a person would ask for."""
    d = (desc or "").strip().rstrip(".")
    if d.startswith("[") and "]" in d:
        d = d.split("]", 1)[1].strip()
    if not d:
        return ""
    d = d[0].lower() + d[1:]
    for head in ("use this to ", "used to ", "this tool ", "tool to "):
        if d.startswith(head):
            d = d[len(head):]
    return d[:200]


def build_toolflow(svc, name: str = "toolflow", limit: int = 40,
                   persona: Optional[dict] = None) -> dict:
    """A suite of tasks to be done THROUGH the finder, built from the live registry."""
    tools = _granted_tools(svc, persona)
    if len(tools) < 5:
        raise ValueError("the tool registry has too few tools to build a suite "
                         "(is the router connected to its MCP servers?)")
    import random as _rnd
    rnd = _rnd.Random("toolflow-v1")
    order = sorted(tools, key=lambda t: t.name)
    rnd.shuffle(order)
    out, skipped = [], 0
    for t in order:
        if len(out) >= max(1, int(limit)):
            break
        task = _task_from(t.name, getattr(t, "description", ""))
        if not task:
            skipped += 1
            continue
        out.append({
            "id": f"FLOW-{len(out) + 1:03d}",
            "task_id": t.name,
            "tags": ["toolflow", "finder", "tool_use", f"server:{t.server or '?'}"],
            "messages": [
                {"role": "system", "content": _FLOW_SYSTEM},
                {"role": "user", "content": f"Please {task}."},
            ],
            # the finder is the test; the target tool is never run
            "reference": {"finder_tool": t.name, "content": t.name,
                          "tool_calls": [{"name": "foundry_call_tool"}]},
        })
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in out:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    try:
        svc.db.log_event("info", "bench",
                         f"built the {name} suite: {len(out)} task(s) to be done "
                         f"through the finder over {len(tools)} granted tools")
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(out), "path": str(f),
            "granted": len(tools), "skipped": skipped}


def _granted_tools(svc, persona: Optional[dict] = None) -> list:
    """The tool objects a persona may reach, as the finder itself sees them."""
    p = persona or {"virtual_name": "bench", "preferred_mcp_tools": '["*"]',
                    "mcp_tool_mode": "finder"}
    try:
        return [t for t in (svc.tool_registry.mcp_tools_for_persona(p) or [])]
    except Exception:                                             # noqa: BLE001
        return []


async def ask_through_finder(svc, model: str, case: dict, max_tokens: int = 2048,
                             label: str = "", persona: Optional[dict] = None) -> dict:
    """One task, done the way a finder persona does it.

    Searches really run; the chosen tool is recorded and never executed."""
    from . import tool_finder
    tools = _granted_tools(svc, persona)
    want = str((case.get("reference") or {}).get("finder_tool") or "")
    specs = [{"type": "function",
              "function": {"name": td.name, "description": td.description,
                           "parameters": td.parameters}}
             for td in tool_finder.tooldefs()]
    convo = wire(case.get("messages") or [])
    t0 = time.monotonic()
    searched, found, picked, calls = 0, False, "", []
    prompt_tokens = completion_tokens = 0
    backend = ""
    for _round in range(TOOLFLOW_ROUNDS):
        try:
            result, backend = await svc.pool.chat(
                model, convo, tools=specs, max_tokens=max_tokens,
                options=dict(SAMPLING))
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            return {"error": describe_exception(e),
                    "seconds": round(time.monotonic() - t0, 2)}
        prompt_tokens += result.prompt_tokens or 0
        completion_tokens += result.completion_tokens or 0
        tcs = result.tool_calls or []
        if not tcs:
            break                                    # it answered in prose; done
        convo = convo + [{"role": "assistant", "content": result.content or "",
                          "tool_calls": [{"id": tc.get("id") or f"c{i}",
                                          "type": "function",
                                          "function": {
                                              "name": tc.get("name"),
                                              "arguments": json.dumps(
                                                  tc.get("arguments") or {})}}
                                         for i, tc in enumerate(tcs)]}]
        for i, tc in enumerate(tcs):
            tname = str(tc.get("name") or "")
            args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
            calls.append(tname)
            if tname == tool_finder.FIND_TOOL:
                searched += 1
                hits = tool_finder.search(
                    tools, str(args.get("query") or ""),
                    tool_finder.DEFAULT_LIMIT)
                if want and any(h.name == want for h in hits):
                    found = True
                text = tool_finder.find_text(tools, args)
            elif tname == tool_finder.CALL_TOOL:
                picked = str(args.get("name") or "")
                # recorded, never run: a benchmark must not generate a video
                text = ("[bench] the call was recorded and not executed.")
            else:
                text = f"[bench] {tname} is not a tool you can use."
            convo.append({"role": "tool", "tool_call_id": tc.get("id") or f"c{i}",
                          "name": tname, "content": text})
        if picked:
            break
    return {"content": picked or (result.content or ""), "backend": backend,
            "tool_calls": [{"name": n, "arguments": {}} for n in calls],
            "finder": {"searched": searched, "found": found, "picked": picked,
                       "want": want, "rounds": len(calls)},
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "seconds": round(time.monotonic() - t0, 2)}


def finder_score(got: dict, want: str) -> dict:
    """found / picked / the case score.

    Half the marks for a search that surfaced the right tool and half for then
    choosing it, because a model that searches well and picks wrongly has failed
    differently from one that never found it, and one number hides which."""
    f = (got or {}).get("finder") or {}
    found = 1.0 if f.get("found") else 0.0
    picked = 1.0 if want and tool_finder_match(f.get("picked") or "", want) else 0.0
    return {"found": found, "picked": picked,
            "score": round(100 * (0.5 * found + 0.5 * picked), 1),
            "searched": int(f.get("searched") or 0)}


def tool_finder_match(got: str, want: str) -> bool:
    """The chosen name is the wanted tool. Exact, or the same tool written
    without its server prefix - foundry_call_tool resolves that too, so marking
    it wrong here would score the harness rather than the model."""
    g, w = str(got or "").strip(), str(want or "").strip()
    if not g or not w:
        return False
    if g == w:
        return True
    return g.rsplit("__", 1)[-1] == w.rsplit("__", 1)[-1] and bool(g.rsplit("__", 1)[-1])


# -- the code-reasoning suite --------------------------------------------------------
#
# What does this code DO. Exactly scoreable, and nothing a model writes is ever
# run: the expected value is computed by executing the snippet when the suite is
# BUILT, which is this module's own code (v0.181.204).

_CODE_SNIPPETS = [
    # (name, source, the expression whose value is the answer)
    ("aliasing",
     "a = [1, 2, 3]\nb = a\nb.append(4)\nresult = len(a)", "result"),
    ("copy_vs_alias",
     "a = [1, 2, 3]\nb = a[:]\nb.append(4)\nresult = len(a)", "result"),
    ("int_division",
     "result = 7 // 2", "result"),
    ("float_division",
     "result = 7 / 2", "result"),
    ("modulo_negative",
     "result = -7 % 3", "result"),
    ("empty_is_falsy",
     "xs = []\nresult = bool(xs or [0])", "result"),
    ("late_binding",
     "fns = [lambda: i for i in range(3)]\nresult = [f() for f in fns]", "result"),
    ("default_arg_shared",
     "def f(x, acc=[]):\n    acc.append(x)\n    return acc\nf(1)\nresult = f(2)",
     "result"),
    ("sort_stability",
     "rows = [('b', 1), ('a', 1), ('c', 0)]\n"
     "result = [n for n, k in sorted(rows, key=lambda r: r[1])]", "result"),
    ("string_multiply",
     "result = 'ab' * 3", "result"),
    ("slice_past_end",
     "result = [1, 2, 3][1:99]", "result"),
    ("negative_slice",
     "result = [1, 2, 3, 4][-2:]", "result"),
    ("dict_update_order",
     "d = {'a': 1}\nd.update({'b': 2, 'a': 3})\nresult = list(d.items())", "result"),
    ("set_dedup_order",
     "result = sorted({3, 1, 2, 1})", "result"),
    ("is_vs_equals",
     "a = [1]\nb = [1]\nresult = (a == b, a is b)", "result"),
    ("chained_comparison",
     "result = (1 < 2 < 3, 1 < 2 > 3)", "result"),
    ("tuple_single",
     "result = type(('x')).__name__", "result"),
    ("str_split_empty",
     "result = 'a,,b'.split(',')", "result"),
    ("zip_shortest",
     "result = list(zip([1, 2, 3], 'ab'))", "result"),
    ("range_step",
     "result = list(range(10, 0, -3))", "result"),
]

_CODE_EXC = [
    ("dict_mutation",
     "d = {'a': 1, 'b': 2}\nfor k in d:\n    del d[k]"),
    ("index_error",
     "xs = [1, 2]\nxs[5]"),
    ("key_error",
     "d = {'a': 1}\nd['b']"),
    ("zero_division",
     "1 / 0"),
    ("type_error_concat",
     "'a' + 1"),
    ("attribute_error",
     "None.strip()"),
    ("value_error_int",
     "int('12a')"),
    ("unbound_local",
     "def f():\n    x += 1\n    return x\nf()"),
]

_VALUE_Q = ("What is the value of `result` after this runs?\n\n```python\n{src}\n```"
            "\n\nReply with the value only, in Python notation.")
_EXC_Q = ("What exception does this raise?\n\n```python\n{src}\n```\n\n"
          "Reply with the exception's name only.")


def _run_snippet(src: str, expr: str):
    """The snippet's real value, computed when the suite is built.

    This is this module's own code, not a model's: the suite is a fixed table
    above, and nothing from a request ever reaches here (v0.181.204)."""
    ns: dict = {}
    exec(compile(src, "<bench-snippet>", "exec"), {"__builtins__": __builtins__}, ns)
    return eval(expr, {"__builtins__": __builtins__}, ns)       # noqa: S307


def _raised_by(src: str) -> str:
    try:
        exec(compile(src, "<bench-snippet>", "exec"),
             {"__builtins__": __builtins__}, {})
    except Exception as e:                                        # noqa: BLE001
        return type(e).__name__
    return ""


def build_codereason(svc, name: str = "codereason", limit: int = 0) -> dict:
    """A suite that asks what code does, with every answer computed by running
    the snippet here rather than recalled."""
    out, refused = [], []
    for key, src, expr in _CODE_SNIPPETS:
        try:
            value = _run_snippet(src, expr)
        except Exception as e:                                    # noqa: BLE001
            refused.append(f"{key}: {type(e).__name__}")
            continue
        out.append({
            "id": f"CODE-{key}",
            "task_id": key,
            "tags": ["codereason", "python", "code"],
            "messages": [
                {"role": "system", "content": "You reason about code precisely."},
                {"role": "user", "content": _VALUE_Q.format(src=src)},
            ],
            # compared as a VALUE: [2,2,2] and [2, 2, 2] are the same answer
            "reference": {"content": repr(value), "py_value": repr(value)},
        })
    for key, src in _CODE_EXC:
        got = _raised_by(src)
        if not got:
            refused.append(f"{key}: raised nothing")
            continue
        out.append({
            "id": f"CODE-{key}",
            "task_id": key,
            "tags": ["codereason", "python", "exception", "code"],
            "messages": [
                {"role": "system", "content": "You reason about code precisely."},
                {"role": "user", "content": _EXC_Q.format(src=src)},
            ],
            "reference": {"content": got, "must_include": [got]},
        })
    if limit:
        out = out[:int(limit)]
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in out:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    try:
        svc.db.log_event("info", "bench",
                         f"built the {name} suite: {len(out)} case(s), every answer "
                         f"computed by running the snippet"
                         + (f"; {len(refused)} refused" if refused else ""))
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(out), "path": str(f), "refused": refused}


# -- running a suite ----------------------------------------------------------------------

def wire(messages: list) -> list:
    """The stored conversation in the shape a backend expects.

    The prepared store keeps a NEUTRAL training format: tool calls are flat
    ({id, name, arguments}) with their arguments already parsed into a dict, and
    every turn carries an author. Handing that straight to a backend is what made
    48 of the first 60 real cases fail with "'dict' object has no attribute
    'startswith'" and another 3 with KeyError 'function' (v0.181.173) - the
    adapter reasonably expects arguments to be the JSON string the wire format
    uses."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        w = {"role": m.get("role") or "user", "content": content}
        if w["role"] == "tool":
            w["tool_call_id"] = str(m.get("tool_call_id") or "")
        calls = []
        for i, tc in enumerate(m.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args if args is not None else {}, default=str)
            calls.append({"id": str(tc.get("id") or f"call_{i}"), "type": "function",
                          "function": {"name": str(fn.get("name") or ""), "arguments": args}})
        if calls:
            w["tool_calls"] = calls
        out.append(w)
    return out


def wire_tools(tools: list) -> list:
    """The same for the tool definitions, which the store keeps flat as well."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        out.append({"type": "function", "function": {
            "name": fn["name"], "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}})
    return out


async def with_recall(svc, case: dict) -> tuple:
    """The case with the work recall would have found put in front of it, and the
    hits it used. ([] when recall is off, empty or unreachable.)

    **The case's own task is held out.** Recall is built from the same store the
    suite is cut from, so without this a case retrieves the very sprint it was cut
    from - including the turn being scored - and the model copies the answer. That
    is not recall helping, it is the test marking its own homework, and it would
    show up as a large improvement.
    """
    from . import recall
    msgs = list(case.get("messages") or [])
    asked = next((m.get("content") or "" for m in reversed(msgs)
                  if m.get("role") == "user" and (m.get("content") or "").strip()), "")
    if not asked:
        return case, []
    mine = str(case.get("task_id") or "")
    hits = [h for h in await recall.search(svc, asked, top_k=6)
            if not (mine and h.get("task_id") == mine)]
    cfg = getattr(svc.config_store.config, "recall", None)
    k = max(1, int(getattr(cfg, "top_k", 3) or 3))
    hits = hits[:k]
    if not hits:
        return case, []
    note = recall.note(hits, int(getattr(cfg, "max_chars", 2000) or 2000))
    at = 0
    for i, m in enumerate(msgs):
        if m.get("role") == "system":
            at = i + 1
    msgs.insert(at, {"role": "system", "content": note})
    return {**case, "messages": msgs}, hits


def with_lessons(svc, case: dict) -> tuple:
    """The case with the approved rulebook in front of it, and how many rules
    went in. (The case unchanged and 0 when lessons are off or none are active.)

    No task is held out, because a rule is not an answer: it carries no file,
    no call and no number from the sprint it was mined from, so a case cannot
    copy its own result out of one the way it can out of a retrieved sprint.
    What a rule mined from VQ findings *can* do is help a VQ case for reasons
    that would not generalise, so a run is read per task family rather than as
    one average - which is the whole question being asked of it (v0.181.188).
    """
    from . import lessons
    cfg = getattr(svc.config_store.config, "lessons", None)
    # text=None: every active rule regardless of scope. A suite case carries the
    # task id it was cut from, so scoping would quietly turn the arm into "rules
    # that happen to match this suite" and the comparison would drift with the
    # queue rather than measuring the rulebook.
    rules, _dropped, _held = lessons.rulebook(
        svc.db, int(getattr(cfg, "max_chars", 1500) or 1500), None)
    if not rules:
        return case, 0
    msgs = list(case.get("messages") or [])
    at = 0
    for i, m in enumerate(msgs):
        if m.get("role") == "system":
            at = i + 1
    msgs.insert(at, {"role": "system", "content": rules})
    import re as _re
    return {**case, "messages": msgs}, len(_re.findall(r"(?m)^\s*\d+\.", rules))


def delete_runs(svc, run_ids: list, drop_samples: bool = True) -> dict:
    """Remove benchmark runs, their cases, and the perf samples they recorded.

    The running one is refused rather than skipped quietly: deleting the rows a
    run is still writing would leave it half gone and the operator told nothing
    (v0.181.198).
    """
    ids = []
    for r in run_ids or []:
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            continue
    out = {"deleted": 0, "cases": 0, "samples": 0, "refused": [], "missing": []}
    if not ids:
        return out
    live = _state.get("run_id") if _state.get("state") == "running" else None
    db = svc.db
    for rid in ids:
        if live and rid == int(live):
            out["refused"].append(rid)
            continue
        row = db.query_one("SELECT id, model, label FROM bench_runs WHERE id=?", (rid,))
        if not row:
            out["missing"].append(rid)
            continue
        n = db.query_one("SELECT COUNT(*) AS n FROM bench_results WHERE run_id=?",
                         (rid,)) or {}
        out["cases"] += int(n.get("n") or 0)
        db.execute("DELETE FROM bench_results WHERE run_id=?", (rid,))
        if drop_samples:
            # only the samples this run labelled as its own; a run from before
            # v0.181.198 has none, and nothing else carries that label
            lbl = run_label(rid, row.get("model") or "", row.get("label") or "")
            s = db.query_one("SELECT COUNT(*) AS n FROM perf_samples WHERE run_label=?",
                             (lbl,)) or {}
            if int(s.get("n") or 0):
                db.execute("DELETE FROM perf_samples WHERE run_label=?", (lbl,))
                out["samples"] += int(s.get("n") or 0)
        db.execute("DELETE FROM bench_runs WHERE id=?", (rid,))
        out["deleted"] += 1
    if out["deleted"]:
        db.log_event("info", "bench",
                     f"deleted {out['deleted']} benchmark run(s), "
                     f"{out['cases']} case(s) and {out['samples']} perf sample(s)")
    return out


def run_label(run_id, model: str, label: str = "") -> str:
    """The perf run label for one bench run, so the Performance page can show
    exactly the calls this benchmark made (v0.181.198)."""
    bits = [f"bench{int(run_id)}" if run_id else "bench", str(model or "")[:40]]
    if label:
        bits.append(str(label)[:24])
    return " ".join(b for b in bits if b)


async def ask(svc, model: str, case: dict, max_tokens: int = 2048,
              persona: str = "", label: str = "") -> dict:
    """One case through one model, with the tools that case was offered."""
    t0 = time.monotonic()
    try:
        result, backend = await svc.pool.chat(
            model, wire(case.get("messages") or []),
            tools=wire_tools(case.get("tools") or []) or None, max_tokens=max_tokens,
            options=dict(SAMPLING))
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        return {"error": describe_exception(e), "seconds": round(time.monotonic() - t0, 2)}
    # v0.181.196: the same telemetry every other dispatch path records. Without
    # it a bench run is invisible to the Performance tab and to the Live view -
    # the brain lane had 0 per-call samples in 24 hours while a 60-case
    # benchmark was running against it.
    try:
        from . import telemetry
        telemetry.record_call(
            svc.db, svc.registry, model=model, backend=backend, result=result,
            persona=persona or "bench", mode="bench",
            wall_ms=(time.monotonic() - t0) * 1000.0, max_tokens=max_tokens,
            extra={"run_label": label} if label else None)
    except Exception:                                             # noqa: BLE001
        log.exception("bench: could not record telemetry for %s", model)
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
    if ref.get("deliverable"):
        # No answer key, so no score - and that is the refusal, not a gap.
        # answer_score is a length heuristic and code_score only compiles, so
        # both would fire here and hand a deliverable a confident 100 (the
        # needle fault of v0.181.199, which scored a model that found nothing).
        return {"score": None, "tools": 0.0, "code": None, "answer": None,
                "exact": None, "code_detail": "deliverable: not scored",
                "error": got.get("error") or ""}
    if got.get("error"):
        return {"score": 0.0, "tools": 0.0, "code": None, "answer": 0.0,
                "code_detail": "", "error": got["error"]}
    if ref.get("finder_tool"):
        fs = finder_score(got, str(ref.get("finder_tool")))
        return {"score": fs["score"], "tools": fs["picked"], "code": None,
                "answer": fs["found"], "exact": None, "code_detail":
                    f"found={bool(fs['found'])} picked={(got.get('finder') or {}).get('picked') or '-'} "
                    f"searches={fs['searched']}",
                "error": ""}
    tools = tool_score(ref.get("tool_calls") or [], got.get("tool_calls") or [])
    code, detail = await code_score(svc, got.get("content") or "")
    answer = (answer_score(got.get("content") or "", ref.get("content") or "")
              if not (ref.get("tool_calls") or []) else None)
    exact = value_score(got.get("content") or "", str(ref.get("py_value") or "")) \
        if ref.get("py_value") else \
        exact_score(got.get("content") or "", ref.get("must_include"))
    return {"score": combine(tools, code, answer, exact), "tools": tools, "code": code,
            "answer": answer, "exact": exact, "code_detail": detail, "error": ""}


def _row(db, sql, args=()):
    db.execute(sql, args)


async def _run(svc, suite: str, model: str, label: str, limit: int,
               use_recall: bool = False, use_lessons: bool = False) -> dict:
    db = svc.db
    items = cases(svc, suite, limit)
    if not items:
        raise ValueError(f"the suite {suite!r} has no cases")
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status, "
               "recall, lessons) VALUES (datetime('now'), ?, ?, ?, ?, 0, 'running', ?, ?)",
               (suite, model, label, len(items), 1 if use_recall else 0,
                1 if use_lessons else 0))
    run_id = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    _state.update({"state": "running", "run_id": run_id, "suite": suite, "model": model,
                   "cases": len(items), "done": 0, "started": time.time(), "score": None,
                   "recall": bool(use_recall), "recalled": 0,
                   "lessons": bool(use_lessons), "ruled": 0})
    # every perf sample this run writes is filed under its own label, so the
    # Performance page can show exactly the calls this benchmark made
    # (v0.181.198)
    rlabel = run_label(run_id, model, label)
    _state["run_label"] = rlabel
    totals = {"score": 0.0, "tools": 0.0, "code": 0.0, "answer": 0.0, "seconds": 0.0,
              "prompt": 0, "completion": 0, "code_n": 0, "answer_n": 0, "errors": 0,
              # cases that HAVE a score. The average divided by cases done,
              # which would quietly lower a run holding unscorable cases, and
              # for a suite with no deliverable in it this is the same number
              "score_n": 0, "delivered": 0}
    for i, case in enumerate(items, start=1):
        if _state.get("stop"):
            break
        hits: list = []
        if use_recall:
            case, hits = await with_recall(svc, case)
            if hits:
                _state["recalled"] += 1
        if use_lessons:
            case, _nrules = with_lessons(svc, case)
            if _nrules:
                _state["ruled"] = _nrules
        # a case that names a finder_tool is run THROUGH the finder: the search
        # really happens, the chosen tool is recorded and never executed
        if (case.get("reference") or {}).get("finder_tool"):
            got = await ask_through_finder(svc, model, case, label=rlabel)
            s = await score_one(svc, case, got)
        else:
            got = await ask(svc, model, case, label=rlabel)
            s = await score_one(svc, case, got)
        db.execute(
            "INSERT INTO bench_results (run_id, case_id, task_id, tags, tools_expected, "
            "tools_called, tools_score, code_pass, code_detail, answer_chars, score, seconds, "
            "error, recalled, deliverable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, case.get("id") or "", case.get("task_id") or "",
             json.dumps(case.get("tags") or []),
             json.dumps([t.get("name") for t in (case.get("reference") or {}).get("tool_calls") or []]),
             json.dumps([t.get("name") for t in got.get("tool_calls") or []]),
             s["tools"], None if s["code"] is None else int(s["code"] >= 1), s["code_detail"],
             len(got.get("content") or ""), s["score"], got.get("seconds") or 0, s["error"],
             json.dumps([h["case_id"] for h in hits]),
             (got.get("content") or "")
             if (case.get("reference") or {}).get("deliverable") else ""))
        if s["score"] is None:
            totals["delivered"] += 1
        else:
            totals["score"] += s["score"]
            totals["score_n"] += 1
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
        _state["score"] = (round(totals["score"] / totals["score_n"], 1)
                           if totals["score_n"] else None)
    n = max(1, _state["done"])
    sn = totals["score_n"]
    out = {"run_id": run_id, "suite": suite, "model": model, "cases": len(items),
           "scored": _state["done"], "delivered": totals["delivered"],
           "score": round(totals["score"] / sn, 1) if sn else None,
           "tools": round(100 * totals["tools"] / n, 1),
           "code": round(100 * totals["code"] / totals["code_n"], 1) if totals["code_n"] else None,
           "answer": round(100 * totals["answer"] / totals["answer_n"], 1) if totals["answer_n"] else None,
           "seconds": round(totals["seconds"] / n, 2), "prompt_tokens": totals["prompt"],
           "completion_tokens": totals["completion"], "errors": totals["errors"],
           "recall": bool(use_recall), "recalled": _state.get("recalled", 0),
           "lessons": bool(use_lessons), "ruled": _state.get("ruled", 0)}
    db.execute("UPDATE bench_runs SET scored=?, tools_score=?, code_score=?, answer_score=?, "
               "score=?, seconds=?, prompt_tokens=?, completion_tokens=?, status=?, note=? "
               "WHERE id=?",
               (out["scored"], out["tools"], out["code"], out["answer"], out["score"],
                out["seconds"], out["prompt_tokens"], out["completion_tokens"],
                "stopped" if _state.get("stop") else "done",
                "; ".join(x for x in (
                    f"{out['errors']} call(s) failed" if out["errors"] else "",
                    f"past work found for {out['recalled']} of {out['scored']} case(s)"
                    if use_recall else "",
                    f"{out['ruled']} approved rule(s) in every prompt"
                    if use_lessons else "",
                    f"{out['delivered']} deliverable task(s) are not scored - "
                    f"compare this run with another one"
                    if out["delivered"] else "") if x), run_id))
    _state.update({"state": "done", "finished": time.time(), **out})
    return out


def start(svc, suite: str, model: str, label: str = "", limit: int = 0,
          use_recall: bool = False, use_lessons: bool = False) -> dict:
    """Run a suite in the background. One at a time. `use_recall` puts the work
    recall finds in front of every case, and `use_lessons` the approved rulebook,
    so two runs answer whether either helps."""
    if _state.get("state") == "running":
        return dict(_state)
    if not _lock.acquire(blocking=False):
        return dict(_state)
    _state.clear()
    _state.update({"state": "running", "suite": suite, "model": model, "done": 0,
                   "recall": bool(use_recall), "lessons": bool(use_lessons)})

    def go():
        try:
            asyncio.run(_run(svc, suite, model, label, limit, use_recall, use_lessons))
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


def reconcile(db) -> int:
    """A run whose thread is gone says "running" for ever, because nothing was
    left alive to write the end of it. A restart in the middle of a run (a
    rebuild, a kill) leaves exactly that, and a row like it is worse than no row
    - it reads as a result that is still coming. Called at startup."""
    n = db.execute_count(
        "UPDATE bench_runs SET status='interrupted', "
        "note=COALESCE(NULLIF(note,''), 'the router restarted while this run was going') "
        "WHERE status='running'")
    if n:
        db.log_event("info", "bench", f"{n} benchmark run(s) were interrupted by a restart")
    return n


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
