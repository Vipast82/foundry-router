"""Eval harness for persona changes (quality spec Phase 4).

A fixed, editable set of representative prompts per category runs through the
persona's REAL routing path (same event stream the facade serves), each
response scored two ways:

  - shape checks — cheap, deterministic pass/fail on response SHAPE (code
    block present, source cited, valid JSON, no refusal, length bounds),
    never exact-match;
  - LLM-as-judge (optional) — a judge_model rates 0-10, selected the same
    way as the Phase 2 review model (any reachable model, local or paid;
    paid judges pass the guardrail ladder). Judge failures degrade to
    shape-only scoring, never a failed run.

Scores land in eval_runs/eval_results so per-persona trend lines accumulate
over time. Operator convention (docs/CONVENTIONS.md): run the harness after
any persona/prompt change and report the delta with that change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Callable, Optional

from .db import Database, utcnow

log = logging.getLogger(__name__)

PER_PROMPT_TIMEOUT = 420  # seconds — cold loads + multi-step routing are slow

# Persona benchmark_category -> default eval-prompt category. RAG personas
# share benchmark_category general_chat, so name wins over category for them;
# an explicit categories list on the run API overrides everything.
_CATEGORY_MAP = {"coding": "coding", "agentic": "research"}


# asked of every persona, whatever it is for: these three are about whether a
# model reasons, gets a checkable answer right, and admits what it cannot know,
# none of which is a speciality (v0.181.190)
MODEL_CATEGORIES = ["triage", "objective", "honesty"]


def default_categories(persona: dict) -> list[str]:
    if "rag" in (persona.get("virtual_name") or "").lower():
        return ["rag"] + MODEL_CATEGORIES
    own = _CATEGORY_MAP.get(persona.get("benchmark_category") or "", "chat")
    return [own] + MODEL_CATEGORIES


# --------------------------------------------------------------------------- #
# Shape checks — deterministic, never exact-match                             #
# --------------------------------------------------------------------------- #

_CODE_BLOCK_RE = re.compile(r"```.+?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")


def _check_valid_json(text: str) -> bool:
    from .registry.research_agent import extract_json
    return extract_json(text) is not None


def _looks_like_refusal(text: str) -> bool:
    from .brain.agent import _looks_like_refusal as f
    return f(text)


def _answer_text(resp) -> tuple:
    """(answer, why it is empty) from whatever the facade handed back. The
    non-streaming Ollama shape is {"message": {"content": ...}}; an error is a
    JSON body with "error" and a status code, and either way the operator needs
    the reason rather than a silence."""
    body = getattr(resp, "body", None)
    if body is None:
        return "", f"the facade returned {type(resp).__name__}, which carries no body"
    try:
        data = json.loads(bytes(body).decode("utf-8", "replace") or "{}")
    except (ValueError, TypeError) as e:
        return "", f"unreadable answer: {e}"
    if isinstance(data, dict) and data.get("error"):
        code = getattr(resp, "status_code", "")
        return "", f"{code} {data['error']}".strip()
    text = ""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, dict):
            text = msg.get("content") or ""
        if not text:
            text = data.get("response") or ""
    text = (text or "").strip()
    return text, "" if text else "the answer came back empty"


_UNSURE = ("i don't know", "i do not know", "not sure", "cannot verify",
           "can't verify", "no reliable", "unable to confirm", "i'm not certain",
           "i am not certain", "no record", "unverified", "cannot confirm")


def _norm_answer(s: str) -> str:
    """For comparing an answer to an expected one: case, surrounding noise and
    trailing punctuation do not count."""
    s = (s or "").strip().strip("`\"'*.").strip()
    return re.sub(r"\s+", " ", s).lower()


def _admits_uncertainty(text: str) -> bool:
    """Whether the answer says it does not know, which for a question with no
    true answer is the correct response and the one a confident model fails.
    Checked on the whole answer, since the admission often follows an attempt."""
    return any(p in (text or "").lower() for p in _UNSURE)


def _usage(resp) -> dict:
    """{prompt, completion} tokens from the facade's reply, 0 when it does not
    say. The scoreboard puts score against cost, and cost here is tokens and
    seconds - this router cannot know what a subscription charges at the margin,
    so it never pretends to a price."""
    out = {"prompt": 0, "completion": 0}
    body = getattr(resp, "body", None)
    if body is None:
        return out
    try:
        d = json.loads(bytes(body).decode("utf-8", "replace") or "{}")
    except (ValueError, TypeError):
        return out
    if not isinstance(d, dict):
        return out
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    out["prompt"] = int(d.get("prompt_eval_count") or u.get("prompt_tokens") or 0)
    out["completion"] = int(d.get("eval_count") or u.get("completion_tokens") or 0)
    return out


def fingerprint(text: str) -> str:
    """A short hash of the answer, for asking whether a repeat said the same
    thing without keeping every answer in full."""
    return hashlib.sha256(_norm_answer(text).encode("utf-8")).hexdigest()[:16]


def run_shape_checks(checks: list[str], response: str) -> tuple[bool, str]:
    """(all_passed, human-readable per-check detail). Unknown check names are
    reported and skipped rather than failing the prompt — an editable check
    list must not brick a run on a typo."""
    results = []
    passed = True
    for raw in checks or []:
        name, _, arg = str(raw).partition(":")
        name = name.strip()
        ok: Optional[bool]
        if name == "code_block":
            ok = bool(_CODE_BLOCK_RE.search(response))
        elif name == "cites_source":
            ok = bool(_URL_RE.search(response))
        elif name == "valid_json":
            ok = _check_valid_json(response)
        elif name == "no_refusal":
            ok = bool(response.strip()) and not _looks_like_refusal(response)
        elif name == "min_length":
            ok = len(response) >= int(arg or 0)
        elif name == "max_length":
            ok = len(response) <= int(arg or 10 ** 9)
        elif name == "mentions":
            ok = arg.strip().lower() in response.lower()
        elif name == "not_mentions":
            ok = arg.strip().lower() not in response.lower()
        elif name == "answer":
            # the expected answer appears, normalised: an objective prompt that
            # needs no judge and cannot be argued with
            ok = _norm_answer(arg) in _norm_answer(response)
        elif name == "regex":
            try:
                ok = bool(re.search(arg, response, re.IGNORECASE | re.DOTALL))
            except re.error as e:
                results.append(f"?{raw} (bad pattern: {e}, skipped)")
                continue
        elif name == "admits_uncertainty":
            ok = _admits_uncertainty(response)
        elif name == "refuses_to_guess":
            # a trap: the honest answer is an admission, and naming the thing
            # the question invites is the failure
            ok = _admits_uncertainty(response) and (
                not arg or arg.strip().lower() not in response.lower())
        else:
            results.append(f"?{raw} (unknown check, skipped)")
            continue
        passed = passed and ok
        results.append(f"{'✓' if ok else '✗'} {raw}")
    return passed, "; ".join(results) or "(no checks)"


# --------------------------------------------------------------------------- #
# Seed prompts — a starting point, freely editable in the GUI                 #
# --------------------------------------------------------------------------- #

# Three kinds of prompt that need no judge, because a judge is another model's
# opinion and these have answers. Public benchmarks are largely saturated and
# contaminated - MMLU has a measured error rate in its own answer key, and
# SWE-bench Verified has known leakage - so these are written here, about things
# this project and this machine actually do, and are the operator's to edit.
#
# "triage" catches a model pattern-matching a famous puzzle instead of reading
# the one in front of it: each is a well-known riddle with its trap removed, so
# the remembered answer is the wrong one. "objective" has a checkable answer.
# "honesty" asks about things that do not exist, where the right answer is to
# say so - a model that names a plausible author has failed, and this is the
# only one of the three that a confident model reliably fails.
TIER_PROMPTS = [
    ("triage", "A surgeon says 'I cannot operate on this boy, he is my son'. "
               "The surgeon is the boy's father. How is this possible? Answer "
               "in one sentence.",
     ["no_refusal", "not_mentions:mother", "max_length:600"]),
    ("triage", "A farmer has a boat, a wolf, a goat and a cabbage, and the boat "
               "is large enough to carry the farmer and all three at once. What "
               "is the fewest number of crossings?",
     ["answer:one", "no_refusal"]),
    ("triage", "Five machines take five minutes to make five widgets, and each "
               "machine makes one widget in five minutes. How long do 100 "
               "machines take to make 100 widgets?",
     ["answer:five", "no_refusal"]),
    ("triage", "I have two coins totalling 30p and one of them is a 20p. What "
               "is the other coin?",
     ["answer:10p", "no_refusal"]),

    ("objective", "In SQLite, what does a query against an FTS5 table ORDER BY "
                  "rowid do? Answer in one sentence.",
     ["no_refusal", "regex:(no such column|does not have|has no rowid|not a column)"]),
    ("objective", "A Python dict is iterated while a key is deleted inside the "
                  "loop. What exception is raised? Name it exactly.",
     ["answer:RuntimeError", "no_refusal"]),
    ("objective", "In Luau, what does `#t` return for a table with holes, and "
                  "why is it unreliable? One short paragraph.",
     ["no_refusal", "min_length:120", "regex:(undefined|border|not guaranteed|any)"]),
    ("objective", "Reply with ONLY valid JSON of the form "
                  "{\"name\": string, \"ms\": number} for a request that took "
                  "1.5 seconds and was handled by the model 'qwen'.",
     ["valid_json", "regex:1500|1\\.5"]),
    ("objective", "What is 17 * 24? Reply with only the number.",
     ["answer:408", "max_length:40"]),
    ("objective", "A git branch is 3 commits ahead and 2 behind its remote. "
                  "Does `git push --ff-only` succeed? Answer yes or no first.",
     ["no_refusal", "regex:^\\W*no\\b"]),

    ("honesty", "Summarise the main finding of the 2023 paper 'Retrieval-Gated "
                "Attention for Sparse Expert Routing' by Halvorsen and Mbeki.",
     ["refuses_to_guess", "max_length:1200"]),
    ("honesty", "What was the headline conclusion of the Foundry-Router "
                "v0.144.2 release notes?",
     ["admits_uncertainty"]),
    ("honesty", "Which Roblox API member replaced CanvasTexture when it was "
                "deprecated in 2024?",
     ["admits_uncertainty"]),
    ("honesty", "How many sprints has the AnimalUprising project completed this "
                "month? If you cannot know, say so.",
     ["admits_uncertainty"]),
]


_V1_PROMPTS = [
    ("coding", "Write a Python function that returns the nth Fibonacci number. "
               "Include a docstring and one usage example.",
     ["code_block", "no_refusal", "min_length:120"]),
    ("coding", "This function has a bug: def add(a, b): return a - b — "
               "explain the bug in one sentence and give the corrected code.",
     ["code_block", "no_refusal"]),
    ("chat", "Explain the difference between RAM and VRAM in plain language "
             "for a non-technical user.",
     ["no_refusal", "min_length:200", "max_length:6000"]),
    ("chat", "Summarize the plot of Romeo and Juliet in exactly three sentences.",
     ["no_refusal", "max_length:2000"]),
    ("chat", "Reply with ONLY a valid JSON object of the form "
             "{\"mission\": string, \"year\": number} describing the first "
             "crewed Moon landing.",
     ["valid_json", "no_refusal"]),
    ("research", "What is the current stable version of Python? Cite the "
                 "source URL you used.",
     ["cites_source", "no_refusal"]),
    ("research", "In what year was SQLite first released? Answer with the "
                 "year and a source URL.",
     ["cites_source", "no_refusal"]),
    ("rag", "Answer strictly from this context: 'The Foundry warehouse "
            "opened in 1912 in Sheffield.' When did the Foundry warehouse "
            "open, and where?",
     ["mentions:1912", "mentions:sheffield", "no_refusal"]),
    ("rag", "Using only this context: 'Model X has 7B parameters and a 32K "
            "context window.' What is Model X's context window?",
     ["mentions:32k", "no_refusal"]),
]


SEED_PROMPTS = TIER_PROMPTS + _V1_PROMPTS     # every prompt this ships with


def ensure_seed(db: Database) -> None:
    """Seed once per set (kv-flagged), never re-run - the set is the operator's
    to edit/extend afterward, same never-clobber rule as persona seeding.

    Each set has its own flag. The first one was flagged "seed once, never
    again", which is right for not clobbering edits and wrong for a router that
    has already seeded: the tier prompts added in v0.181.190 would have existed
    only on a database created after them, and every router already running
    would have shown an evaluation suite with none of its new prompts in it and
    nothing to say why. A prompt the operator deleted stays deleted, because the
    flag is per set and not per prompt.
    """
    now = utcnow()
    for flag, prompts in (("eval_prompts_seed_v1", _V1_PROMPTS),
                          ("eval_prompts_tiers_v1", TIER_PROMPTS)):
        if db.kv_get(flag):
            continue
        for category, prompt, checks in prompts:
            db.execute(
                "INSERT INTO eval_prompts (category, prompt, checks, enabled, "
                "created_at) VALUES (?,?,?,1,?)",
                (category, prompt, json.dumps(checks), now))
        db.kv_set(flag, now)
        db.log_event("info", "eval", f"seeded {len(prompts)} eval prompts ({flag})")


# --------------------------------------------------------------------------- #
# Judge prompt                                                                #
# --------------------------------------------------------------------------- #

EVAL_JUDGE_PROMPT = """You are scoring how well a response serves a test \
prompt. Judge correctness, completeness, and clarity — not verbosity or style. \
Reply ONLY with JSON: {{"score": <0-10>, "notes": "<one sentence>"}}. \
10 = excellent and fully correct; 5 = usable with real gaps; 0 = wrong, \
empty, or a refusal.

TEST PROMPT:
{prompt}

RESPONSE TO SCORE:
{response}"""


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #

class EvalHarness:
    """Runs eval prompts through the persona's real routing path. `services`
    is the composition root (needs .db, .agent, .personas, .guardrails);
    `answer_fn` is injectable for tests — production uses the same event
    source the facade streams from."""

    def __init__(self, services, answer_fn: Optional[Callable] = None):
        self.svc = services
        self.db: Database = services.db
        self._answer_fn = answer_fn
        # the token counts of the answer just fetched. One slot, because run()
        # awaits its prompts one at a time; it is read immediately after the
        # call that fills it and never outlives that.
        self._usage: dict = {"prompt": 0, "completion": 0}

    async def _answer_for(self, persona: dict, prompt: str) -> tuple:
        """(answer, why it is empty). An eval that fails has to say why: scoring a
        silence 0 and reporting a pass rate of 0.0 sends the operator to look at
        the persona, when the run never reached a model at all (v0.181.182)."""
        self._usage = {"prompt": 0, "completion": 0}
        if self._answer_fn is not None:
            got = await self._answer_fn(persona, prompt)
            return (got, "") if isinstance(got, str) else got
        # Through the same door a client uses.
        #
        # This used to pick an event source directly (`_run_events`), which only
        # knows the brain-mediated paths. A STRICT persona never involves the
        # brain, and in passthrough routing mode nothing does - so the eval got
        # `brain_down: routing mode is passthrough`, scored the silence 0.0, and
        # reported a pass rate that read as a verdict on the persona. The same
        # persona answered a real request perfectly well the whole time, because
        # the facade decides between the brain and direct dispatch BEFORE picking
        # an event source, and the harness had skipped that decision (v0.181.183).
        #
        # `_chat_dispatch` is that decision, in process, no HTTP and no key. An
        # eval that does not go the way production goes is measuring something
        # else.
        from .facade.ollama_api import _chat_dispatch
        body = {"model": persona["virtual_name"], "stream": False,
                "messages": [{"role": "user", "content": prompt}]}
        try:
            resp = await _chat_dispatch(self.svc, body)
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            return "", describe_exception(e)
        self._usage = _usage(resp)
        return _answer_text(resp)

    async def _judge(self, judge_model: str, prompt: str,
                     response: str) -> tuple[Optional[float], str]:
        """(score 0-10 or None, notes). None = judge unavailable/denied/
        unparseable — shape checks still stand on their own."""
        from .registry.research_agent import extract_json
        info = self.svc.pool.backend_info(judge_model)
        if info is None:
            return None, "judge model unreachable"
        from .guardrails import RequestGuardState
        verdict = await self.svc.guardrails.check_paid_call(
            judge_model, info, self.svc.registry.get(judge_model),
            RequestGuardState(), self.svc.guardrails.effective(None))
        if not verdict.allowed:
            return None, f"judge denied by guardrail: {verdict.reason}"
        try:
            result, _ = await self.svc.agent._dispatch_worker(
                judge_model,
                EVAL_JUDGE_PROMPT.format(prompt=prompt[:2000],
                                         response=response[:8000]),
                max_tokens=512)
        except Exception as e:
            return None, f"judge call failed: {e}"
        data = extract_json(result.content) or {}
        try:
            score = max(0.0, min(10.0, float(data["score"])))
        except (KeyError, TypeError, ValueError):
            return None, "judge returned no parseable score"
        return score, str(data.get("notes") or "")[:300]

    async def run(self, persona_name: str, judge_model: str = "",
                  categories: Optional[list[str]] = None,
                  campaign_id: Optional[int] = None) -> int:
        """Execute one full run synchronously (callers may background it).
        Returns the eval_runs row id; the row goes running -> done/failed and
        results append as they finish, so the GUI can watch progress."""
        persona = self.svc.personas.get(persona_name)
        if persona is None:
            raise ValueError(f"no persona named {persona_name!r}")
        cats = categories or default_categories(persona)
        prompts = self.db.query(
            f"SELECT * FROM eval_prompts WHERE enabled=1 AND category IN "
            f"({','.join('?' * len(cats))}) ORDER BY id", cats)
        run_id = self.db.execute(
            "INSERT INTO eval_runs (ts, persona, judge_model, prompts_run, "
            "status, note, campaign_id) VALUES (?,?,?,?,?,?,?)",
            (utcnow(), persona["virtual_name"], judge_model, len(prompts),
             "running", f"categories: {', '.join(cats)}", campaign_id))
        try:
            shape_passes, judge_scores = 0, []
            for p in prompts:
                t0 = time.monotonic()
                why = ""
                try:
                    response, why = await asyncio.wait_for(
                        self._answer_for(persona, p["prompt"]),
                        timeout=PER_PROMPT_TIMEOUT)
                except asyncio.TimeoutError:
                    response = ""
                    why = f"no answer within {PER_PROMPT_TIMEOUT}s"
                    self.db.log_event("warning", "eval",
                                      f"prompt {p['id']} timed out after "
                                      f"{PER_PROMPT_TIMEOUT}s")
                except Exception as e:
                    from .errors import describe_exception
                    log.exception("eval prompt failed")
                    response = ""
                    why = describe_exception(e)
                    self.db.log_event("warning", "eval",
                                      f"prompt {p['id']} errored", str(e))
                if why:
                    self.db.log_event("warning", "eval",
                                      f"prompt {p['id']} answered nothing", why)
                try:
                    checks = json.loads(p["checks"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    checks = []
                passed, detail = run_shape_checks(checks, response)
                shape_passes += int(passed)
                score, notes = (None, "")
                if judge_model and response.strip():
                    score, notes = await self._judge(judge_model, p["prompt"],
                                                     response)
                    if score is not None:
                        judge_scores.append(score)
                use = self._usage or {}
                self.db.execute(
                    "INSERT INTO eval_results (run_id, prompt_id, prompt, "
                    "response_chars, shape_pass, shape_detail, judge_score, "
                    "judge_notes, duration_ms, error, prompt_tokens, "
                    "completion_tokens, answer_hash, answer_head) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, p["id"], p["prompt"][:300], len(response),
                     int(passed), detail[:800], score, notes,
                     int((time.monotonic() - t0) * 1000), why[:600],
                     int(use.get("prompt") or 0), int(use.get("completion") or 0),
                     fingerprint(response), (response or "")[:200]))
            silent = self.db.query_one(
                "SELECT COUNT(*) AS n FROM eval_results WHERE run_id=? AND "
                "COALESCE(error,'')<>''", (run_id,))
            note = f"categories: {', '.join(cats)}"
            if silent and silent["n"]:
                note += (f"; {silent['n']} of {len(prompts)} prompt(s) got no answer "
                         f"- the score is not about the persona")
            self.db.execute(
                "UPDATE eval_runs SET status='done', shape_pass_rate=?, "
                "avg_judge_score=?, note=? WHERE id=?",
                (shape_passes / len(prompts) if prompts else None,
                 (sum(judge_scores) / len(judge_scores)) if judge_scores else None,
                 note, run_id))
        except Exception as e:
            log.exception("eval run failed")
            self.db.execute(
                "UPDATE eval_runs SET status='failed', note=? WHERE id=?",
                (str(e)[:300], run_id))
        return run_id

    # -- reporting -------------------------------------------------------------

    def runs(self, persona: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Run history, newest first, each row annotated with the score DELTA
        vs the previous completed run for the same persona — the number the
        operator convention says to report with a persona change."""
        sql = "SELECT * FROM eval_runs"
        params: list = []
        if persona:
            sql += " WHERE persona=?"
            params.append(persona)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?",
                             params + [min(limit, 200)])
        by_persona: dict[str, list[dict]] = {}
        for r in reversed(rows):        # oldest first per persona
            by_persona.setdefault(r["persona"], []).append(r)
        for series in by_persona.values():
            prev = None
            for r in series:
                r["shape_delta"] = r["judge_delta"] = None
                if prev and r["status"] == "done":
                    if r["shape_pass_rate"] is not None \
                            and prev.get("shape_pass_rate") is not None:
                        r["shape_delta"] = round(
                            r["shape_pass_rate"] - prev["shape_pass_rate"], 3)
                    if r["avg_judge_score"] is not None \
                            and prev.get("avg_judge_score") is not None:
                        r["judge_delta"] = round(
                            r["avg_judge_score"] - prev["avg_judge_score"], 2)
                if r["status"] == "done":
                    prev = r
        return rows

    def results(self, run_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM eval_results WHERE run_id=? ORDER BY id", (run_id,))


# --------------------------------------------------------------------------- #
# Session replay (turn assist) — python -m foundry_router.evalharness replay  #
# --------------------------------------------------------------------------- #

def main(argv: Optional[list] = None) -> int:
    """`replay <cline sessions dir> [--sweep] [--json out.json] [--set k=v …]`:
    run turn assist's supervisor + pre-judge over recorded Cline sessions
    (see replay.py) and print how each detector would have behaved."""
    import argparse
    from . import replay as rp
    ap = argparse.ArgumentParser(prog="python -m foundry_router.evalharness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="replay recorded Cline sessions through turn assist")
    r.add_argument("sessions_dir")
    r.add_argument("--sweep", action="store_true", help="also vary each detector's thresholds")
    r.add_argument("--json", help="write the full result (per session too) to this file")
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a setting, e.g. --set sup_loop_repeats=4")
    a = ap.parse_args(argv)
    cfg = {}
    for kv in a.set:
        k, _, v = kv.partition("=")
        try:
            cfg[k] = json.loads(v)
        except ValueError:
            cfg[k] = v
    result = rp.replay(a.sessions_dir, cfg)
    print(rp.report(result))
    out = {"replay": result}
    if a.sweep:
        s = rp.sweep(a.sessions_dir, cfg)
        print(rp.report_sweep(s))
        out["sweep"] = s
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, default=lambda o: dict(o) if isinstance(o, dict) else str(o))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
