"""The judge protocol: one model's opinion, made as hard to fool as it can be.

The harness has had a judge since Phase 4, and it is the weakest kind there is:
one model reads one answer and gives it 0-10. That number cannot be checked
against anything, drifts with the judge's mood about length and formatting, and
is worth most where there is no right answer - which is exactly where it is
least verifiable. Eval tier 2 (pairwise with style control) and tier 4 (blind
deliverable comparison) have both been "not here yet" since v0.181.190 for that
reason, and the agent tasks need the same thing. So it gets built once, here,
and all three use it rather than growing three judging schemes that drift apart.

What this protocol does that an absolute score does not:

**It asks which of two, not how good.** A judge comparing two answers to the
same prompt is answering a question it can actually hold in mind. "Is this an 8
or a 7" is not.

**It judges every pair twice, with the sides swapped.** Position bias is the
largest measured artifact in pairwise LLM judging - a judge that prefers
whatever it reads first will hand you a clean, repeatable, meaningless result.
A verdict counts only when both orders agree; when they disagree the pair is a
tie and `flips` records it. A high flip rate means the judge could not tell
these apart, which is a finding and not a failure, and it is reported rather
than averaged away.

**It is blind.** Sides are A and B. Model and persona names are scrubbed from
the answers too, because a model that signs its work ("As Qwen, I...") is
otherwise identifying itself to the judge.

**No model judges its own work.** A pair containing the judge's own output is
refused with a reason, never quietly scored: self-preference is well measured
and it is the one bias that cannot be cancelled by swapping sides.

**Style is measured and reported, not silently controlled.** Length and markup
are recorded for both sides, and the tally says how often the winner was simply
the longer answer, plus the win rate over only those pairs whose lengths are
within a quarter of each other. That is an honest style control: it shows the
confound and lets the operator see the number with it removed, rather than
applying a regression this project has no way to validate. A win rate of 70%
where the longer answer won 95% of the time is a report about length.

**A Fable model is never a judge.** Refused here as well as left out of the
picker, because the picker is not the only way in.

Judge failures degrade to no verdict, never a failed run - the same contract the
absolute judge has always had.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Both orders of every pair, so a judge that prefers position 1 cancels out.
ORDERS = ("ab", "ba")

# How close two answers must be in length to count as length-matched: the
# shorter is at least this fraction of the longer.
MATCHED_RATIO = 0.75


# --------------------------------------------------------------------------- #
# Who may judge                                                               #
# --------------------------------------------------------------------------- #

_FABLE = re.compile(r"\bfable\b|\bmythos\b", re.IGNORECASE)


def is_fable(model_id: str) -> bool:
    """Fable (and the mythos tier alias) may never judge, score or test here.

    The operator's rule, stated plainly: it "should not be an option ever".
    Enforced in the protocol rather than only in the dropdown, because a POST
    to the run API does not go through the dropdown."""
    return bool(_FABLE.search(model_id or ""))


def judge_refusal(judge_model: str, left: str = "", right: str = "") -> str:
    """Why this judge may not judge this pair, or "" when it may.

    A reason, not a boolean: a refused comparison has to say what it refused,
    or the operator sees a missing number and goes looking at the models."""
    if not (judge_model or "").strip():
        return "no judge model given"
    if is_fable(judge_model):
        return "a Fable model is never a judge"
    for side in (left, right):
        if side and _same_model(judge_model, side):
            return f"{judge_model} cannot judge its own answer"
    return ""


def _same_model(a: str, b: str) -> bool:
    """Same underlying model, however the two ids are spelled.

    A persona's virtual name and the model behind it are different strings for
    the same weights, so comparing the ids exactly would let a model judge
    itself through a persona. Compared on the last path segment, lowercased,
    with separators dropped."""
    return _norm_model(a) == _norm_model(b) and bool(_norm_model(a))


def _norm_model(m: str) -> str:
    """An id reduced to the characters that identify the weights.

    The provider prefix goes, and `:latest` goes, but no other tag does: an
    Ollama tag often carries the SIZE (`qwen3:30b` against `qwen3:8b`), so
    taking the name before the colon would make two different models one, and
    taking the part after it turned every `:latest` id into "latest" - which
    let any Ollama model judge its own answer, found by this test rather than
    by reading the function."""
    tail = (m or "").strip().split("/")[-1]
    parts = [p for p in tail.split(":") if p.strip().lower() != "latest"]
    return re.sub(r"[^a-z0-9]", "", ":".join(parts).lower())


# --------------------------------------------------------------------------- #
# Blinding and style                                                          #
# --------------------------------------------------------------------------- #

_SIGNATURE_WORDS = (
    "qwen", "llama", "mistral", "gemma", "phi", "deepseek", "claude",
    "opus", "sonnet", "haiku", "gpt", "chatgpt", "openai", "anthropic",
    "gemini", "foundry", "kilo", "strata",
)


def blind(text: str, names=()) -> str:
    """Strip anything that tells the judge who wrote this.

    The persona and model names of the run, plus the family names a model uses
    when it introduces itself - a judge that can tell a local model from a paid
    one is no longer comparing the answers."""
    out = text or ""
    for n in sorted({str(x) for x in names if x}, key=len, reverse=True):
        for piece in {n, _norm_model(n)}:
            if len(piece) >= 3:
                out = re.sub(re.escape(piece), "the author", out,
                             flags=re.IGNORECASE)
    for w in _SIGNATURE_WORDS:
        out = re.sub(rf"\b{w}[\w.\-]*", "the author", out, flags=re.IGNORECASE)
    return out


def style_of(text: str) -> dict:
    """The features a judge is known to reward that are not substance.

    Recorded so the tally can say whether the result is about them."""
    t = text or ""
    return {
        "chars": len(t),
        "words": len(t.split()),
        "code_blocks": t.count("```") // 2,
        "md_marks": len(re.findall(r"^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+\.\s)",
                                   t, re.MULTILINE)),
    }


def length_matched(a_style: dict, b_style: dict,
                   ratio: float = MATCHED_RATIO) -> bool:
    """Whether a pair is close enough in length that length cannot explain it."""
    a, b = int(a_style.get("chars") or 0), int(b_style.get("chars") or 0)
    lo, hi = min(a, b), max(a, b)
    if hi <= 0:
        return False
    return (lo / hi) >= ratio


# --------------------------------------------------------------------------- #
# The judge call                                                              #
# --------------------------------------------------------------------------- #

PAIRWISE_PROMPT = """Two answers to the same task are below, A and B. Say \
which better serves the task.

Judge substance: correctness first, then whether it does what was asked, then \
whether it is clear. Do NOT reward length, headings, bullet lists or \
confidence. A short correct answer beats a long one that is padded or wrong. \
If one answer is wrong and the other is right, the right one wins however it \
is written. Choose "tie" only when you genuinely cannot separate them.

Reply ONLY with JSON: {{"winner": "A" | "B" | "tie", "why": "<one sentence>"}}

TASK:
{prompt}

ANSWER A:
{a}

ANSWER B:
{b}"""

# The same protocol over a whole deliverable, against the task's own acceptance
# criteria (eval tier 4). The criteria are the difference between a comparison
# and a preference poll: a judge asked "which is better" answers with its
# taste, and a judge asked "which meets these" answers about the work. They
# come from the suite the run was built with, not from today's source.
PAIRWISE_CRITERIA_PROMPT = """Two deliverables for the same task are below, A and B. Say which better meets the stated requirements.

Go through the requirements one at a time and prefer the deliverable that meets more of them. A requirement met plainly beats the same requirement described at length. Do NOT reward length, headings, bullet lists or confidence, and do not add requirements of your own. If both meet the same requirements, choose "tie".

Reply ONLY with JSON: {{"winner": "A" | "B" | "tie", "why": "<one sentence>"}}

TASK:
{prompt}

REQUIREMENTS:
{criteria}

DELIVERABLE A:
{a}

DELIVERABLE B:
{b}"""

_WINNERS = {"a": "A", "b": "B", "tie": "tie", "neither": "tie", "both": "tie"}


def parse_verdict(content: str) -> Optional[str]:
    """"A", "B", "tie", or None when the judge said nothing usable.

    None is not a tie. A judge that failed to answer and a judge that could not
    separate two answers are different facts, and averaging the first into the
    second is how a broken judge comes out looking indecisive."""
    from .registry.research_agent import extract_json
    data = extract_json(content or "") or {}
    raw = str(data.get("winner") or "").strip().lower()
    if raw in _WINNERS:
        return _WINNERS[raw]
    # a judge that wrote prose instead of JSON, but wrote it unambiguously
    m = re.search(r"\b(?:answer\s+)?([ab])\b\s+(?:is|wins|better)", content or "",
                  re.IGNORECASE)
    if m:
        return m.group(1).upper()
    if re.search(r"\btie\b|\bequal(?:ly)?\b|cannot separate", content or "",
                 re.IGNORECASE):
        return "tie"
    return None


class Pairwise:
    """Runs the protocol. `services` needs .agent (for _dispatch_worker),
    .pool, .registry and .guardrails; `ask` is injectable for tests, which is
    how both orders and the self-preference refusal are asserted without a
    model."""

    def __init__(self, services, ask=None):
        self.svc = services
        self._ask = ask

    async def _judge_once(self, judge_model: str, prompt: str,
                          a: str, b: str,
                          criteria=()) -> tuple[Optional[str], str]:
        if criteria:
            body = PAIRWISE_CRITERIA_PROMPT.format(
                prompt=(prompt or "")[:4000],
                criteria="\n".join(f"{i}. {c}" for i, c
                                   in enumerate(criteria, start=1)),
                a=(a or "")[:16000], b=(b or "")[:16000])
        else:
            body = PAIRWISE_PROMPT.format(prompt=(prompt or "")[:2000],
                                          a=(a or "")[:8000],
                                          b=(b or "")[:8000])
        if self._ask is not None:
            try:
                content = await self._ask(judge_model, body)
            except Exception as e:                                # noqa: BLE001
                return None, f"judge call failed: {e}"
            return parse_verdict(content), ""
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
                judge_model, body, max_tokens=512)
        except Exception as e:                                    # noqa: BLE001
            return None, f"judge call failed: {e}"
        got = parse_verdict(result.content)
        return got, "" if got else "judge returned no parseable verdict"

    async def compare(self, judge_model: str, prompt: str,
                      left: dict, right: dict, criteria=()) -> dict:
        """One pair, judged both ways round.

        `left`/`right` are {"model": id, "text": answer}. The result names the
        winner by model id, so a caller never has to remember which side was
        A - which is where an off-by-one in a swap test turns into a silent
        reversal of every number on the board."""
        lm, rm = str(left.get("model") or ""), str(right.get("model") or "")
        out = {
            "left": lm, "right": rm, "winner": None, "flip": False,
            "orders": {}, "notes": "",
            "style": {"left": style_of(left.get("text") or ""),
                      "right": style_of(right.get("text") or "")},
        }
        out["matched"] = length_matched(out["style"]["left"],
                                        out["style"]["right"])
        why = judge_refusal(judge_model, lm, rm)
        if why:
            out["notes"] = why
            return out
        names = [lm, rm, left.get("persona") or "", right.get("persona") or ""]
        lt = blind(left.get("text") or "", names)
        rt = blind(right.get("text") or "", names)
        seen = []
        for order in ORDERS:
            a, b = (lt, rt) if order == "ab" else (rt, lt)
            got, note = await self._judge_once(judge_model, prompt, a, b,
                                               criteria=criteria)
            if got in ("A", "B"):
                # back from the side the judge saw to the model that wrote it
                picked = (lm if got == "A" else rm) if order == "ab" \
                    else (rm if got == "A" else lm)
            else:
                picked = got            # "tie" or None
            out["orders"][order] = picked
            if note:
                out["notes"] = (out["notes"] + "; " + note).strip("; ")
            seen.append(picked)
        if None in seen:
            out["notes"] = out["notes"] or "no verdict in one or both orders"
            return out
        if seen[0] == seen[1]:
            out["winner"] = seen[0]     # a model id, or "tie"
            return out
        # the two orders disagree: the judge is reading position, not substance
        out["winner"], out["flip"] = "tie", True
        out["notes"] = (out["notes"] + "; orders disagreed").strip("; ")
        return out


# --------------------------------------------------------------------------- #
# Tally                                                                       #
# --------------------------------------------------------------------------- #

def tally(rows: list, subject: str = "") -> dict:
    """Totals over compared pairs, with the style confound in view.

    `subject` is the model the win rate is FOR; without it the first row's left
    side is used. Pairs with no verdict are counted as `undecided` and left out
    of the rate, because a judge that did not answer is not evidence of a
    loss - the same mistake the board made with timed-out prompts."""
    from .evalboard import wilson
    subject = subject or (rows[0].get("left") if rows else "")
    wins = losses = ties = undecided = flips = 0
    longer_won = decided_sized = 0
    m_wins = m_decided = 0
    for r in rows:
        w = r.get("winner")
        if r.get("flip"):
            flips += 1
        if w is None:
            undecided += 1
            continue
        if w == "tie":
            ties += 1
            continue
        if _same_model(subject, str(w)):
            wins += 1
        else:
            losses += 1
        st = r.get("style") or {}
        a, b = st.get("left") or {}, st.get("right") or {}
        la, lb = int(a.get("chars") or 0), int(b.get("chars") or 0)
        if la != lb:
            decided_sized += 1
            win_left = _same_model(str(w), str(r.get("left") or ""))
            if (win_left and la > lb) or (not win_left and lb > la):
                longer_won += 1
        if r.get("matched"):
            m_decided += 1
            if _same_model(subject, str(w)):
                m_wins += 1
    decided = wins + losses
    lo, hi = wilson(wins, decided) if decided else (0.0, 0.0)
    out = {
        "subject": subject, "wins": wins, "losses": losses, "ties": ties,
        "undecided": undecided, "flips": flips, "pairs": len(rows),
        "decided": decided,
        "win_rate": round(wins / decided, 3) if decided else None,
        "lo": round(lo, 3), "hi": round(hi, 3),
        "flip_rate": round(flips / len(rows), 3) if rows else None,
        "longer_won_pct": round(longer_won / decided_sized, 3)
        if decided_sized else None,
        "matched_pairs": m_decided,
        "matched_win_rate": round(m_wins / m_decided, 3) if m_decided else None,
    }
    out["notes"] = "; ".join(_notes(out))
    return out


def _notes(t: dict) -> list:
    """What the operator has to be told before reading the rate."""
    out = []
    if not t["decided"]:
        out.append("no pair was decided, so there is no win rate")
        return out
    if t["decided"] < 5:
        out.append(f"only {t['decided']} decided pairs: the interval is "
                   f"{round((t['hi'] - t['lo']) * 100)} points wide")
    if (t["flip_rate"] or 0) >= 0.3:
        out.append(f"the two orders disagreed on {round(t['flip_rate'] * 100)}% "
                   "of pairs, so the judge is partly reading position, not "
                   "substance")
    lw = t["longer_won_pct"]
    if lw is not None and lw >= 0.7:
        out.append(f"the longer answer won {round(lw * 100)}% of decided pairs: "
                   "read the length-matched rate instead")
    if t["undecided"]:
        out.append(f"{t['undecided']} pairs got no verdict and are excluded, "
                   "not counted as losses")
    return out


# --------------------------------------------------------------------------- #
# Eval tier 2: two personas, the same prompts, their stored answers           #
# --------------------------------------------------------------------------- #

async def pairwise_campaign(svc, campaign_id: int, judge_model: str,
                            left: str, right: str, ask=None) -> dict:
    """Compare two personas over the answers a campaign already collected.

    Nothing is asked of either persona again: a campaign's answers are what
    they said, and re-asking would compare two fresh samples rather than the
    run the operator is looking at. Each prompt both personas answered becomes
    one pair, judged in both orders.

    A campaign run before v0.181.205 stored only a 200-character head of each
    answer, so there is nothing here to compare, and this says so rather than
    returning an empty comparison that reads like a tie."""
    from .db import utcnow
    db = svc.db
    rows = db.query(
        "SELECT r.persona AS persona, e.prompt_id AS prompt_id, "
        "       e.prompt AS prompt, e.answer_full AS answer_full, "
        "       e.answer_head AS answer_head, COALESCE(e.error,'') AS error "
        "FROM eval_results e JOIN eval_runs r ON r.id = e.run_id "
        "WHERE r.campaign_id=? AND r.persona IN (?,?) ORDER BY e.id",
        (int(campaign_id), left, right))
    out = {"campaign_id": int(campaign_id), "judge_model": judge_model,
           "left": left, "right": right, "pairs": [], "skipped": [],
           "tally": {}, "note": ""}
    why = judge_refusal(judge_model, left, right)
    if why:
        out["note"] = why
        return out

    # the first answer each persona gave to each prompt; a repeat is another
    # sample of the same persona, not a second contestant
    best: dict = {}
    prompts: dict = {}
    for r in rows:
        key = (r["persona"], r["prompt_id"])
        if key in best:
            continue
        prompts.setdefault(r["prompt_id"], r["prompt"] or "")
        best[key] = r
    stored = sum(1 for r in best.values() if (r["answer_full"] or "").strip())
    if not stored:
        out["note"] = (
            f"campaign {campaign_id} stored no answers in full - it ran before "
            "v0.181.205, when only a 200-character head was kept. Run a new "
            "campaign to compare these personas; judging the head would rank "
            "their opening sentences.")
        return out

    pw = Pairwise(svc, ask=ask)
    for pid, prompt in prompts.items():
        lr, rr = best.get((left, pid)), best.get((right, pid))
        if lr is None or rr is None:
            out["skipped"].append({"prompt_id": pid,
                                   "why": "only one persona answered it"})
            continue
        lt, rt = (lr["answer_full"] or ""), (rr["answer_full"] or "")
        if not lt.strip() or not rt.strip():
            # a prompt that timed out is not a loss: the honesty category is
            # where every timeout landed in campaign 2, and scoring a silence
            # against the persona was the fault that reading fixed
            out["skipped"].append({
                "prompt_id": pid,
                "why": (lr["error"] or rr["error"] or "no answer stored")[:200]})
            continue
        got = await pw.compare(judge_model, prompt,
                               {"model": left, "text": lt, "persona": left},
                               {"model": right, "text": rt, "persona": right})
        got["prompt_id"] = pid
        got["prompt"] = prompt[:300]
        out["pairs"].append(got)
        db.execute(
            "INSERT INTO eval_pairs (ts, campaign_id, judge_model, "
            "left_persona, right_persona, prompt_id, prompt, winner, flip, "
            "matched, left_chars, right_chars, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), int(campaign_id), judge_model, left, right, pid,
             prompt[:300], got["winner"] or "", int(bool(got["flip"])),
             int(bool(got["matched"])), got["style"]["left"]["chars"],
             got["style"]["right"]["chars"], (got["notes"] or "")[:300]))
    out["tally"] = tally(out["pairs"], left)
    if out["skipped"]:
        out["tally"]["notes"] = "; ".join(
            x for x in [out["tally"].get("notes"),
                        f"{len(out['skipped'])} prompt(s) had no answer from "
                        "one side and were left out"] if x)
    return out


# --------------------------------------------------------------------------- #
# Eval tier 4: two benchmark runs of the agent-task suite                     #
# --------------------------------------------------------------------------- #

async def pairwise_runs(svc, left_run: int, right_run: int, judge_model: str,
                        ask=None) -> dict:
    """Compare what two models produced for the same deliverable tasks.

    Deliverables are never scored - there is no key - so this is the only thing
    that can be said about them, and it is said with the criteria the suite
    carries rather than a judge's taste."""
    from .agenttasks import criteria_for, deliverables, missing_reason
    from .db import utcnow
    db = svc.db
    out = {"left_run": int(left_run), "right_run": int(right_run),
           "judge_model": judge_model, "left": "", "right": "",
           "pairs": [], "skipped": [], "tally": {}, "note": ""}
    if int(left_run) == int(right_run):
        out["note"] = "a run compared with itself is not a comparison"
        return out
    rows = {}
    for side, rid in (("left", left_run), ("right", right_run)):
        run = db.query_one("SELECT id, suite, model, label FROM bench_runs "
                           "WHERE id=?", (int(rid),))
        if run is None:
            out["note"] = f"there is no run {rid}"
            return out
        out[side] = run["model"] or f"run {rid}"
        out[side + "_suite"] = run["suite"]
        rows[side] = (run, deliverables(db, int(rid)))
        bad = missing_reason(rows[side][1])
        if bad:
            out["note"] = f"run {rid}: {bad}"
            return out
    lrun, lrows = rows["left"]
    rrun, rrows = rows["right"]
    if lrun["suite"] != rrun["suite"]:
        out["note"] = (f"those runs are different suites ({lrun['suite']} and "
                       f"{rrun['suite']}): the tasks would not line up")
        return out
    # the two models may be the same one twice, which is a real thing to measure
    # (its own spread) but not a thing to judge, since the judge would be
    # comparing a model with itself and self-preference cannot be cancelled
    why = judge_refusal(judge_model, out["left"], out["right"])
    if why:
        out["note"] = why
        return out

    by_task = {r["task_id"]: r for r in rrows}
    pw = Pairwise(svc, ask=ask)
    for lr in lrows:
        tid = lr["task_id"]
        rr = by_task.get(tid)
        if rr is None:
            out["skipped"].append({"task_id": tid,
                                   "why": "the other run has no such task"})
            continue
        lt, rt = (lr["deliverable"] or ""), (rr["deliverable"] or "")
        if not lt.strip() or not rt.strip():
            out["skipped"].append({
                "task_id": tid,
                "why": (lr["error"] or rr["error"] or "no deliverable")[:200]})
            continue
        crit = criteria_for(svc, lrun["suite"], tid)
        task = _task_text(svc, lrun["suite"], tid)
        got = await pw.compare(judge_model, task,
                               {"model": out["left"], "text": lt},
                               {"model": out["right"], "text": rt},
                               criteria=crit)
        got["task_id"] = tid
        got["criteria"] = len(crit)
        out["pairs"].append(got)
        db.execute(
            "INSERT INTO eval_pairs (ts, campaign_id, judge_model, "
            "left_persona, right_persona, prompt_id, prompt, winner, flip, "
            "matched, left_chars, right_chars, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), 0, judge_model, out["left"], out["right"], 0,
             f"{lrun['suite']}:{tid}", got["winner"] or "",
             int(bool(got["flip"])), int(bool(got["matched"])),
             got["style"]["left"]["chars"], got["style"]["right"]["chars"],
             (got["notes"] or "")[:300]))
    out["tally"] = tally(out["pairs"], out["left"])
    if out["pairs"] and not crit_seen(out["pairs"]):
        out["tally"]["notes"] = "; ".join(x for x in [
            out["tally"].get("notes"),
            "no task carried acceptance criteria, so this compared the "
            "deliverables on the judge's taste alone"] if x)
    if out["skipped"]:
        out["tally"]["notes"] = "; ".join(x for x in [
            out["tally"].get("notes"),
            f"{len(out['skipped'])} task(s) had no deliverable from one side "
            "and were left out"] if x)
    return out


def crit_seen(pairs: list) -> bool:
    """Whether any pair was judged against stated requirements."""
    return any(int(p.get("criteria") or 0) for p in pairs)


def _task_text(svc, suite: str, task_id: str) -> str:
    """The task as it was asked, from the built suite."""
    from .bench import cases
    for c in cases(svc, suite):
        if (c.get("task_id") or "") == task_id:
            for m in reversed(c.get("messages") or []):
                if m.get("role") == "user":
                    return str(m.get("content") or "")
    return task_id


# --------------------------------------------------------------------------- #
# Calibration: can this judge pick an answer that is already known to be right #
# --------------------------------------------------------------------------- #

def perturb(repr_text: str) -> str:
    """A near miss: the same KIND of answer, definitely wrong.

    Built from the parsed value rather than its text, so "[2, 2, 2]" does not
    become "[2, 2, 2]x". Returns "" when no different value of the same kind
    can be made, and that case is then left out rather than paired with
    itself."""
    import ast
    try:
        v = ast.literal_eval(repr_text)
    except (ValueError, SyntaxError):
        # an exception NAME is not a literal, and it is the near miss that
        # matters for the eight exception cases: telling RuntimeError from
        # KeyError is the whole question those ask
        return _other_exception(repr_text.strip())
    if isinstance(v, bool):
        out = not v
    elif isinstance(v, int):
        out = v + 1
    elif isinstance(v, float):
        out = round(v + 0.5, 6)
    elif isinstance(v, str):
        out = v[:-1] if len(v) > 1 else "x"
    elif isinstance(v, (list, tuple)):
        if len(v) > 1 and list(reversed(list(v))) != list(v):
            out = type(v)(reversed(list(v)))
        elif v:
            out = type(v)(list(v)[:-1])
        else:
            return ""
    elif isinstance(v, dict):
        if not v:
            return ""
        out = {k: v[k] for k in list(v)[:-1]}
    elif isinstance(v, set):
        out = set(list(v)[:-1]) if v else None
        if out is None:
            return ""
    else:
        return ""
    return "" if repr(out) == repr(v) else repr(out)


_EXCEPTIONS = ("TypeError", "ValueError", "KeyError", "IndexError",
               "AttributeError", "RuntimeError", "ZeroDivisionError",
               "StopIteration", "NameError", "UnboundLocalError")


def _other_exception(name: str) -> str:
    """A different exception of the same shape, or "" when this is not one."""
    if name not in _EXCEPTIONS:
        return ""
    i = _EXCEPTIONS.index(name)
    return _EXCEPTIONS[(i + 1) % len(_EXCEPTIONS)]


def _key_of(case: dict) -> str:
    """The case's answer as text, or "" when it has no single key."""
    ref = case.get("reference") or {}
    if ref.get("deliverable"):
        return ""                 # no key by design
    if ref.get("py_value"):
        return str(ref["py_value"])
    inc = ref.get("must_include") or []
    if len(inc) == 1 and str(inc[0]).strip():
        return str(inc[0])
    if ref.get("content") and len(str(ref["content"])) <= 120:
        return str(ref["content"])
    return ""


def _question(case: dict) -> str:
    for m in reversed(case.get("messages") or []):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def calibration_pairs(svc, suite: str, limit: int = 0) -> list:
    """{task, right, wrong, kind} over a suite that has answer keys."""
    from .bench import cases
    rows = [c for c in cases(svc, suite) if _key_of(c) and _question(c)]
    out = []
    for i, c in enumerate(rows):
        right, task = _key_of(c), _question(c)
        other = _key_of(rows[(i + 1) % len(rows)]) if len(rows) > 1 else ""
        if other and other != right:
            out.append({"task_id": c.get("task_id") or "", "task": task,
                        "right": right, "wrong": other, "kind": "other"})
        near = perturb(right)
        if near:
            out.append({"task_id": c.get("task_id") or "", "task": task,
                        "right": right, "wrong": near, "kind": "near"})
    if limit:
        out = out[:int(limit)]
    return out


async def calibrate(svc, judge_model: str, suite: str = "codereason",
                    limit: int = 0, ask=None) -> dict:
    """How often this judge picks the answer the suite already knows is right.

    A floor, not a ceiling: see this module's docstring. Every pair is judged
    in both orders like any other, so a position-reading judge scores no
    agreement rather than 50%."""
    out = {"judge_model": judge_model, "suite": suite, "pairs": [],
           "note": "", "by_kind": {}, "verdict": ""}
    why = judge_refusal(judge_model)
    if why:
        out["note"] = why
        return out
    rows = calibration_pairs(svc, suite, limit)
    if not rows:
        out["note"] = (f"the suite {suite!r} has no cases with a single answer "
                       "key, so there is nothing here a judge can be checked "
                       "against")
        return out
    pw = Pairwise(svc, ask=ask)
    for r in rows:
        got = await pw.compare(judge_model, r["task"],
                               {"model": "right", "text": r["right"]},
                               {"model": "wrong", "text": r["wrong"]})
        out["pairs"].append({
            "task_id": r["task_id"], "kind": r["kind"],
            "right": r["right"], "wrong": r["wrong"],
            "picked": got["winner"], "flip": got["flip"],
            "agreed": got["winner"] == "right",
        })
    for kind in ("other", "near"):
        got = [p for p in out["pairs"] if p["kind"] == kind]
        if not got:
            continue
        decided = [p for p in got if p["picked"] in ("right", "wrong")]
        out["by_kind"][kind] = {
            "pairs": len(got), "decided": len(decided),
            "agreed": sum(1 for p in got if p["agreed"]),
            "rate": round(sum(1 for p in got if p["agreed"]) / len(decided), 3)
            if decided else None,
            "flips": sum(1 for p in got if p["flip"]),
        }
    out["verdict"] = _verdict(out["by_kind"])
    return out


# --------------------------------------------------------------------------- #
# Running a check in the background, and keeping what it found                #
# --------------------------------------------------------------------------- #

_check_state: dict = {"state": "idle"}
_check_lock = threading.Lock()


def check_state() -> dict:
    return dict(_check_state)


def start_check(svc, judge_model: str, suite: str = "codereason",
                limit: int = 0) -> dict:
    """Run a calibration in the background and keep the result.

    Fourteen minutes of judging held open as one HTTP request loses everything
    when the connection drops, which is not a hypothetical - it happened twice
    while this was being used, and a browser gives the operator no timeout to
    raise. The work was fine both times; only the answer was thrown away."""
    from .db import utcnow
    if _check_state.get("state") == "running":
        return dict(_check_state)
    why = judge_refusal(judge_model)
    if why:
        return {"state": "failed", "error": why}
    if not _check_lock.acquire(blocking=False):
        return dict(_check_state)
    db = svc.db
    db.execute("INSERT INTO judge_checks (ts, judge_model, suite, status) "
               "VALUES (?,?,?,'running')", (utcnow(), judge_model, suite))
    cid = db.query_one("SELECT MAX(id) AS id FROM judge_checks")["id"]
    _check_state.clear()
    _check_state.update({"state": "running", "id": cid, "judge_model": judge_model,
                         "suite": suite, "started": time.time()})

    def go():
        import asyncio
        try:
            from .loopclient import run_on_own_loop
            out = run_on_own_loop(svc, calibrate(svc, judge_model, suite, limit))
            db.execute(
                "UPDATE judge_checks SET status=?, pairs=?, verdict=?, "
                "by_kind=?, note=?, detail=? WHERE id=?",
                ("done" if not out.get("note") else "refused",
                 len(out.get("pairs") or []), out.get("verdict") or "",
                 json.dumps(out.get("by_kind") or {}), out.get("note") or "",
                 json.dumps(out.get("pairs") or [])[:200000], cid))
            _check_state.update({"state": "done", "finished": time.time(),
                                 "verdict": out.get("verdict") or "",
                                 "note": out.get("note") or ""})
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            msg = describe_exception(e)
            db.execute("UPDATE judge_checks SET status='failed', note=? "
                       "WHERE id=?", (msg[:400], cid))
            _check_state.update({"state": "failed", "error": msg,
                                 "finished": time.time()})
        finally:
            _check_lock.release()

    threading.Thread(target=go, name="foundry-judge-check", daemon=True).start()
    time.sleep(0.05)
    return dict(_check_state)


def checks(db, limit: int = 30) -> list:
    """Checks already run, newest first, with their detail parsed."""
    rows = db.query("SELECT id, ts, judge_model, suite, pairs, verdict, "
                    "by_kind, note, status FROM judge_checks "
                    "ORDER BY id DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        row = dict(r)
        try:
            row["by_kind"] = json.loads(row.get("by_kind") or "{}")
        except (ValueError, TypeError):
            row["by_kind"] = {}
        out.append(row)
    return out


def check_detail(db, check_id: int) -> dict:
    """One check's pairs, for reading which cases it got wrong - which says
    more than the percentage does."""
    r = db.query_one("SELECT * FROM judge_checks WHERE id=?", (int(check_id),))
    if r is None:
        return {"error": f"there is no check {check_id}"}
    out = dict(r)
    for key in ("by_kind", "detail"):
        try:
            out[key] = json.loads(out.get(key) or ("{}" if key == "by_kind" else "[]"))
        except (ValueError, TypeError):
            out[key] = {} if key == "by_kind" else []
    out["pairs_detail"] = out.pop("detail")
    return out


# A judge that cannot beat an answer to a different question is not a judge.
FLOOR_OTHER = 0.9
# A near miss is the real test, and this is where a weak local model shows.
FLOOR_NEAR = 0.8


def _verdict(by_kind: dict) -> str:
    """Plain words, because a rate on its own invites the wrong reading."""
    o, n = by_kind.get("other") or {}, by_kind.get("near") or {}
    bits = []
    if o.get("rate") is None and n.get("rate") is None:
        return ("this judge decided none of the pairs, so it cannot be "
                "calibrated - treat it as unusable rather than neutral")
    if o.get("rate") is not None:
        bits.append(f"{round(o['rate'] * 100)}% against an answer to another "
                    f"question ({o['agreed']} of {o['decided']} decided)")
    if n.get("rate") is not None:
        bits.append(f"{round(n['rate'] * 100)}% against a near miss "
                    f"({n['agreed']} of {n['decided']} decided)")
    flips = (o.get("flips") or 0) + (n.get("flips") or 0)
    total = (o.get("pairs") or 0) + (n.get("pairs") or 0)
    if flips:
        bits.append(f"{round(100 * flips / total)}% of pairs flipped when the "
                    f"sides were swapped")
    head = "usable"
    if (o.get("rate") or 0) < FLOOR_OTHER:
        head = ("NOT usable: it cannot reliably tell a right answer from the "
                "answer to a different question")
    elif (n.get("rate") or 0) < FLOOR_NEAR:
        head = ("weak: it beats an obviously wrong answer but not a near miss, "
                "so read its win rates as indicative and not as evidence")
    return head + " - " + "; ".join(bits) + (
        ". This is a floor: telling right from wrong is not the same skill as "
        "preferring the better of two defensible answers.")


def pair_history(db, campaign_id: int = 0, limit: int = 200) -> list:
    """Comparisons already made, newest first."""
    if campaign_id:
        return db.query("SELECT * FROM eval_pairs WHERE campaign_id=? "
                        "ORDER BY id DESC LIMIT ?", (int(campaign_id), int(limit)))
    return db.query("SELECT * FROM eval_pairs ORDER BY id DESC LIMIT ?",
                    (int(limit),))
