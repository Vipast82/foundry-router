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
# Bumped whenever SCORING changes meaning, so a run scored the old way is never
# lined up against one scored the new way as if they measured the same thing.
# 2 (v0.181.211): the finder suite no longer ends a case at the first call,
# and a decoy in a needle case scores 0 when the answer names it.
SCORER_VERSION = "2"
KEEP_ANSWER = 2000             # characters of each answer kept for reading a failure
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


def case_view(svc, suite: str, case_id: str, last: int = 8) -> dict:
    """One case as the model saw it, for reading a miss (v0.181.215): the last
    turns of its conversation, the tools it was offered, and what was wanted.

    A score says a case was missed; only the prompt says whether the miss was
    the model's or the test's - a task with no URL in it, a reference that was
    really narration before a call. Text is trimmed for the page; nothing is
    changed."""
    for c in cases(svc, suite):
        if str(c.get("id")) != str(case_id):
            continue
        msgs = c.get("messages") or []

        def _m(m):
            out = {"role": m.get("role"), "content": str(m.get("content") or "")[:4000]}
            tcs = m.get("tool_calls") or []
            if tcs:
                out["tool_calls"] = [
                    {"name": (tc.get("function") or tc).get("name") or "",
                     "arguments": str((tc.get("function") or tc).get("arguments") or "")[:600]}
                    for tc in tcs]
            if m.get("name"):
                out["name"] = m["name"]
            return out
        return {"suite": suite, "id": c.get("id"), "task_id": c.get("task_id") or "",
                "tags": c.get("tags") or [], "turns": len(msgs),
                "shown": [_m(m) for m in msgs[-max(1, int(last)):]],
                "system": next((str(m.get("content") or "")[:1500] for m in msgs
                                if m.get("role") == "system"), ""),
                "tools_offered": [(t.get("function") or t).get("name") or ""
                                  for t in c.get("tools") or []],
                "reference": c.get("reference") or {}}
    return {"error": f"no case {case_id!r} in the {suite!r} suite - it may have been "
                     "rebuilt since this run"}


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
    tags = list(meta.get("tags") or [])
    calls = ref.get("tool_calls") or []
    # A words-only turn followed straight away by another ASSISTANT turn that
    # calls a tool is one move written as two messages - "Let me check the
    # file." and then the call - and the move is the call (v0.181.215). Scored
    # as "answer in words", a model that narrates and calls in one turn was
    # marked wrong for doing exactly what the verified session did. A real
    # end of turn is followed by a user message, and is left alone.
    if not calls and at + 1 < len(msgs) and msgs[at + 1].get("role") == "assistant" \
            and msgs[at + 1].get("tool_calls"):
        calls = msgs[at + 1]["tool_calls"]
        tags.append("split-turn")
    return {"id": f"{meta.get('task_id') or 'case'}-{meta.get('session') or ''}-{at}".strip("-"),
            "task_id": meta.get("task_id") or "", "tier": meta.get("tier") or "",
            "tags": tags, "models": meta.get("models") or [],
            "messages": prefix, "tools": example.get("tools") or [],
            "reference": {"content": (ref.get("content") or "")[:20000],
                          "tool_calls": [{"name": (tc.get("function") or tc).get("name") or "",
                                          "arguments": (tc.get("function") or tc).get("arguments")}
                                         for tc in calls]}}


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


def exact_score(text: str, must_include, must_exclude=None) -> Optional[float]:
    """The share of the strings a case REQUIRES that are in the answer, or None
    when the case requires none (v0.181.199).

    This is what a retrieval case is scored on. `answer_score` cannot do it: it
    is a length heuristic and returns 1.0 for any answer over twenty characters.

    `must_exclude` is for a case with a decoy in it (v0.181.211): an answer
    that names the decoy scores 0 even if it names the right one too, because
    listing every candidate is not finding the one that was asked for.
    """
    want = [w for w in (must_include or []) if str(w or "").strip()]
    if not want:
        return None
    body = _norm(text)
    if not body:
        return 0.0
    if any(_contains(body, _norm(x)) for x in (must_exclude or [])
           if str(x or "").strip()):
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
# characters of haystack. 4k-32k (~1k-8k tokens) is where both local models
# already scored 100, so it could not tell a better model from a good one; 64k
# and 96k (~16k-24k tokens) are where context starts to cost (v0.181.211)
NEEDLE_LENGTHS = (4000, 16000, 32000, 64000, 96000)
NEEDLE_DECOYS = 3              # decoy cases per length, kept OUT of the depth grid
TIERS = ("easy", "medium", "hard", "expert")


def needle_tier(length: int, decoy: bool = False) -> str:
    """How hard a needle case is, for the card's difficulty ladder. A decoy
    case is one step harder than a plain needle at the same length."""
    i = 0 if length <= 4000 else 1 if length <= 32000 else 2 if length <= 64000 else 3
    return TIERS[min(3, i + (1 if decoy else 0))]
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
    need = max(NEEDLE_LENGTHS) + 2000
    if len(body) < need:
        # never fail for want of filler: repeat what there is rather than invent.
        # It must cover the LONGEST case, or a "96k" case would be cut short and
        # still be labelled 96k
        body = (body + "\n\n") * (1 + need // max(1, len(body)))
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
        "tags": ["needle", "context", f"depth:{depth}", f"chars:{length}",
                 f"tier:{needle_tier(length)}"],
        "messages": [
            {"role": "system", "content": "You answer from the notes you are given."},
            {"role": "user", "content": body + "\n\n" +
             _NEEDLE_QUESTION.format(task=task)},
        ],
        # scored on this alone: see combine(). The code is unguessable, so a model
        # that did not find it cannot score by writing something plausible.
        "reference": {"content": code, "must_include": [code]},
    }


def _decoy_task(task: str) -> str:
    """A sprint id that reads like `task` - same prefix, digits swapped (or one
    up) - so the decoy cannot be told apart by its shape."""
    head, _, num = task.rpartition("-")
    other = num[::-1] if num[::-1] != num else str(int(num) % 89 + 10)
    return f"{head}-{other}"


def _decoy_case(hay: str, length: int, seq: int) -> dict:
    """A needle with a DECOY: a second sentence of exactly the same shape, for
    a near-identical sprint id, somewhere else in the same context.

    A plain needle is found by spotting the one sentence that does not belong;
    every model here already does that to 32k. With a decoy it has to read
    which sprint the sentence is about, and an answer naming both codes scores
    0 - see exact_score's must_exclude."""
    import random as _rnd
    rnd = _rnd.Random(f"needle-decoy-{length}-{seq}")
    code = f"{rnd.randint(100000, 999999)}"
    fake = f"{rnd.randint(100000, 999999)}"
    while fake == code:
        fake = f"{rnd.randint(100000, 999999)}"
    task = f"{rnd.choice(('VR-AS', 'LOB', 'FIN', 'MV2', 'POL'))}-{rnd.randint(10, 99)}"
    decoy = _decoy_task(task)
    start = rnd.randrange(0, max(1, len(hay) - length - 1))
    filler = hay[start:start + length]
    # the two land in different halves, the order chosen per case, so neither
    # "the first one" nor "the last one" is the answer by position
    lo, hi = rnd.randint(10, 40), rnd.randint(60, 90)
    real_at, fake_at = (lo, hi) if rnd.random() < 0.5 else (hi, lo)
    marks = sorted([(real_at, _NEEDLE_SENTENCE.format(task=task, code=code)),
                    (fake_at, _NEEDLE_SENTENCE.format(task=decoy, code=fake))])
    body, last = "", 0
    for pct, sentence in marks:
        at = int(len(filler) * pct / 100)
        nl = filler.find("\n", at)
        at = nl + 1 if 0 <= nl <= at + 400 else at
        body += filler[last:at] + "\n" + sentence + "\n"
        last = at
    body += filler[last:]
    return {
        "id": f"NEEDLE-{length//1000}k-decoy-{seq}",
        "task_id": task,
        # no depth: tag, so the depth grid stays a grid of plain needles
        "tags": ["needle", "context", "decoy", f"chars:{length}",
                 f"tier:{needle_tier(length, decoy=True)}"],
        "messages": [
            {"role": "system", "content": "You answer from the notes you are given."},
            {"role": "user", "content": body + "\n\n" +
             _NEEDLE_QUESTION.format(task=task)},
        ],
        "reference": {"content": code, "must_include": [code],
                      "must_exclude": [fake], "decoy_task": decoy},
    }


def build_needle(svc, name: str = "needle", per_cell: int = 3,
                 depths=NEEDLE_DEPTHS, lengths=NEEDLE_LENGTHS,
                 decoys: int = NEEDLE_DECOYS) -> dict:
    """Write a needle-in-a-haystack suite: `per_cell` cases at each depth and
    length, plus `decoys` decoy cases per length. Returns {name, cases, path}."""
    hay = _haystack_text(svc)
    out = []
    for length in lengths:
        for depth in depths:
            for seq in range(int(per_cell)):
                out.append(_needle_case(hay, int(length), int(depth), seq))
        for seq in range(int(decoys or 0)):
            out.append(_decoy_case(hay, int(length), seq))
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


def _first_sentence(desc: str, cap: int = 220) -> str:
    """What the tool does, as one clean sentence (v0.181.215).

    The v0.181.201 task was the description pasted in whole and cut at 200
    characters: docstring blocks included ("Args: name: ..."), words cut in half
    ("...captio."), and phrases that are not requests ("Please narrated tutorial
    video ..."). A model asked something unreadable is not being measured."""
    d = (desc or "").strip()
    if d.startswith("[") and "]" in d:
        d = d.split("]", 1)[1].strip()
    import re as _re
    # the summary paragraph only: docstring sections and later paragraphs go
    d = _re.split(r"\n\s*\n|\n\s*(?:Args|Arguments|Returns|Parameters|Example|Examples|"
                  r"Raises|Note|Notes)\s*:", d, maxsplit=1)[0]
    d = " ".join(d.split())
    m = _re.match(r"(.+?[.!?])(?:\s|$)", d)
    if m and len(m.group(1)) >= 20:
        d = m.group(1)
    if len(d) > cap:
        cut = d[:cap].rsplit(" ", 1)[0].rstrip(",;:-")
        d = cut + "..."
    return d.rstrip()


# Example values for a tool's REQUIRED inputs, by the parameter's name, so the
# task carries what the tool needs. Without them a correct model has to look the
# input up first (snapshot the page, list the jobs) or ask for it - and both were
# scored as misses (v0.181.215). The target tool is never executed, so the
# values only need to be the right kind of thing.
_ARG_EXAMPLES = (
    ("url", "https://example.com/docs/getting-started"),
    ("urls", ["https://example.com/a", "https://example.com/b"]),
    ("job_id", "a1b2c3d4e5f6"),
    ("ref", "e12"),
    ("element", "the Submit button"),
    ("selector", "#submit"),
    ("path", "assets/example.png"),
    ("file", "assets/example.png"),
    ("image", "assets/example.png"),
    ("video", "assets/example.mp4"),
    ("audio", "assets/example.wav"),
    ("text", "Welcome to Animal Uprising."),
    ("prompt", "a red barn at sunset, painted style"),
    ("query", "player data saving"),
    ("name", "example"),
    ("voice", "narrator"),
    ("entities", ["PlayerService", "DataStore"]),
    ("relations", [{"from": "PlayerService", "to": "DataStore", "relationType": "uses"}]),
    ("entityNames", ["PlayerService"]),
    ("fields", [{"name": "email", "type": "textbox", "ref": "e7", "value": "a@b.c"}]),
    ("expression", "() => document.title"),
    ("function", "() => document.title"),
    ("time", 2),
)


def _example_arg(pname: str, schema: dict):
    s = schema or {}
    if s.get("enum"):
        return s["enum"][0]
    low = pname.lower()
    for key, val in _ARG_EXAMPLES:
        if low == key.lower() or low.endswith("_" + key.lower()):
            return val
    t = s.get("type")
    if t in ("integer", "number"):
        return 5
    if t == "boolean":
        return True
    if t == "array":
        return ["example"]
    if t == "object":
        return {}
    for key, val in _ARG_EXAMPLES:
        if key.lower() in low and isinstance(val, str):
            return val
    return "example"


def task_inputs(parameters: dict, limit: int = 4) -> dict:
    """{required parameter: an example value} for a tool's schema."""
    p = parameters or {}
    props = p.get("properties") or {}
    out = {}
    for name in (p.get("required") or [])[:limit]:
        out[name] = _example_arg(str(name), props.get(name) or {})
    return out


def toolflow_task(desc: str, parameters: dict) -> str:
    """The request a toolflow case makes: what to do, and what with."""
    what = _first_sentence(desc)
    if not what:
        return ""
    inputs = task_inputs(parameters)
    ask = f"I need this done: {what}"
    if inputs:
        ask += "\nUse these inputs: " + "; ".join(
            f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in inputs.items())
    return ask


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
        # the same shuffle as before, so a rebuilt suite tests the same tools and
        # only the wording and the inputs change (v0.181.215)
        task = toolflow_task(getattr(t, "description", ""),
                             getattr(t, "parameters", None) or {})
        if not task:
            skipped += 1
            continue
        out.append({
            "id": f"FLOW-{len(out) + 1:03d}",
            "task_id": t.name,
            "tags": ["toolflow", "finder", "tool_use", f"server:{t.server or '?'}"],
            "messages": [
                {"role": "system", "content": _FLOW_SYSTEM},
                {"role": "user", "content": task},
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
    used: list = []                    # every tool it CALLED, in order
    queries: list = []                 # what it searched for (v0.181.215)
    prompt_tokens = completion_tokens = 0
    backend = ""
    result = None
    for _round in range(TOOLFLOW_ROUNDS):
        r0 = time.monotonic()
        try:
            result, backend = await svc.pool.chat(
                model, convo, tools=specs, max_tokens=max_tokens,
                options=dict(SAMPLING))
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            return {"error": describe_exception(e),
                    "seconds": round(time.monotonic() - t0, 2)}
        # every round is a model call and is recorded like one. Without this
        # a finder run left no performance sample at all (v0.181.211): the
        # telemetry ask() records was never added to this second path.
        _record(svc, model, backend, result, (time.monotonic() - r0) * 1000.0,
                max_tokens, label)
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
                queries.append(str(args.get("query") or "")[:200])
                hits = tool_finder.search(
                    tools, str(args.get("query") or ""),
                    tool_finder.DEFAULT_LIMIT)
                if want and any(h.name == want for h in hits):
                    found = True
                text = tool_finder.find_text(tools, args)
            elif tname == tool_finder.CALL_TOOL:
                name = str(args.get("name") or "")
                if name:
                    used.append(name)
                if want and tool_finder_match(name, want):
                    picked = name
                # recorded, never run: a benchmark must not generate a video
                text = ("[bench] the call was recorded and not executed.")
            else:
                text = f"[bench] {tname} is not a tool you can use."
            convo.append({"role": "tool", "tool_call_id": tc.get("id") or f"c{i}",
                          "name": tname, "content": text})
        # Stop when it has used the tool the task needs - NOT at its first call.
        # Up to v0.181.210 the first call ended the case, so taking a page
        # snapshot before clicking, or listing the voices before speaking, was
        # scored as choosing the wrong tool: 22 of flash's 25 toolflow misses
        # were that. A preparatory call costs a round, and the rounds are
        # capped, so wandering is still a failure.
        if picked:
            break
    first = used[0] if used else ""
    return {"content": picked or first or ((result.content or "") if result else ""),
            "backend": backend,
            "tool_calls": [{"name": n, "arguments": {}} for n in calls],
            "finder": {"searched": searched, "found": found,
                       "picked": picked or first, "want": want,
                       "used": used, "hit": bool(picked), "queries": queries,
                       # what it said instead, when it ended without the tool:
                       # "which URL?" is a different miss from a wrong pick
                       "said": ((result.content or "") if result else "")[:600]
                       if not picked else "",
                       "rounds": len(calls)},
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "seconds": round(time.monotonic() - t0, 2)}


def finder_score(got: dict, want: str) -> dict:
    """found / picked / the case score.

    Half the marks for a search that surfaced the right tool and half for then
    choosing it, because a model that searches well and picks wrongly has failed
    differently from one that never found it, and one number hides which."""
    f = (got or {}).get("finder") or {}
    found = 1.0 if f.get("found") else 0.0
    used = [str(u) for u in (f.get("used") or []) if u] or (
        [str(f.get("picked"))] if f.get("picked") else [])
    picked = 1.0 if want and any(tool_finder_match(u, want) for u in used) else 0.0
    return {"found": found, "picked": picked,
            "score": round(100 * (0.5 * found + 0.5 * picked), 1),
            "searched": int(f.get("searched") or 0),
            "used": used,
            # calls made before the one the task needed: not wrong, but slower
            "detours": (next((i for i, u in enumerate(used)
                              if tool_finder_match(u, want)), len(used))
                        if picked else len(used))}


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

# Harder tiers (v0.181.211). The twenty-eight above are one-line gotchas and
# the small flash model scored all of them, so the suite said nothing about a
# better model. These have to be TRACED: state carried across a loop, a
# generator, a closure, the MRO, a try/finally. Same rule as the rest - the
# answer is computed by running the snippet when the suite is built.
_CODE_HARD = [
    ("generator_exhausted",
     "g = (x * x for x in range(4))\nfirst = sum(g)\nsecond = sum(g)\n"
     "result = (first, second)", "result"),
    ("closure_counters",
     "def make():\n    n = 0\n    def inc(k=1):\n        nonlocal n\n        n += k\n"
     "        return n\n    return inc\na = make()\nb = make()\na()\na(5)\nb(2)\n"
     "result = (a(0), b(0))", "result"),
    ("class_attr_shadow",
     "class C:\n    items = []\n    count = 0\n    def add(self, x):\n"
     "        self.items.append(x)\n        self.count += 1\na, b = C(), C()\n"
     "a.add(1)\nb.add(2)\na.add(3)\nresult = (len(C.items), C.count, a.count, b.count)",
     "result"),
    ("try_except_finally",
     "log = []\ndef g(v):\n    try:\n        log.append('t')\n        return 10 // v\n"
     "    except ZeroDivisionError:\n        log.append('e')\n        return -1\n"
     "    finally:\n        log.append('f')\nresult = (g(2), g(0), ''.join(log))",
     "result"),
    ("mro_super",
     "class A:\n    def who(self): return 'A'\nclass B(A):\n"
     "    def who(self): return 'B' + super().who()\nclass C(A):\n"
     "    def who(self): return 'C' + super().who()\nclass D(B, C):\n"
     "    def who(self): return 'D' + super().who()\nresult = D().who()", "result"),
    ("nested_comprehension",
     "result = [(i, j) for i in range(4) if i % 2 for j in range(i) if j != 1]",
     "result"),
    ("dict_key_collision",
     "d = {1: 'int', 1.0: 'float', True: 'bool'}\nresult = (len(d), d[1])", "result"),
    ("walrus_loop",
     "data = [3, 1, 4, 1, 5, 9, 2, 6]\nout = []\ni = 0\n"
     "while (n := data[i]) != 9:\n    if n % 2:\n        out.append(n * 10)\n"
     "    i += 1\nresult = (out, i)", "result"),
]

_CODE_EXPERT = [
    ("inventory_orders",
     "stock = {'wood': 5, 'stone': 2}\n"
     "orders = [('wood', 3), ('stone', 3), ('iron', 1), ('wood', 2), ('stone', 2), ('wood', 1)]\n"
     "filled, short = [], {}\nfor item, qty in orders:\n    have = stock.get(item, 0)\n"
     "    if have >= qty:\n        stock[item] = have - qty\n        filled.append(item[0])\n"
     "    else:\n        short[item] = short.get(item, 0) + qty - have\n"
     "        stock[item] = 0\nresult = (''.join(filled), stock, short)", "result"),
    ("memo_recursion",
     "from functools import lru_cache\ncalls = []\n@lru_cache(maxsize=None)\n"
     "def ways(n):\n    calls.append(n)\n    if n < 0:\n        return 0\n"
     "    if n == 0:\n        return 1\n    return ways(n - 1) + ways(n - 3)\n"
     "result = (ways(7), len(calls))", "result"),
    ("generator_send",
     "def acc():\n    total = 0\n    while True:\n        x = yield total\n"
     "        if x is None:\n            break\n        total += x\ng = acc()\n"
     "seen = [next(g)]\nfor v in (5, -2, 10):\n    seen.append(g.send(v))\ntry:\n"
     "    g.send(None)\nexcept StopIteration:\n    seen.append('done')\nresult = seen",
     "result"),
    ("multi_key_sort",
     "rows = [('ana', 3, 9.5), ('bo', 1, 7.0), ('cy', 3, 8.0), ('di', 2, 9.5), ('ed', 1, 9.0)]\n"
     "by = sorted(rows, key=lambda r: (-r[1], r[2], r[0]))\nresult = [r[0] for r in by]",
     "result"),
    ("run_length_decode",
     "s = 'x3y12z'\nout, num, prev = '', '', ''\nfor ch in s + '!':\n"
     "    if ch.isdigit():\n        num += ch\n        continue\n    if prev:\n"
     "        out += prev * (int(num) if num else 1)\n    prev, num = ch, ''\n"
     "result = (out, len(out))", "result"),
    ("closure_sees_last_binding",
     "x = 'g'\ndef outer():\n    x = 'o'\n    def inner():\n        return x\n"
     "    x = 'o2'\n    return inner\nf = outer()\nx = 'g2'\nresult = f() + '/' + x",
     "result"),
    ("bit_flags",
     "flags = 0\nfor bit in (0, 3, 5, 3):\n    flags ^= 1 << bit\n"
     "mask = flags & ~(1 << 5)\nresult = (flags, mask, bin(flags).count('1'))", "result"),
    ("exception_cause",
     "def parse(v):\n    try:\n        return int(v)\n    except ValueError as e:\n"
     "        raise KeyError(v) from e\nout = []\nfor v in ('1', 'x', '3'):\n    try:\n"
     "        out.append(parse(v))\n    except KeyError as e:\n"
     "        out.append(type(e.__cause__).__name__)\nresult = out", "result"),
]

# which of the original twenty-eight are more than a single rule to recall
_CODE_MEDIUM = {"modulo_negative", "empty_is_falsy", "late_binding",
                "default_arg_shared", "sort_stability", "dict_update_order",
                "is_vs_equals", "chained_comparison", "tuple_single",
                "str_split_empty", "dict_mutation", "unbound_local"}


def code_tier(key: str) -> str:
    if any(k == key for k, _s, _e in _CODE_EXPERT):
        return "expert"
    if any(k == key for k, _s, _e in _CODE_HARD):
        return "hard"
    return "medium" if key in _CODE_MEDIUM else "easy"

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
    # ONE namespace, as a module has. Separate globals and locals is class-body
    # scoping: a function in the snippet could not see a name defined beside
    # it, so a snippet like `log = []; def g(): log.append(...)` raised
    # NameError here although it runs fine as a file. The original twenty-eight
    # give the same values either way (checked in v0.181.211); the traced
    # snippets need this.
    ns: dict = {"__builtins__": __builtins__}
    exec(compile(src, "<bench-snippet>", "exec"), ns)
    return eval(expr, ns)                                        # noqa: S307


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
    for key, src, expr in _CODE_SNIPPETS + _CODE_HARD + _CODE_EXPERT:
        try:
            value = _run_snippet(src, expr)
        except Exception as e:                                    # noqa: BLE001
            refused.append(f"{key}: {type(e).__name__}")
            continue
        out.append({
            "id": f"CODE-{key}",
            "task_id": key,
            "tags": ["codereason", "python", "code", f"tier:{code_tier(key)}"],
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
            "tags": ["codereason", "python", "exception", "code",
                     f"tier:{code_tier(key)}"],
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


def _record(svc, model: str, backend: str, result, wall_ms: float,
            max_tokens: int, label: str = "", persona: str = "") -> None:
    """One model call's telemetry, for EVERY path a benchmark asks through.

    v0.181.196 added this to ask() after the brain lane showed 0 samples while
    a 60-case run was going against it; the finder path was a second caller and
    did not get it, so every toolflow run was invisible to the Performance tab
    until v0.181.211. One function, so a third path cannot miss it the same way."""
    try:
        from . import telemetry
        telemetry.record_call(
            svc.db, svc.registry, model=model, backend=backend, result=result,
            persona=persona or "bench", mode="bench",
            wall_ms=wall_ms, max_tokens=max_tokens,
            extra={"run_label": label} if label else None)
    except Exception:                                             # noqa: BLE001
        log.exception("bench: could not record telemetry for %s", model)


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
    _record(svc, model, backend, result, (time.monotonic() - t0) * 1000.0,
            max_tokens, label, persona)
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
                    f"searches={fs['searched']} used={','.join(fs['used']) or '-'} "
                    f"detours={fs['detours']}",
                "error": ""}
    tools = tool_score(ref.get("tool_calls") or [], got.get("tool_calls") or [])
    code, detail = await code_score(svc, got.get("content") or "")
    answer = (answer_score(got.get("content") or "", ref.get("content") or "")
              if not (ref.get("tool_calls") or []) else None)
    exact = value_score(got.get("content") or "", str(ref.get("py_value") or "")) \
        if ref.get("py_value") else \
        exact_score(got.get("content") or "", ref.get("must_include"),
                    ref.get("must_exclude"))
    return {"score": combine(tools, code, answer, exact), "tools": tools, "code": code,
            "answer": answer, "exact": exact, "code_detail": detail, "error": ""}


def _row(db, sql, args=()):
    db.execute(sql, args)


def suite_hash(svc, suite: str) -> str:
    """The suite's content and the scorer version, as twelve hex characters.

    Two runs with the same name and a different hash are different tests: the
    needle suite rebuilt with 96k contexts is not the 32k one, and a toolflow
    run scored before v0.181.211 is not scored like one after it."""
    import hashlib
    h = hashlib.sha1(f"scorer:{SCORER_VERSION}\n".encode())
    try:
        h.update((_dir(svc) / f"{suite}.jsonl").read_bytes())
    except OSError:
        return ""
    return h.hexdigest()[:12]


def expected_of(case: dict) -> str:
    """What a case wanted, in words a person reads next to the answer."""
    ref = case.get("reference") or {}
    if ref.get("deliverable"):
        return "a deliverable (no answer key)"
    if ref.get("finder_tool"):
        return f"use {ref['finder_tool']}"
    if ref.get("py_value"):
        return str(ref["py_value"])
    if ref.get("must_include"):
        s = " and ".join(str(x) for x in ref["must_include"])
        if ref.get("must_exclude"):
            s += f" (not the decoy {', '.join(str(x) for x in ref['must_exclude'])}" \
                 + (f" for {ref['decoy_task']}" if ref.get("decoy_task") else "") + ")"
        return s
    calls = [t.get("name") for t in ref.get("tool_calls") or [] if t.get("name")]
    if calls:
        return "call " + ", then ".join(calls)
    return "answer in words, no tool"


def answer_of(got: dict) -> str:
    """What the model did, kept for reading a failure: its text, and the tools
    it called when it called any."""
    parts = []
    f = got.get("finder") or {}
    if f:
        if f.get("used"):
            parts.append("used " + " → ".join(f["used"]))
        parts.append(f"{f.get('searched', 0)} search(es), "
                     + ("the right tool came up" if f.get("found")
                        else "the right tool never came up"))
        if f.get("queries"):
            parts.append("searched: " + "; ".join(json.dumps(q) for q in f["queries"]))
        if f.get("said"):
            parts.append("then said: " + f["said"].strip())
    else:
        calls = [t.get("name") for t in got.get("tool_calls") or [] if t.get("name")]
        if calls:
            parts.append("called " + ", ".join(calls))
    text = (got.get("content") or "").strip()
    if text and not f:
        parts.append(text)
    if got.get("error"):
        parts.append("error: " + str(got["error"]))
    return "\n".join(parts)[:KEEP_ANSWER]


async def _run(svc, suite: str, model: str, label: str, limit: int,
               use_recall: bool = False, use_lessons: bool = False) -> dict:
    db = svc.db
    items = cases(svc, suite, limit)
    if not items:
        raise ValueError(f"the suite {suite!r} has no cases")
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status, "
               "recall, lessons, suite_hash) "
               "VALUES (datetime('now'), ?, ?, ?, ?, 0, 'running', ?, ?, ?)",
               (suite, model, label, len(items), 1 if use_recall else 0,
                1 if use_lessons else 0, suite_hash(svc, suite)))
    run_id = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    # a fresh state for every run (v0.181.211). start() clears it, but a
    # profile calls _run directly, suite after suite, so the previous run's
    # finished/scored/delivered/seconds showed on the live status as if they
    # were this run's - and a stop left over from a stopped profile would have
    # ended every suite of the NEXT profile at its first case. A stop asked
    # for in the moment between start() and here is kept: that one is meant
    # for this run.
    keep_stop = _state.get("state") == "running" and bool(_state.get("stop"))
    _state.clear()
    if keep_stop:
        _state["stop"] = True
    _state.update({"state": "running", "run_id": run_id, "suite": suite, "model": model,
                   "cases": len(items), "done": 0, "started": time.time(), "score": None,
                   "recall": bool(use_recall), "recalled": 0,
                   "lessons": bool(use_lessons), "ruled": 0,
                   # live, for the progress strip: how fast, how well, how long
                   "model_seconds": 0.0, "out_tokens": 0, "in_tokens": 0,
                   "passed": 0, "errors": 0, "last": None,
                   "case_started": time.time()})
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
            "error, recalled, deliverable, answer, expected) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, case.get("id") or "", case.get("task_id") or "",
             json.dumps(case.get("tags") or []),
             json.dumps([t.get("name") for t in (case.get("reference") or {}).get("tool_calls") or []]),
             json.dumps([t.get("name") for t in got.get("tool_calls") or []]),
             s["tools"], None if s["code"] is None else int(s["code"] >= 1), s["code_detail"],
             len(got.get("content") or ""), s["score"], got.get("seconds") or 0, s["error"],
             json.dumps([h["case_id"] for h in hits]),
             (got.get("content") or "")
             if (case.get("reference") or {}).get("deliverable") else "",
             answer_of(got), expected_of(case)))
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
        _state["model_seconds"] = round(totals["seconds"], 2)
        _state["out_tokens"] = totals["completion"]
        _state["in_tokens"] = totals["prompt"]
        _state["errors"] = totals["errors"]
        if s["score"] is not None and s["score"] >= 100:
            _state["passed"] = _state.get("passed", 0) + 1
        _state["last"] = {"case": case.get("id") or "", "score": s["score"],
                          "seconds": got.get("seconds") or 0,
                          "error": (s["error"] or "")[:160]}
        _state["case_started"] = time.time()
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


def state() -> dict:
    """The run in progress, without needing a service.

    benchprofile has to know whether a single-suite run is already going
    before it starts a profile - two at once would be two models on one GPU -
    and reaching into `_state` from another module is how that check silently
    stops matching the one `status` reports."""
    return dict(_state)


def status(svc) -> dict:
    return {"run": state(), "suites": suites(svc)}


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
