"""Templated CLOSE for AnimalUprising sprints (automation revamp B7, 2026-10-21).

The CLOSE session's duties (adapter._close_duties) are bookkeeping the coordinator can
do from the task record: the sprint SUMMARY, the STATE.md ledger row, the MASTER_ORDER
status line (when the plan lists it), the plan's final CLOSE row, and done_check's output
in the evidence log. Three CLOSE sessions cost ~1.1M tokens in the UI-D1/UI-02a window.

Conservative by design: existing rows are edited, never rewritten; anything this module
cannot place is left alone. tools/guard/done_check.py stays the judge: when it does not
print DONE-CHECK <id> PASS after the documents are committed, the template hands over
to the ordinary CLOSE session (engine._closeout_template -> "none"), which sees the
coordinator's documents and the failure reason.
"""

from __future__ import annotations

import os
import re

STATUS_WORDS = r"NOT STARTED|NOT DONE|TODO|IN PROGRESS|IN-PROGRESS|PLANNED|QUEUED|NEXT|OPEN|ACTIVE"
ROW_CAP = 600


def status_word(spec) -> str:
    return "COMPLETE-PENDING-VISUAL-REVIEW" if (spec.data or {}).get("visual") else "DONE"


def _read(root: str, rel: str):
    try:
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return None


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in (text or "") else "\n"


def summary(spec, ctx: dict, template) -> str:
    t, status = spec.task_id, status_word(spec)
    gate = ctx.get("gate") or {}
    rev = ctx.get("review") or {}
    vis = ctx.get("visual") or {}
    facts = {
        "status": [f"**Status:** {status}"],
        "shipped": [f"- {p}" for p in (ctx.get("changed") or [])[:60]] or ["- (no files)"],
        "evidence": [
            f"- Studio gate: {gate.get('summary') or 'n/a'} at {str(gate.get('head') or '')[:12]}"
            + (f" ({gate.get('verdict')}, {gate.get('checks')} checks, "
               f"{gate.get('captures')} captures)" if gate else ""),
            f"- Code review: {rev.get('verdict') or 'n/a'} ({rev.get('review_id') or '-'}, "
            f"{rev.get('persona') or '-'}) at {str(ctx.get('reviewed_sha') or '')[:12]}",
            f"- Visual approval: " + (f"{vis.get('approver')} at {str(vis.get('revision'))[:12]}"
                                      if vis else "n/a"),
            f"- Commits: {len(ctx.get('commits') or [])} from {str(ctx.get('base') or '')[:12]} "
            f"to {str(ctx.get('head') or '')[:12]}"],
        "followups": [f"- {n}" for n in (ctx.get("vision_notes") or [])[:20]] or ["- none"],
    }
    body = [f"# {t} — {spec.title}", "", *facts["status"], "",
            f"Closed by the coordinator on {ctx.get('date')} from the task record "
            f"(templated CLOSE, no model session).", ""]
    heads = [ln.strip() for ln in (template or "").splitlines() if ln.startswith("## ")]
    want = heads or ["## What shipped", "## Evidence", "## Follow-ups"]
    for h in want:
        low = h.lower()
        body.append(h)
        if any(k in low for k in ("ship", "change", "built", "deliver", "scope", "file")):
            body += facts["shipped"]
        elif any(k in low for k in ("evidence", "verif", "gate", "test", "review", "proof")):
            body += facts["evidence"]
        elif any(k in low for k in ("follow", "risk", "note", "open", "debt", "next")):
            body += facts["followups"]
        elif "status" in low:
            body += facts["status"]
        else:
            body.append("- see the evidence log and the coordinator's task record")
        body.append("")
    return "\n".join(body).rstrip() + "\n"


def state_row(text: str, spec, ctx: dict) -> tuple:
    """(new STATE.md text, note) or (None, why). Edits this sprint's ledger row in
    place, else appends one shaped like the last ledger row (<= 600 characters)."""
    t, status = spec.task_id, status_word(spec)
    eol = _eol(text)
    lines = text.split(eol)
    mine = [i for i, ln in enumerate(lines) if re.match(r"^\|\s*%s\s*\|" % re.escape(t), ln)]
    if mine:
        i = mine[-1]
        cells = lines[i].split("|")
        if len(cells) < 4:
            return None, "the sprint's STATE.md row has no status cell"
        cells[2] = f" {status} "
        lines[i] = "|".join(cells)[:ROW_CAP]
        return eol.join(lines), "edited the sprint's ledger row"
    rows = [i for i, ln in enumerate(lines)
            if re.match(r"^\|\s*[A-Z]{1,6}[0-9A-Za-z.-]*\s*\|", ln) and "---" not in ln]
    if not rows:
        return None, "no ledger table found in STATE.md"
    last = rows[-1]
    ncell = max(3, lines[last].count("|") - 1)
    gate = ctx.get("gate") or {}
    one = (f"{spec.title}: closed by the coordinator; gate "
           f"{'PASS' if gate.get('result') == 'PASS' else 'n/a'} at "
           f"{str(gate.get('head') or ctx.get('head') or '')[:12]}, review "
           f"{(ctx.get('review') or {}).get('verdict') or 'n/a'}")
    cells = [t, status, ctx.get("date") or "", one] + ["-"] * max(0, ncell - 4)
    row = ("| " + " | ".join(str(c).replace("|", "/") for c in cells[:ncell]) + " |")[:ROW_CAP]
    lines.insert(last + 1, row)
    return eol.join(lines), "appended a ledger row"


def master_order(text: str, spec) -> tuple:
    t, status = spec.task_id, status_word(spec)
    eol = _eol(text)
    lines = text.split(eol)
    hit = [i for i, ln in enumerate(lines) if re.search(r"\b%s\b" % re.escape(t), ln)
           and re.search(r"\b(?:%s)\b" % STATUS_WORDS, ln)]
    if len(hit) != 1:
        return None, "no single MASTER_ORDER status line for the sprint"
    i = hit[0]
    lines[i] = re.sub(r"\b(?:%s)\b" % STATUS_WORDS, status, lines[i], count=1)
    return eol.join(lines), "updated the MASTER_ORDER status line"


def close_row(plan: str, ctx: dict) -> tuple:
    from ...bookkeeping import tick_lines
    eol = _eol(plan)
    lines = plan.split(eol)
    rows = [i for i, ln in enumerate(lines)
            if re.match(r"^\s*(?:[-*+]|\d+[.)])\s*\[( |~|-|/)\]", ln)]
    allrows = [i for i, ln in enumerate(lines) if re.match(r"^\s*(?:[-*+]|\d+[.)])\s*\[", ln)]
    if not allrows or not rows or rows[-1] != allrows[-1]:
        return None, "the plan's CLOSE row is missing or already ticked"
    i = rows[-1]
    # tick_lines never ticks the last row; tick it as the only row of a one-row text
    new, ticked = tick_lines(lines[i] + "\n- [ ] (sentinel)", {
        r".": f"closed by the coordinator at {str(ctx.get('head') or '')[:12]}: summary, "
              f"ledger row, done_check in the evidence log"})
    if not ticked:
        return None, "the plan's CLOSE row could not be ticked"
    lines[i] = new.split("\n")[0]
    return eol.join(lines), "ticked the plan's CLOSE row"


def build(adapter, spec, ctx: dict) -> dict:
    """{"files": {path: text}, "notes": [...]} or {"error": why}."""
    root = adapter.p.workspace
    t = spec.task_id
    files, notes = {}, []
    plan_rel = (spec.data or {}).get("plan") or f"docs/progress/{t}_PLAN.md"
    plan = _read(root, plan_rel)
    tpl = _read(root, "docs/progress/_SUMMARY_TEMPLATE.md")
    files[f"docs/progress/{t}_SUMMARY.md"] = summary(spec, ctx, tpl)
    state = _read(root, "docs/progress/STATE.md")
    if state is None:
        return {"error": "docs/progress/STATE.md not found"}
    new, why = state_row(state, spec, ctx)
    if new is None:
        return {"error": why}
    files["docs/progress/STATE.md"] = new
    notes.append(why)
    if plan and "MASTER_ORDER" in plan:
        mo = _read(root, "docs/MASTER_ORDER.md")
        if mo is not None:
            new, why = master_order(mo, spec)
            if new is None:
                return {"error": why}
            files["docs/MASTER_ORDER.md"] = new
            notes.append(why)
    if plan:
        new, why = close_row(plan, ctx)
        if new is not None:
            files[plan_rel] = new
        notes.append(why)
    return {"files": files, "notes": notes}


def finish(adapter, spec, ctx: dict) -> dict:
    """After the documents are committed: done_check must PASS; its output goes into
    the evidence log (the duty the CLOSE session had). {"files": ...} | {"error": why}."""
    t = spec.task_id
    head = ctx.get("head") or ""
    rows = adapter._done_check(spec, "closeout", head)
    final = next((r for r in rows if r.check == "au.done_check"), None)
    if final is None or final.result != "PASS":
        bad = [f"{r.check}: {r.result} {str(r.detail or '')[:200]}" for r in rows
               if r.result != "PASS"][:6]
        return {"error": "done_check did not pass after the templated closeout: "
                         + ("; ".join(bad) or "no verdict")}
    try:
        with open(final.evidence, encoding="utf-8") as f:
            out = f.read()
    except OSError:
        out = final.detail or ""
    rel = f"docs/progress/{t}_EVIDENCE_RAW.log"
    log = _read(adapter.p.workspace, rel) or ""
    eol = _eol(log)
    block = (f"=== DONE-CHECK {t} (coordinator closeout at {head[:12]}) ==={eol}"
             + eol.join(out.rstrip().splitlines()) + eol)
    return {"files": {rel: log + ("" if not log or log.endswith(eol) else eol) + block}}
