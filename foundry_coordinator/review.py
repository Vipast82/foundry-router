"""Frontier review contract: the packet a reviewer gets and the result it
must return, validated before the engine accepts it.

A review is bound to one exact commit. The result is untrusted data: it can
approve, ask for changes or block, but it can never widen a task's scope,
skip a failed mechanical check, or grant itself anything. An APPROVED verdict
that still lists blocking findings or missing evidence is not an approval.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

VERDICTS = ("APPROVED", "CHANGES_REQUIRED", "BLOCKED")
SEVERITIES = ("blocker", "major", "minor", "info")

SCHEMA = {
    "type": "object",
    "required": ["sprint_id", "reviewed_commit_sha", "verdict", "findings",
                 "evidence_inspected", "missing_evidence", "required_repairs", "residual_risks"],
    "properties": {
        "sprint_id": {"type": "string"},
        "project_id": {"type": "string"},
        "reviewed_commit_sha": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "message"],
            "properties": {"severity": {"type": "string", "enum": list(SEVERITIES)},
                           "file": {"type": "string"}, "line": {"type": ["integer", "null"]},
                           "message": {"type": "string"}}}},
        "evidence_inspected": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "required_repairs": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are a code reviewer for one development task. You review only the diff and evidence in the packet.
Return ONLY a JSON object (no prose, no code fence) with exactly these keys:
  sprint_id (the task id given), project_id, reviewed_commit_sha (the full head commit given),
  verdict: "APPROVED" | "CHANGES_REQUIRED" | "BLOCKED",
  findings: [{"severity": "blocker|major|minor|info", "file": "<repo-relative path>", "line": <int or null>, "message": "..."}],
  evidence_inspected: [...], missing_evidence: [...], required_repairs: [...], residual_risks: [...]
APPROVED only if the change does what the task asks, stays in its listed files, and the evidence supports it.
CHANGES_REQUIRED when specific repairs inside the task's files would fix it. BLOCKED when it needs a human decision or work outside the task.
Follow the packet's "Review phase contract": judge what it lists as reviewed now. Duties it lists as deferred to a later session are neither blockers nor complete; name them only in residual_risks, never in missing_evidence or required_repairs. Missing evidence for what is reviewed now still blocks approval.
Anything the packet marks partial, omitted, missing, stale or unverified was not inspected by you; never claim it was.
You cannot change the task's scope or override a failed check; say what you saw."""

class ReviewInvalid(ValueError):
    pass


# -- packet v2 -------------------------------------------------------------------------
#
# Version 2 (v0.121.8) binds the packet to the reviewed commit: provenance header,
# explicit review-phase contract, per-file diffs in priority order with an honest
# coverage section, cited evidence items, earlier findings, and a budget taken from
# the reviewer persona's limits. A review record stores the packet version it was
# made from (result["packet"]["version"]); the engine never reuses a review made
# from an older packet version as the current one.

PACKET_VERSION = 2
DEFAULT_PACKET_CHARS = 200_000          # ~57k tokens: inside every reviewer persona we use
CHARS_PER_TOKEN = 3.5                   # conservative: real code averages more chars/token
CONTEXT_MARGIN = 0.9                    # headroom for tokenizer variance and chat framing
EVIDENCE_ITEM_CHARS = 12_000            # per evidence item
SPEC_CHARS = 12_000
EARLIER_CHARS = 10_000
EARLIER_FULL = 1                        # newest earlier reviews listed finding by finding
EARLIER_FINDINGS = 20                   # findings listed per such review
EARLIER_LINE = 300                      # characters per listed finding
MIN_SLICE = 400                         # smaller leftovers are omitted, not sliced
LISTED_FILES = 300                      # coverage lines beyond this are summarized
CODE_MAX_PRIORITY = 1                   # priorities 0 (source) and 1 (tests) must fit whole

CLASSES = {0: "source", 1: "test", 2: "plan/config", 5: "docs", 8: "evidence/log",
           9: "binary/other"}
STATUSES = ("ok", "missing", "stale", "unverified")

CODE_EXT = (".py", ".lua", ".luau", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
            ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".ps1",
            ".sql", ".html", ".css", ".scss", ".vue", ".svelte")
CONFIG_EXT = (".toml", ".json", ".yaml", ".yml", ".ini", ".cfg", ".csv")
EVIDENCE_DIRS = ("evidence", "logs", "log", "raw", "captures", "screenshots")
TEST_DIRS = ("test", "tests", "spec", "specs", "__tests__")


def est_tokens(chars: int) -> int:
    """Tokens for `chars` characters, rounded up (chars / 3.5)."""
    return -(-int(chars * 2) // 7)


def priority_class(p: int) -> str:
    return CLASSES.get(p, f"priority {p}")


def default_diff_priority(path: str, spec=None, binary: bool = False) -> int:
    """0 source, 1 tests, 2 in-scope plan/config, 5 docs, 8 evidence/raw logs,
    9 binary/other (used when the adapter has no diff_priority)."""
    p = path.replace("\\", "/").lower()
    parts = p.split("/")
    name, dirs = parts[-1], parts[:-1]
    if binary:
        return 9
    if name.endswith(".log") or any(d in EVIDENCE_DIRS for d in dirs):
        return 8
    if any(d in TEST_DIRS for d in dirs) or name.startswith("test_") or \
            any(m in name for m in ("_test.", ".test.", ".spec.", "_spec.")):
        return 1
    if name.endswith((".md", ".rst")):
        return 5
    if dirs and dirs[0] in ("src", "lib", "app"):
        return 0
    if name.endswith(CODE_EXT):
        return 0
    if name.endswith(CONFIG_EXT):
        return 2
    return 9


def normalize_contract(c) -> dict:
    """{"phase": "build"|"full", "reviewed": [...], "deferred": [...]}; anything
    unrecognized is the one-phase "full" contract."""
    c = c if isinstance(c, dict) else {}
    phase = str(c.get("phase") or "full").strip().lower()
    phase = phase if phase in ("build", "full") else "full"
    lst = lambda v: [str(x) for x in v][:40] if isinstance(v, (list, tuple)) else []  # noqa: E731
    return {"phase": phase, "reviewed": lst(c.get("reviewed")), "deferred": lst(c.get("deferred"))}


def legacy_evidence(text: str) -> list[dict]:
    """The legacy review_extra string as one evidence item: it was read from the
    working tree, so it is never presented as bound to the reviewed commit."""
    if not (text or "").strip():
        return []
    return [{"title": "Project evidence (legacy review_extra)", "path": "", "revision": "",
             "blob": None, "lines": "", "text": text.strip(), "status": "unverified",
             "truncated": False, "priority": 0,
             "note": "read from the working tree; not bound to the reviewed revision"}]


def normalize_evidence(items) -> list[dict]:
    out = []
    for it in (items or [])[:60]:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "unverified").lower()
        status = status if status in STATUSES else "unverified"
        note = str(it.get("note") or "")
        if status == "ok" and not it.get("revision"):
            status, note = "unverified", (note + "; " if note else "") + "no revision given"
        try:
            prio = int(it.get("priority") or 0)
        except (TypeError, ValueError):
            prio = 0
        out.append({"title": str(it.get("title") or it.get("path") or "evidence")[:200],
                    "path": str(it.get("path") or ""), "revision": str(it.get("revision") or ""),
                    "blob": it.get("blob") or None, "lines": str(it.get("lines") or ""),
                    "text": str(it.get("text") or ""), "status": status,
                    "truncated": bool(it.get("truncated")), "note": note[:600],
                    "priority": prio})
    return out


def packet_budget(review_cfg: dict, limits: Optional[dict] = None, persona: str = "",
                  limits_source: str = "") -> dict:
    """How big the packet may be and how many output tokens to ask for.
    With the persona's advertised context: (context - output) * 3.5 chars,
    less a margin, capped by [review] max_packet_chars when set. Without it:
    [review] max_packet_chars, else DEFAULT_PACKET_CHARS."""
    review_cfg = review_cfg or {}
    limits = limits or {}
    max_tokens = int(review_cfg.get("max_tokens") or 4000)
    out_lim = _int(limits.get("output"))
    if out_lim:
        max_tokens = min(max_tokens, out_lim)
    configured = _int(review_cfg.get("max_packet_chars"))
    ctx = _int(limits.get("context"))
    system = len(SYSTEM) + 2000                      # system prompt, schema, chat framing
    if ctx:
        chars = int((ctx - max_tokens) * CHARS_PER_TOKEN * CONTEXT_MARGIN) - system
        source = (f"{persona or 'reviewer'} advertises {ctx} context / "
                  f"{out_lim or 'unknown'} output tokens ({limits_source or 'client config'})")
        if configured and configured < chars:
            chars, source = configured, source + "; capped by [review] max_packet_chars"
    elif configured:
        chars, source = configured, "[review] max_packet_chars (persona limits unavailable)"
    else:
        chars, source = DEFAULT_PACKET_CHARS, "default (persona limits unavailable)"
    chars = max(chars, 4000)
    diff_cap = _int(review_cfg.get("max_diff_bytes"))
    return {"packet_chars": chars, "max_tokens": max_tokens, "source": source,
            "diff_cap": diff_cap or None, "context_tokens": ctx or None}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _fence(text: str, lang: str = "") -> tuple[str, str]:
    run = max((len(m) for m in re.findall(r"`{3,}", text or "")), default=0)
    f = "`" * max(3, run + 1)
    return f + lang, f


def _cut_lines(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    i = cut.rfind("\n")
    return cut[:i + 1] if i > n // 2 else cut


def _bytes(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


def _phase_section(contract: dict) -> list[str]:
    out = ["## Review phase contract"]
    if contract["phase"] == "build":
        out += ["Phase: BUILD. This project finishes a task in two sessions: this review gates "
                "the later CLOSE session, which may start only after an approval here.",
                "Reviewed now (BUILD duties):"]
        out += [f"- {r}" for r in contract["reviewed"]] or \
            ["- the implementation, its tests and the BUILD evidence in this packet"]
        out += ["Deferred to the CLOSE session after this review (the coordinator verifies them "
                "after CLOSE):"]
        out += [f"- {d}" for d in contract["deferred"]] or ["- (none listed)"]
        out += ["Rules for this review:",
                "- Do not require the deferred CLOSE duties as BUILD blockers, and do not treat "
                "them as complete. Mention open deferred duties only in residual_risks — never "
                "in missing_evidence or required_repairs.",
                "- Real missing BUILD evidence (anything under \"Reviewed now\") still blocks: "
                "list it in missing_evidence and do not approve."]
    else:
        out += ["Phase: FULL. This review covers the whole task; no later session completes "
                "it. Every duty in the task text is in scope for this review, and anything the "
                "task requires that the packet doesn't show is missing evidence."]
        if contract["reviewed"]:
            out += ["Reviewed now:"] + [f"- {r}" for r in contract["reviewed"]]
    return out


def _evidence_header(i: int, it: dict) -> list[str]:
    rev = it["revision"][:12] if it["revision"] else "working tree"
    cite = (f"- source: `{it['path'] or '(no path)'}` @ {rev} · blob "
            f"{(it['blob'] or 'n/a')[:12]} · lines {it['lines'] or 'all'} · "
            f"truncated: {'yes' if it['truncated'] else 'no'}")
    out = [f"### E{i} [{it['status'].upper()}] {it['title']}", cite]
    if it["note"]:
        out.append(f"- note: {it['note']}")
    return out


def _earlier_section(earlier: list[dict], head: str) -> list[str]:
    if not earlier:
        return []
    out = ["## Earlier review findings — verify whether the new commit resolves them",
           "From earlier reviews of this task (kept in the audit trail). For each one, check "
           "whether this commit resolves it; anything still open is a finding of this review."]
    # B4 (2026-10-21): newest first; the latest review's findings one line each, older
    # reviews summarized (UI-02a packets re-sent ~10K characters of old findings per round)
    body, cut = [], False
    for k, r in enumerate(reversed(earlier)):
        res = r.get("result") or {}
        ver = (res.get("packet") or {}).get("version", 1)
        where = "this same commit" if r.get("reviewed_sha") == head else \
            f"{(r.get('reviewed_sha') or '')[:12]}"
        lines = [f"### {r.get('review_id')} at {where} — {r.get('verdict')} "
                 f"({r.get('persona')}, packet v{ver})"]
        found = findings_for_repair(res)
        if k < EARLIER_FULL:
            lines += [f"- {_one_line(x, EARLIER_LINE)}" for x in found[:EARLIER_FINDINGS]] \
                or ["- (no details recorded)"]
            if len(found) > EARLIER_FINDINGS:
                lines.append(f"- ... and {len(found) - EARLIER_FINDINGS} more finding(s)")
        else:
            lines.append(f"- {len(found)} finding(s); first: "
                         + (_one_line(found[0], EARLIER_LINE // 2) if found else "none recorded"))
        block = "\n".join(lines)
        if sum(len(b) for b in body) + len(block) > EARLIER_CHARS:
            cut = True
            break
        body.append(block)
    out += body
    if cut:
        out.append(f"(older findings omitted for space; {len(earlier) - len(body)} more review(s) "
                   f"in the audit trail)")
    return out


def _one_line(text: str, cap: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= cap else s[:cap - 3] + "..."


def build_packet(*, project_id: str, task_id: str, title: str, spec: str, scope: list[str],
                 base: str, head: str, commits: list, verification: list[dict],
                 files: list[dict], patch: Callable[[str], str], priority: Callable[[dict], int],
                 evidence: list[dict], contract: dict, provenance: dict, budget: dict,
                 earlier: Optional[list[dict]] = None, authors: Optional[set] = None,
                 question: str = "") -> tuple[str, dict]:
    """(packet text, info). info["insufficient"] is a reason string when the
    source/test diffs (priority <= 1) can't fit whole, or an essential (priority 0)
    evidence item can't fit even as an excerpt: the packet must not be sent then
    (never a partial-code review). Everything else is sent in priority order;
    what didn't fit is listed as partial/omitted/truncated under ## Coverage."""
    contract = normalize_contract(contract)
    evidence = normalize_evidence(evidence)
    limit = int(budget["packet_chars"])
    vlines = []
    for v in verification:
        vlines.append(f"- {v['check_name']}: {v['result']}{' (required)' if v['required'] else ''}"
                      f" — evidence: {v.get('evidence') or 'inline'}\n"
                      + "\n".join("    " + l for l in (v.get('detail') or '').splitlines()[-8:]))
    spec_text = spec.strip()
    spec_note = (f"\n(task text truncated at {SPEC_CHARS} of {len(spec_text)} characters)"
                 if len(spec_text) > SPEC_CHARS else "")
    if contract["phase"] == "build":
        default_q = ("Does this commit complete the BUILD phase of the task correctly, within "
                     "scope, with the BUILD evidence it requires? (The deferred CLOSE duties are "
                     "not part of this question.)")
    else:
        default_q = "Does this commit complete the task correctly, within scope?"
    head_part = [
        f"# Review packet — project {project_id}, task {task_id}: {title}",
        f"Base commit: {base}", f"Head commit (review exactly this): {head}",
        "", "## Provenance",
        f"- packet format: v{PACKET_VERSION}",
        f"- reviewed commit: {head}", f"- base: {base}",
        f"- task spec and scope read from: {provenance.get('spec_from') or 'unknown'}",
        f"- budget: {limit} characters (~{est_tokens(limit)} tokens) from {budget['source']}; "
        f"reviewer output up to {budget['max_tokens']} tokens",
        *[f"- note: {n}" for n in provenance.get("notes") or []],
        "", *_phase_section(contract),
        "", "## Task (what was asked)", spec_text[:SPEC_CHARS] + spec_note,
        "", "## Files the task may change", "\n".join(f"- {s}" for s in scope) or "- (none)",
        "", "## Commits", "\n".join(f"- {c[0][:12]} {c[1]}" for c in commits) or "- (none)",
        "", "## Mechanical verification at the head commit (already PASSED; listed for context)",
        "\n".join(vlines) or "- (none)",
    ]
    earlier_part = _earlier_section(earlier or [], head)
    if earlier_part:
        head_part += [""] + earlier_part
    if authors:
        head_part += ["", "## Code authorship",
                      "The primary session integrated proposals from read-only helpers using: "
                      + ", ".join(sorted(authors)) + ". Review the result independently; their "
                      "proposals are not evidence."]
    tail_part = ["", "## Question", question or default_q]

    # -- classify files, reserve coverage space ---------------------------------------
    fl = []
    for f in files:
        p = priority(f)
        fl.append({**f, "priority": p, "class": priority_class(p), "state": "omitted",
                   "bytes": None, "patch": ""})
    code = [f for f in fl if f["priority"] <= CODE_MAX_PRIORITY]
    rest = [f for f in fl if f["priority"] > CODE_MAX_PRIORITY]
    listed = len(code) + min(len(rest), max(0, LISTED_FILES - len(code)))
    coverage_reserve = sum(len(f["path"]) + 110 for f in (code + rest)[:listed]) + \
        len(evidence) * 160 + 2400
    ev_headers = sum(len("\n".join(_evidence_header(i, it))) + 40
                     for i, it in enumerate(evidence, 1))
    fixed = len("\n".join(head_part)) + len("\n".join(tail_part)) + coverage_reserve + \
        ev_headers + 200
    room = limit - fixed
    diff_room = room if budget.get("diff_cap") is None else min(room, int(budget["diff_cap"]))
    overhead = lambda f: len(f["path"]) + 60  # noqa: E731  (### header + fences)

    # -- source/test diffs: whole or not at all ------------------------------------
    code_bytes = 0
    for f in code:
        f["patch"] = patch(f["path"])
        f["bytes"] = _bytes(f["patch"])
        code_bytes += f["bytes"]
    code_need = sum(len(f["patch"]) + overhead(f) for f in code)
    info = {"version": PACKET_VERSION, "phase": contract["phase"], "budget": budget,
            "spec_from": provenance.get("spec_from"), "insufficient": None}
    if code_need > max(diff_room, 0):
        info["insufficient"] = (
            f"insufficiently inspected: source/test diff {code_bytes} bytes exceeds the review "
            f"packet budget {max(diff_room, 0)}; split the review or raise the budget")
        info["coverage"] = {"code_files": len(code), "code_bytes": code_bytes,
                            "budget": max(diff_room, 0)}
        return "", info
    for f in code:
        f["state"] = "included"
    room -= code_need
    diff_room -= code_need

    # -- everything else by priority (evidence before diffs at equal priority) ----------
    queue = [(it["priority"], 0, i, "e") for i, it in enumerate(evidence)] + \
            [(f["priority"], 1, i, "d") for i, f in enumerate(rest)]
    ev_body = {}
    for _, _, i, kind in sorted(queue):
        if kind == "e":
            it = evidence[i]
            text = it["text"]
            if not text:
                it["shown"] = "no content"
                continue
            cap = min(EVIDENCE_ITEM_CHARS, room - 40)
            if cap < MIN_SLICE:
                it["shown"] = "omitted (packet budget)"
                it["omitted"] = True
                continue
            body = _cut_lines(text, cap)
            if len(body) < len(text):
                it["truncated"] = True
                it["shown"] = f"excerpt {len(body)} of {len(text)} chars"
            else:
                it["shown"] = f"{len(text)} chars"
            ev_body[i] = body
            room -= len(body) + 20
        else:
            f = rest[i]
            avail = min(room, diff_room) - overhead(f)
            if avail < MIN_SLICE:
                continue                                  # omitted; patch not even fetched
            p = patch(f["path"])
            f["bytes"] = _bytes(p)
            if len(p) <= avail:
                f["patch"], f["state"] = p, "included"
            else:
                f["patch"], f["state"] = _cut_lines(p, avail), "partial"
            used = len(f["patch"]) + overhead(f)
            room -= used
            diff_room -= used

    lost = [(i, it) for i, it in enumerate(evidence) if it.get("omitted") and it["priority"] == 0]
    if lost:
        i, it = lost[0]
        info["insufficient"] = (
            f"insufficiently inspected: essential evidence E{i + 1} ({it['title'][:80]}) does not "
            f"fit the review packet budget {limit} after the source/test diff ({code_bytes} "
            f"bytes); split the review or raise the budget")
        info["coverage"] = {"code_files": len(code), "code_bytes": code_bytes, "budget": limit,
                            "essential_evidence_omitted": [x["title"] for _, x in lost]}
        return "", info

    # -- render ---------------------------------------------------------------------
    ev_part = ["", "## Project evidence"]
    if not evidence:
        ev_part.append("- (the adapter supplied no evidence items)")
    for i, it in enumerate(evidence):
        ev_part += _evidence_header(i + 1, it)
        if i in ev_body:
            o, c = _fence(ev_body[i], "text")
            ev_part += [o, ev_body[i].rstrip("\n"), c]
            if it["truncated"]:
                ev_part.append(f"(truncated: {it.get('shown')}; the rest was not sent)")
        elif it["status"] == "missing":
            ev_part.append("(MISSING: no content at the reviewed revision)")
        else:
            ev_part.append(f"({it.get('shown') or 'no content'})")

    def size(f):
        sz = f"{f['bytes']} bytes" if f["bytes"] is not None else "patch not fetched"
        return f"+{f.get('added', 0)}/-{f.get('deleted', 0)} lines, {sz}"
    order = code + sorted(rest, key=lambda f: (f["priority"], f["path"]))
    cov = ["", "## Coverage", f"{len(fl)} changed file(s) between {base[:12]} and {head[:12]}, "
           f"sent in priority order:"]
    for f in order[:listed]:
        cov.append(f"- {f['path']} — {f['class']} (p{f['priority']}), {size(f)}: {f['state']}")
    if len(order) > listed:
        more = order[listed:]
        by = {}
        for f in more:
            by[f["class"]] = by.get(f["class"], 0) + 1
        cov.append(f"- ... and {len(more)} more file(s), all omitted: "
                   + ", ".join(f"{n} {k}" for k, n in sorted(by.items())))
    cov.append("Summary:")
    cov.append(f"- all source/test changes included ({len(code)} file(s), {code_bytes} bytes)"
               if code else "- no source/test files changed")
    partial = [f["path"] for f in rest if f["state"] == "partial"]
    omitted = [f["path"] for f in rest if f["state"] == "omitted"]
    if partial or omitted:
        if partial:
            cov.append(f"- partial (cut for the budget): {', '.join(partial[:20])}"
                       + (f" and {len(partial) - 20} more" if len(partial) > 20 else ""))
        if omitted:
            cov.append(f"- omitted (lower priority, budget): {', '.join(omitted[:20])}"
                       + (f" and {len(omitted) - 20} more" if len(omitted) > 20 else ""))
    else:
        cov.append("- every changed file is included in full")
    for st in ("missing", "stale", "unverified"):
        names = [f"E{i + 1}" for i, it in enumerate(evidence) if it["status"] == st]
        if names:
            cov.append(f"- evidence {st}: {', '.join(names)}")
    tr = [f"E{i + 1}" for i, it in enumerate(evidence) if it["truncated"]]
    om = [f"E{i + 1}" for i, it in enumerate(evidence) if it.get("omitted")]
    if tr:
        cov.append(f"- evidence truncated: {', '.join(tr)}")
    if om:
        cov.append(f"- evidence omitted (budget): {', '.join(om)}")
    cov.append("Do not claim you inspected anything listed as partial, omitted, missing, stale "
               "or unverified.")

    diff_part = ["", "## Diff (per file, priority order)"]
    for f in order:
        if f["state"] == "omitted":
            continue
        o, c = _fence(f["patch"], "diff")
        diff_part += [f"### {f['path']} ({f['class']}, {f['state']})", o,
                      f["patch"].rstrip("\n"), c]
        if f["state"] == "partial":
            diff_part.append(f"(partial: {_bytes(f['patch'])} of {f['bytes']} bytes sent)")
    if not any(f["state"] != "omitted" for f in order):
        diff_part.append("- (no file diffs included)" if fl else "- (no changes)")

    text = "\n".join(head_part + ev_part + cov + diff_part + tail_part)
    info["coverage"] = {
        "files": {f["path"]: f["state"] for f in fl}, "code_files": len(code),
        "code_bytes": code_bytes, "partial": partial, "omitted": omitted,
        "evidence": [{"title": it["title"], "status": it["status"], "truncated": it["truncated"],
                      "omitted": bool(it.get("omitted"))} for it in evidence]}
    info["chars"] = len(text)
    info["est_tokens"] = est_tokens(len(text))
    return text, info


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        cands.append(text[i:j + 1])
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


def _strs(v, n=40, ln=600) -> list[str]:
    if not isinstance(v, list):
        raise ReviewInvalid("expected a list of strings")
    return [str(x)[:ln] for x in v[:n]]


def validate(text: str, *, project_id: str, task_id: str, head_sha: str,
             scope: list[str]) -> dict:
    """The normalized review, or ReviewInvalid. Checks: JSON shape, verdict,
    the task id, and that it names the exact head commit (full sha, or a
    prefix of at least 12 characters)."""
    d = _extract_json(text)
    if d is None:
        raise ReviewInvalid("reviewer did not return a JSON object")
    missing = [k for k in SCHEMA["required"] if k not in d]
    if missing:
        raise ReviewInvalid(f"review is missing {', '.join(missing)}")
    if str(d["sprint_id"]).strip() != task_id:
        raise ReviewInvalid(f"review is for {d['sprint_id']!r}, not {task_id!r}")
    if d.get("project_id") and str(d["project_id"]).strip() != project_id:
        raise ReviewInvalid(f"review is for project {d['project_id']!r}, not {project_id!r}")
    sha = str(d["reviewed_commit_sha"]).strip().lower()
    if len(sha) < 12 or not head_sha.lower().startswith(sha):
        raise ReviewInvalid(f"review names commit {sha[:16]!r}, not the head {head_sha[:12]}")
    verdict = str(d["verdict"]).strip().upper()
    if verdict not in VERDICTS:
        raise ReviewInvalid(f"verdict {verdict!r} is not one of {VERDICTS}")
    if not isinstance(d["findings"], list):
        raise ReviewInvalid("findings must be a list")
    findings, notes = [], []
    for f in d["findings"][:60]:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "info").lower()
        sev = sev if sev in SEVERITIES else "info"
        path = str(f.get("file") or "").replace("\\", "/").strip()
        if path and (path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path)):
            notes.append(f"finding path {path[:80]!r} is outside the repository; dropped the path")
            path = ""
        line = f.get("line")
        findings.append({"severity": sev, "file": path,
                         "line": line if isinstance(line, int) else None,
                         "message": str(f.get("message") or "")[:1500]})
    out = {"sprint_id": task_id, "project_id": project_id, "reviewed_commit_sha": head_sha,
           "verdict": verdict, "findings": findings,
           "evidence_inspected": _strs(d["evidence_inspected"]),
           "missing_evidence": _strs(d["missing_evidence"]),
           "required_repairs": _strs(d["required_repairs"]),
           "residual_risks": _strs(d["residual_risks"]), "validator_notes": notes}
    blocking = [f for f in findings if f["severity"] in ("blocker", "major")]
    if verdict == "APPROVED" and (blocking or out["missing_evidence"]):
        out["verdict"] = "CHANGES_REQUIRED"
        notes.append("APPROVED with blocking findings or missing evidence is not an approval")
    return out


def findings_for_repair(review: dict) -> list[str]:
    """Reviewer requests, quoted as advice for the repair session."""
    out = []
    for f in review.get("findings") or []:
        if f["severity"] in ("blocker", "major", "minor"):
            loc = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file", "")
            out.append(f"reviewer ({f['severity']}){' ' + loc if loc else ''}: {f['message']}")
    out += [f"reviewer repair: {r}" for r in review.get("required_repairs") or []]
    out += [f"reviewer: missing evidence: {m}" for m in review.get("missing_evidence") or []]
    return out[:30]


def packet_version(review: dict) -> int:
    """The packet format a stored review was made from (1 before v0.121.8,
    when reviews didn't record it)."""
    try:
        return int(((review or {}).get("result") or {}).get("packet", {}).get("version") or 1)
    except (TypeError, ValueError, AttributeError):
        return 1
