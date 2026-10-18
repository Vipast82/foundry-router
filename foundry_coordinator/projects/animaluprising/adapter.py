"""AnimalUprising adapter: the project's own documents and tools, nothing re-implemented.

Task source   docs/progress/<ID>_PLAN.md, written by the Sprint Manager (or
              tools/guard/plan_new.py). Its "Files this sprint may change"
              list is the task's scope and its **Tier:** line the evidence
              tier, both parsed by the project's own tools/guard/done_check.py.
Dependencies  the plan's **Depends on:** line. A dependency finished before the
              coordinator existed counts when its STATE.md ledger row says
              DONE (or COMPLETE-PENDING-VISUAL-REVIEW, which MASTER_ORDER lets
              dependents start on).
Verification  tools/guard/done_check.py (run, rows parsed), the gate verdict in
              docs/progress/<ID>_EVIDENCE_RAW.log bound to the commit the gate
              ran at (R-C live-to-HEAD), the Lune suites + negative control
              re-run by the coordinator itself, and the Kilo session audit
              (kilo_checkpoint.py) as advisory evidence.
Sessions      BUILD session(s) implement + run the gate; a CLOSE session writes
              the summary/STATE rows, runs done_check and the one plain
              `git push origin master` (AGENT_RULES §9, §24, §34 BUILD/CLOSE).
Approval      tier-B / visual sprints stop at COMPLETE-PENDING-VISUAL-REVIEW
              until Victor approves the evidence images (§19, §20, R-A).

The coordinator refuses to start any sprint until AGENT_RULES.md carries the
coordinator amendment (RULE_AMENDMENT.md): the one-sprint-per-session rule is
never overridden silently.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time

from ...adapters.base import (FAIL, PASS, UNKNOWN, EVIDENCE_ITEM_CHARS, TaskSpec,
                              VerificationResult, bound_text, evidence_dir, evidence_item,
                              is_binary_path, report_instructions)
from ...adapters.project import AdapterError, GenericAdapter
from ...profile import matches_any

AMENDMENT_MARKER = "COORDINATOR-ADVANCEMENT"
VERIFY_ROWS = ("COMMITS", "CLEAN", "SCOPE", "TEST", "GATE", "BOOT", "EVIDENCE", "TIER-C")
CLOSEOUT_ROWS = VERIFY_ROWS + ("PLAN", "SUMMARY", "PATHS", "API", "STATE")
_ROW = re.compile(r"^(PASS|FAIL|NOTE)\s+(\S+)\s+(.*)$")


class AnimalUprisingAdapter(GenericAdapter):
    def __init__(self, profile, state_dir):
        super().__init__(profile, state_dir)
        self.cfg = dict(profile.raw.get("animaluprising") or {})
        self._dc = None

    # -- project tools ---------------------------------------------------------------

    def _done_check_module(self):
        """tools/guard/done_check.py, imported from the workspace (its plan
        parsing is the project's definition of scope and tier)."""
        if self._dc is None:
            path = os.path.join(self.p.workspace, "tools", "guard", "done_check.py")
            if not os.path.isfile(path):
                raise AdapterError("tools/guard/done_check.py not found in the workspace")
            guard = os.path.dirname(path)
            if guard not in sys.path:
                sys.path.insert(0, guard)
            spec = importlib.util.spec_from_file_location("au_done_check", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._dc = mod
        return self._dc

    def _python(self) -> list[str]:
        return list(self.cfg.get("python") or [sys.executable])

    def _read(self, rel: str) -> str:
        with open(os.path.join(self.p.workspace, rel), encoding="utf-8", errors="replace") as f:
            return f.read()

    # -- tasks ---------------------------------------------------------------------

    def list_tasks(self) -> list[str]:
        d = os.path.join(self.p.workspace, "docs", "progress")
        return sorted(n[:-len("_PLAN.md")] for n in os.listdir(d) if n.endswith("_PLAN.md"))

    def describe(self, task_id: str) -> TaskSpec:
        rel = _plan_rel(task_id)
        if not os.path.isfile(os.path.join(self.p.workspace, rel)):
            raise AdapterError(f"{rel} does not exist; the Sprint Manager writes the plan first "
                               f"(or: py -3 tools\\guard\\plan_new.py {task_id})")
        spec = self._parse_plan(task_id, rel, self._read(rel))
        spec.data["spec_source"] = "working tree"
        return spec

    def describe_at(self, task_id: str, revision: str, ws) -> TaskSpec:
        """The sprint as the reviewed commit's plan defines it, never the working
        tree: docs/progress/<ID>_PLAN.md read with `git show <revision>:<plan>`
        and parsed by the same parser as describe()."""
        rel = _plan_rel(task_id)
        plan = ws.show_at(revision, rel)
        if plan is None:
            raise AdapterError(f"{rel} does not exist at {revision[:12]}; the review needs the "
                               f"plan as committed")
        spec = self._parse_plan(task_id, rel, plan)
        spec.data.update(spec_source="revision", spec_revision=revision,
                         plan_blob=ws.blob_at(revision, rel))
        return spec

    def _parse_plan(self, task_id: str, rel: str, plan: str) -> TaskSpec:
        """The plan text -> TaskSpec (done_check.py's own scope/tier parsing)."""
        dc = self._done_check_module()
        scope = dc.plan_files(plan)
        tier = dc.plan_tier(plan) or "A"
        tags = [f"tier:{tier}"]
        # Visual = the sprint delivers images for Victor's look (§19/§20, R-A): evidence
        # captures in its file list, or its acceptance lines end at visual review.
        # Tier B alone isn't visual (HK-G2 was tier B and closed DONE).
        visual = (any(s.startswith(f"docs/evidence/{task_id}/") and
                      s.lower().endswith((".png", ".jpg")) for s in scope)
                  or "COMPLETE-PENDING-VISUAL-REVIEW" in _section(plan, "Acceptance lines"))
        if visual:
            tags.append("visual")
        if re.search(r"(?i)\brisk:\s*high\b|\bTier:\*\*\s*A\b.*server", plan):
            tags.append("risk:high")
        deps = []
        m = re.search(r"\*\*Depends on:\*\*\s*([^\n]+)", plan)
        if m:
            deps = [d for d in re.findall(r"\b([A-Z]{1,5}[0-9]?-?[A-Za-z0-9.-]*[0-9][A-Za-z0-9]*)\b",
                                          m.group(1).split("**")[0]) if d != task_id]
        goal = _section(plan, "Goal")
        acceptance = _section(plan, "Acceptance lines")
        nxt = [l for l in plan.splitlines() if l.startswith("NEXT:")]
        checklist = "\n".join(l for l in plan.splitlines() if re.match(r"^\s*-\s\[[ x]\]", l))
        spec = "\n".join(x for x in [
            f"Plan: {rel} (read it first; it is the sprint's memory, AGENT_RULES §34).",
            f"Tier: {tier}." + (" Visual sprint: status is at most COMPLETE-PENDING-VISUAL-REVIEW "
                                "until Victor approves the evidence images." if visual else ""),
            "## Goal", goal.strip(), "## Acceptance lines", acceptance.strip(),
            "## Checklist", checklist, ("## Last NEXT line\n" + nxt[-1]) if nxt else ""] if x)
        return TaskSpec(project_id=self.p.id, task_id=task_id, title=_title(plan, task_id),
                        spec=spec, scope=scope, tags=tags, deps=deps,
                        data={"tier": tier, "visual": visual, "plan": rel,
                              "close_step": _close_step(plan)})

    def preflight_task(self, spec: TaskSpec):
        if self.cfg.get("require_rule_amendment", True):
            try:
                rules = self._read("AGENT_RULES.md")
            except OSError:
                return "AGENT_RULES.md not found"
            if AMENDMENT_MARKER not in rules:
                return ("AGENT_RULES.md has no coordinator amendment (marker "
                        f"{AMENDMENT_MARKER}); the one-sprint-per-session rule is not overridden "
                        "silently. Apply RULE_AMENDMENT.md through an HK sprint first.")
        if not spec.scope:
            return f"{spec.task_id}_PLAN.md has no 'Files this sprint may change' list"
        return None

    def dependency_done(self, dep: str) -> bool:
        """Finished before the coordinator: the STATE.md ledger row says so."""
        try:
            state = self._read("docs/progress/STATE.md")
        except OSError:
            return False
        accept_pending = "visual" in self.p.deps_accept_pending
        for line in state.splitlines():
            if re.match(r"^\|\s*%s\s*\|" % re.escape(dep), line):
                if "NOT DONE" in line:
                    return False
                if re.search(r"\bDONE\b", line) or (accept_pending and
                                                    "COMPLETE-PENDING-VISUAL-REVIEW" in line):
                    return True
        return False

    # -- prompts ---------------------------------------------------------------------

    def rules_text(self, spec: TaskSpec) -> str:
        return ("Project rules: AGENT_RULES.md (full) and .clinerules / .kilo rules (core) apply "
                "in full, including the coordinator amendment (marker "
                f"{AMENDMENT_MARKER}). You run exactly one sprint in this session: {spec.task_id}.")

    def implement_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        planning = ""
        if self.p.roles.get("planner"):
            planning = """
Before executing this attempt, invoke the read-only `au-planner` subagent once through the task tool. Give it this sprint's approved plan, current revision, latest NEXT/checklist, relevant observed results, and any repair findings below. It uses the configured Foundry planner persona to identify the next bounded steps and resolve routine procedure choices already covered by the plan. Keep this delegation bounded; do not re-plan every file or run duplicate planning calls.
The planner returns advice, not acceptance evidence or permission to widen scope. Integrate its result and continue the approved BUILD work in this Code session; do not stop merely to ask Victor to copy a planner response into another chat or switch modes. The primary alone writes and controls Studio. Stop for Victor only when the project actually requires a human decision, visual approval, credentials/OS interaction, or an unresolved blocker after the configured attempts. Never waive a failed/unknown gate or weaken a test to avoid a stop.
"""
        return f"""BUILD session for sprint {spec.task_id} — {spec.title} (coordinator run {ctx.get('run_id')}, attempt {ctx.get('attempt')}).

{self.rules_text(spec)}
Read docs/progress/STATE.md header + last 5 ledger rows, then docs/progress/{spec.task_id}_PLAN.md, and resume from its checklist and NEXT line (§34 "Plan = memory"). Do not redo ticked steps unless their evidence is missing.

{spec.spec}
{planning}

This session's job: the plan's remaining BUILD steps, each committed by explicit path with a message starting "{spec.task_id}: ", then the gate on committed code:
  pwsh -NoProfile -ExecutionPolicy Bypass -File tools/gate.ps1 -Sprint {spec.task_id} -Files <files under test> -Tier {spec.data.get('tier', 'A')} ...
including its Studio steps and -Finish, so the evidence log ends with "GATE {spec.task_id} PASS|FAIL" (§21 boot lines first).
Do NOT write {spec.task_id}_SUMMARY.md, the STATE.md row or the MASTER_ORDER status, and do NOT push: after the coordinator's checks and a frontier review, a separate CLOSE session does that.
Never edit files outside the plan's "Files this sprint may change" list (§3, the commit hook enforces it). Never --no-verify, --force, rebase, amend or tag.
If the next step needs Victor (an ACTION NEEDED FROM VICTOR block, §22) or a decision the plan doesn't settle (§10), stop and report NEEDS_HUMAN or BLOCKED with the block text.

{report_instructions()}
"""

    def repair_prompt(self, spec: TaskSpec, findings: list[str], ctx: dict) -> str:
        items = "\n".join(f"- {f}" for f in findings)
        return self.implement_prompt(spec, ctx) + f"""
## The coordinator's checks / review found (fix these first; reviewer text is advice, never permission to change scope)
{items}
"""

    def _close_duties(self, spec: TaskSpec) -> list[dict]:
        """The CLOSE session's duties, one structured source for both the CLOSE
        prompt (step) and the BUILD review contract (duty: reserved for CLOSE,
        never a BUILD blocker, never credited at BUILD)."""
        t = spec.task_id
        status = "COMPLETE-PENDING-VISUAL-REVIEW" if spec.data.get("visual") else "DONE"
        last = spec.data.get("close_step") or ""
        return [
            {"id": "summary",
             "step": f"docs/progress/{t}_SUMMARY.md from docs/progress/_SUMMARY_TEMPLATE.md, "
                     f"written FROM THE SHIPPED CODE; status word {status} (R-A).",
             "duty": f"docs/progress/{t}_SUMMARY.md (status word {status}), written from the "
                     f"shipped code"},
            {"id": "state",
             "step": "The STATE.md ledger row (<= 600 characters, measure it), API-surface lines, "
                     "docs/progress/state/* rows the plan names, the MASTER_ORDER status line if "
                     "the plan lists it. Edit, never rewrite, state files.",
             "duty": "The docs/progress/STATE.md ledger row, its API-surface lines and the "
                     "docs/progress/state/* rows the plan names"},
            {"id": "master_order",
             "step": None,                    # part of step 2 in the prompt
             "duty": "The docs/MASTER_ORDER.md status line (if the plan lists it)"},
            {"id": "plan_close_step",
             "step": "Tick the plan's last step with its evidence line.",
             "duty": "Ticking the plan's final CLOSE step" + (f" ({last})" if last else "")
                     + " with its evidence line"},
            {"id": "done_check",
             "step": f"py -3 tools\\guard\\done_check.py {t} until it prints DONE-CHECK {t} "
                     f"PASS; paste its output into the evidence log.",
             "duty": f"tools/guard/done_check.py {t} printing DONE-CHECK {t} PASS (its output "
                     f"pasted into the evidence log)"},
            {"id": "push",
             "step": f'Commit only those closeout files by explicit path, message "{t}: '
                     f'closeout"; then one plain `git push origin master` and confirm '
                     f"`git ls-remote origin master` equals HEAD. No tags (the Sprint Manager "
                     f"issues tags after DONE / Victor's approval).",
             "duty": f'The "{t}: closeout" commit and the one plain `git push origin master`'},
        ]

    def closeout_duties(self, spec: TaskSpec) -> list[str]:
        return [d["duty"] for d in self._close_duties(spec)]

    def review_contract(self, spec: TaskSpec) -> dict:
        """BUILD review (AGENT_RULES §34 BUILD/CLOSE): the CLOSE session runs only
        after this review approves, so its duties are deferred, not missing."""
        t = spec.task_id
        last = spec.data.get("close_step") or ""
        reviewed = [
            f"The BUILD implementation: the code and test changes of {t} up to the reviewed commit",
            "The completed BUILD checklist items in the plan as committed (ticked lines must be "
            "backed by evidence; the final CLOSE step" + (f" ({last})" if last else "")
            + " stays unticked at BUILD)",
            f"Revision-bound tests and gate: GATE {t} PASS in docs/progress/{t}_EVIDENCE_RAW.log "
            f"at the reviewed commit, with proper suite verdicts/tallies",
            "Scope: only files in the plan's 'Files this sprint may change' list changed",
        ]
        if spec.data.get("visual") or any(s.startswith(f"docs/evidence/{t}/") for s in spec.scope):
            reviewed.append(f"The required evidence captures under docs/evidence/{t}/ at the "
                            f"reviewed commit (visual approval itself is Victor's, later)")
        return {"phase": "build", "reviewed": reviewed, "deferred": self.closeout_duties(spec)}

    def diff_priority(self, path: str, spec: TaskSpec) -> int:
        """0 src, 1 tests, 2 the plan / tools / config in scope, 5 other docs,
        8 evidence logs and docs/evidence, 9 binary."""
        p = (path or "").replace("\\", "/")
        if is_binary_path(p):
            return 9
        if (p.startswith("docs/progress/") and p.endswith(".log")) or p.startswith("docs/evidence/"):
            return 8
        if p.startswith("src/"):
            return 0
        if p.startswith("tests/"):
            return 1
        if p == spec.data.get("plan", f"docs/progress/{spec.task_id}_PLAN.md"):
            return 2
        if not p.startswith("docs/") and (p in spec.scope or p.startswith("tools/")):
            return 2 if p in spec.scope else 3
        if p.startswith("docs/") or p.endswith(".md"):
            return 5
        return 4

    def closeout_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        retry =(f"\nAn earlier CLOSE attempt was incomplete; the coordinator saw: "
                 f"{ctx['retry_reason']}\n") if ctx.get("retry_reason") else ""
        steps = "\n".join(f"{i}. {step}" for i, step in
                          enumerate((d["step"] for d in self._close_duties(spec) if d["step"]), 1))
        return f"""CLOSE session for sprint {spec.task_id} — {spec.title}.
{self.rules_text(spec)}
The code is committed, the gate passed and a frontier review approved commit {ctx.get('base', '')[:12]}. Do only closeout (§9, §31, §34 CLOSE):
{steps}
Change no code or test file in this session; if one needs changing, stop and report BLOCKED.{retry}

{report_instructions()}
"""

    # -- verification ------------------------------------------------------------------

    def verify(self, spec: TaskSpec, phase: str, revision: str, base: str) -> list:
        out = self._done_check(spec, phase, revision)
        if phase == "verify" and spec.data.get("tier") in ("A", "B"):
            # tier C has no gate (§30): its proof is the empty src/tests diff in done_check
            out.append(self._gate_binding(spec, revision))
            out += self._lune(spec, revision)
        out += super().verify(spec, phase, revision, base)       # extra profile checks
        return out

    def _done_check(self, spec: TaskSpec, phase: str, revision: str) -> list:
        cmd = self._python() + [os.path.join("tools", "guard", "done_check.py"), spec.task_id]
        t0 = time.time()
        log = os.path.join(evidence_dir(self.state_dir, self.p.id, spec.task_id, revision),
                           f"done_check-{phase}.log")
        try:
            r = subprocess.run(cmd, cwd=self.p.workspace, capture_output=True, text=True,
                               timeout=600, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            return [VerificationResult("au.done_check", UNKNOWN, True, "", str(e), revision)]
        text = r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
        with open(log, "w", encoding="utf-8") as f:
            f.write(f"$ {' '.join(cmd)}\n{text}\n# exit {r.returncode}\n")
        wanted = VERIFY_ROWS if phase == "verify" else CLOSEOUT_ROWS
        res, seen = [], set()
        for line in text.splitlines():
            m = _ROW.match(line.strip())
            if not m or m.group(1) == "NOTE":
                continue
            name = m.group(2)
            if name not in wanted:
                continue
            key = f"au.done_check.{name}"
            # several rows can share a name (e.g. TEST); any FAIL fails it
            prev = next((x for x in res if x.check == key), None)
            if prev:
                if m.group(1) == "FAIL":
                    prev.result = FAIL
                prev.detail += "\n" + m.group(3)
                continue
            seen.add(name)
            res.append(VerificationResult(key, m.group(1), True, log, m.group(3), revision,
                                          duration_ms=int((time.time() - t0) * 1000)))
        if phase == "closeout":
            final = re.search(r"^DONE-CHECK %s (PASS|FAIL)" % re.escape(spec.task_id), text, re.M)
            res.append(VerificationResult("au.done_check", final.group(1) if final else UNKNOWN,
                                          True, log, (final.group(0) if final else
                                                      "no DONE-CHECK verdict line"), revision))
        if not res:
            res.append(VerificationResult("au.done_check", UNKNOWN, True, log,
                                          "done_check printed no rows: " + text[-300:], revision))
        return res

    def _gate_binding(self, spec: TaskSpec, revision: str) -> VerificationResult:
        """The last gate verdict must be PASS and the gate must have run at the
        reviewed code: only closeout-exempt files (the evidence log itself,
        studio lines) may change between the gate's HEAD and this revision."""
        rel = f"docs/progress/{spec.task_id}_EVIDENCE_RAW.log"
        try:
            ev = self._read(rel)
        except OSError:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, "", f"{rel} missing",
                                      revision)
        last, heads = _gate_verdict_and_heads(ev, spec.task_id)
        if last is None:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      f"no 'GATE {spec.task_id} PASS|FAIL' line", revision)
        if last.group(1) != "PASS":
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      f"last gate verdict is {last.group(0)}", revision)
        if not heads:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      "no 'PASS: GATE HEAD <sha>' before the verdict", revision)
        gate_head = heads[-1]
        try:
            full = subprocess.run(["git", "-C", self.p.workspace, "rev-parse", gate_head],
                                  capture_output=True, text=True).stdout.strip()
            changed = subprocess.run(["git", "-C", self.p.workspace, "diff", "--name-only",
                                      f"{full}..{revision}"], capture_output=True,
                                     text=True).stdout.split()
        except OSError as e:
            return VerificationResult("au.gate_bound_to_head", UNKNOWN, True, rel, str(e), revision)
        if not full:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      f"gate HEAD {gate_head} is not a commit here", revision)
        exempt = self.exempt_paths(spec) + [f"docs/progress/{spec.task_id}_*"]
        code = [c for c in changed if not matches_any(c, exempt)]
        if code:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      f"the gate ran at {gate_head}, but these files changed "
                                      f"since: {', '.join(code[:8])} — re-run the gate on the "
                                      f"committed code (R-C)", revision)
        return VerificationResult("au.gate_bound_to_head", PASS, True, rel,
                                  f"GATE {spec.task_id} PASS at {gate_head}; only evidence files "
                                  f"changed since ({len(changed)})", revision)

    def _lune(self, spec: TaskSpec, revision: str) -> list:
        lune = self.cfg.get("lune") or shutil.which("lune")
        if not lune:
            return [VerificationResult("au.lune", UNKNOWN, True, "", "lune not on PATH (rokit "
                                       "install)", revision)]
        out = []
        for name, args, pat in (("au.lune", [], r"^LUNE \d+/\d+ PASS"),
                                ("au.lune_negative", ["--", "--negative"],
                                 r"^NEGATIVE CONTROL PASS")):
            log = os.path.join(evidence_dir(self.state_dir, self.p.id, spec.task_id, revision),
                               name + ".log")
            try:
                r = subprocess.run([lune, "run", "tests/lune/run.luau", *args],
                                   cwd=self.p.workspace, capture_output=True, text=True,
                                   timeout=600, encoding="utf-8", errors="replace")
                text = r.stdout + r.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                out.append(VerificationResult(name, UNKNOWN, True, "", str(e), revision))
                continue
            with open(log, "w", encoding="utf-8") as f:
                f.write(text)
            ok = re.search(pat, text, re.M)
            out.append(VerificationResult(name, PASS if ok else FAIL, True, log,
                                          (ok.group(0) if ok else text[-500:]), revision))
        return out

    def session_audit(self, spec: TaskSpec, session_id: str, transcript: list,
                      revision: str) -> list:
        """checkpoint.py's own rules over the Kilo session (advisory: a STOP is
        shown to the reviewer and in the findings, the code checks decide)."""
        if not transcript:
            return []
        from .kilo_checkpoint import audit
        try:
            text, level = audit(self.p.workspace, session_id, transcript)
        except Exception as e:                                    # noqa: BLE001
            return [VerificationResult("au.kilo_checkpoint", UNKNOWN, False, "",
                                       f"checkpoint adapter failed: {e}", revision)]
        log = os.path.join(evidence_dir(self.state_dir, self.p.id, spec.task_id, revision),
                           f"kilo_checkpoint-{session_id}.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(text)
        verdict = text.strip().splitlines()[-1] if text.strip() else "VERDICT UNKNOWN"
        return [VerificationResult("au.kilo_checkpoint", FAIL if level >= 2 else PASS, False, log,
                                   verdict + "\n" + "\n".join(text.splitlines()[1:8]), revision)]

    def closeout_mode(self) -> str:
        return "session"

    def review_extra(self, spec: TaskSpec, revision: str) -> str:
        parts = []
        rel = f"docs/progress/{spec.task_id}_EVIDENCE_RAW.log"
        try:
            ev = self._read(rel)
            i = ev.rfind("=== GATE FINISH ===")
            parts.append(f"### Last gate summary ({rel})\n```\n{ev[i:i + 6000] if i >= 0 else ev[-4000:]}\n```")
        except OSError:
            parts.append(f"{rel}: missing")
        return "\n\n".join(parts)

    # -- review evidence bound to the reviewed commit -----------------------------------

    def review_evidence(self, spec: TaskSpec, revision: str, ws) -> list[dict]:
        """Evidence for the BUILD review, read ONLY from `revision` (git show /
        ls-tree), never the working tree: the last gate block and raw suite
        verdict/tally lines of the evidence log, the captures the plan requires
        vs. those committed, and the plan's BUILD checklist. Raw lines are quoted
        with their line numbers; nothing is summarised into a verdict. Missing
        files are explicit items, stale gate bindings are marked stale, and cut
        text is flagged truncated."""
        t = spec.task_id
        cap = int(self.cfg.get("review_evidence_item_chars") or EVIDENCE_ITEM_CHARS)
        items: list[dict] = []
        plan_rel = spec.data.get("plan") or f"docs/progress/{t}_PLAN.md"
        plan = ws.show_at(revision, plan_rel)
        plan_blob = ws.blob_at(revision, plan_rel) if plan is not None else ""
        items += self._log_evidence(spec, revision, ws, cap)
        items += self._capture_evidence(spec, revision, ws, plan, cap)
        if plan is None:
            items.append(evidence_item(f"{t} plan BUILD checklist", path=plan_rel,
                                       revision=revision, status="missing", priority=0,
                                       note=f"{plan_rel} does not exist at {revision[:12]}"))
        else:
            items.append(self._checklist_evidence(spec, revision, plan_rel, plan_blob, plan, cap))
        return items

    def _log_evidence(self, spec: TaskSpec, revision: str, ws, cap: int) -> list[dict]:
        t = spec.task_id
        rel = f"docs/progress/{t}_EVIDENCE_RAW.log"
        ev = ws.show_at(revision, rel)
        if ev is None:
            note = f"{rel} does not exist at {revision[:12]}: no gate or suite evidence"
            return [evidence_item(f"{t} last gate block", path=rel, revision=revision,
                                  status="missing", priority=0, note=note),
                    evidence_item(f"{t} suite verdict lines", path=rel, revision=revision,
                                  status="missing", priority=1, note=note)]
        blob = ws.blob_at(revision, rel) or ""
        lines = _lines(ev)
        head = f"# {rel}@{revision} blob {blob} (raw lines, 1-based line numbers)"
        out = []

        # a. the last === GATE FINISH === block
        start = max((i for i, l in enumerate(lines) if "=== GATE FINISH ===" in l), default=-1)
        if start < 0:
            body, cut, _ = bound_text(_numbered(lines, range(max(0, len(lines) - 40), len(lines))),
                                      cap, keep="tail")
            out.append(evidence_item(f"{t} last gate block", path=rel, revision=revision, blob=blob,
                                     lines=f"{max(1, len(lines) - 39)}-{len(lines)}",
                                     text=head + "\n" + body, status="missing", truncated=cut,
                                     priority=0, note="no '=== GATE FINISH ===' marker in the log "
                                     "at this revision; the last 40 raw lines are shown instead"))
        else:
            verdict_re = re.compile(r"^GATE %s (PASS|FAIL)\b" % re.escape(t))
            end = min(len(lines) - 1, start + 300)
            for j in range(start + 1, end + 1):
                if verdict_re.match(lines[j].strip()):
                    end = j
                    break
                if lines[j].startswith("=== GATE") or _SESSION.match(lines[j]):
                    end = j - 1
                    break
            body, cut, omitted = bound_text(_numbered(lines, range(start, end + 1)), cap)
            status, notes = self._gate_staleness(spec, ev, lines[start:end + 1], revision, ws)
            if not verdict_re.match(lines[end].strip()):
                notes.append(f"this block has no 'GATE {t} PASS|FAIL' line")
                if status == "ok":
                    status = "unverified"
            if cut:
                notes.append(f"{omitted} lines of the block cut")
            out.append(evidence_item(f"{t} last gate block", path=rel, revision=revision, blob=blob,
                                     lines=f"{start + 1}-{end + 1}", text=head + "\n" + body,
                                     status=status, truncated=cut, priority=0,
                                     note="; ".join(notes)))

        # b. suite verdict / tally material, quoted raw
        hits = [i for i, l in enumerate(lines) if _suite_line(l, t)]
        max_lines = int(self.cfg.get("review_suite_lines") or 400)
        kept = hits[-max_lines:]
        body, cut, omitted = bound_text(_numbered(lines, kept), cap, keep="tail")
        cut = cut or len(kept) < len(hits)
        shown = len(kept) - omitted
        note = (f"{len(hits)} SESSION / suite verdict / tally lines of {len(lines)} quoted raw; "
                "no verdict is inferred from individual PASS lines")
        if cut:
            note += f"; only the last {shown} matching lines are shown"
        out.append(evidence_item(f"{t} suite verdict lines", path=rel, revision=revision,
                                 blob=blob,
                                 lines=f"{kept[-shown] + 1}-{kept[-1] + 1}" if shown else "",
                                 text=head + "\n" + body if shown else head,
                                 status="ok" if hits else "missing", truncated=cut, priority=1,
                                 note=note if hits else "no SESSION, suite verdict or tally "
                                 "lines in the log at this revision"))
        return out

    def _gate_staleness(self, spec: TaskSpec, ev: str, block: list[str], revision: str,
                        ws) -> tuple[str, list[str]]:
        """Is the gate bound to the reviewed commit? The same rule as
        _gate_binding (only closeout-exempt files may change since the gate's
        HEAD), plus: every revision the gate names (GATE HEAD, a source /
        baseline sha in the block) must be the reviewed commit or its ancestor."""
        t = spec.task_id
        last, heads = _gate_verdict_and_heads(ev, t)
        notes = [f"last verdict line: {last.group(0)}" if last else
                 f"no 'GATE {t} PASS|FAIL' verdict line in the log"]
        named = ([("GATE HEAD", heads[-1])] if heads else [])
        for line in block:
            for m in _BASELINE.finditer(line):
                if all(m.group(1) != s for _, s in named):
                    named.append((m.group(0).split(m.group(1))[0].strip(" :=") or "baseline",
                                  m.group(1)))
        if not heads:
            notes.append("no 'PASS: GATE HEAD <sha>' before the verdict: the gate is not bound "
                         "to a revision")
        status = "ok" if heads else "unverified"
        exempt = self.exempt_paths(spec) + [f"docs/progress/{t}_*"]
        for label, sha in named:
            full = ws.run("rev-parse", "--verify", "--quiet", sha + "^{commit}",
                          check=False).strip()
            if not full:
                status = "stale"
                notes.append(f"{label} {sha} is not a commit in this repository")
                continue
            if full != revision and not ws.is_ancestor(full, revision):
                status = "stale"
                notes.append(f"{label} {sha} is not the reviewed commit {revision[:12]} or an "
                             f"ancestor of it")
                continue
            code = [c for c in ws.changed_files(full, revision) if not matches_any(c, exempt)]
            if code:
                status = "stale"
                notes.append(f"{label} {sha}: these files changed since: "
                             f"{', '.join(code[:8])}" + (" ..." if len(code) > 8 else ""))
            else:
                notes.append(f"{label} {sha} is bound to {revision[:12]} (only closeout-exempt "
                             f"files changed since)")
        return status, notes

    def _capture_evidence(self, spec: TaskSpec, revision: str, ws, plan, cap: int) -> list[dict]:
        t = spec.task_id
        d = f"docs/evidence/{t}"
        present = dict(ws.ls_tree_at(revision, d))
        required = [s for s in spec.scope if s.startswith(d + "/") and not _is_glob(s)
                    and s.lower().endswith((".png", ".jpg", ".jpeg"))]
        globs = [s for s in spec.scope if s.startswith(d + "/") and _is_glob(s)]
        if plan:
            for m in re.finditer(r"docs/evidence/%s/[\w./-]+\.(?:png|jpe?g)" % re.escape(t),
                                 _section(plan, "Acceptance lines"), re.I):
                if m.group(0) not in required:
                    required.append(m.group(0))
        missing = [r for r in required if r not in present]
        listing = "\n".join(f"{p}  blob {b}" + ("  (required)" if p in required else "")
                            for p, b in sorted(present.items()))
        body, cut, omitted = bound_text(f"# {d}/ @ {revision}\n" + (listing or "(no files)"), cap)
        notes = [f"{len(present)} files committed, {len(required)} required by the plan's file "
                 f"list / acceptance lines, {len(missing)} missing"]
        if globs:
            notes.append("scope globs (not checked file by file): " + ", ".join(globs))
        if cut:
            notes.append(f"{omitted} listing lines cut")
        status = "ok"
        if not present and (required or spec.data.get("visual")):
            status = "missing"
        out = [evidence_item(f"{t} evidence captures", path=d + "/", revision=revision,
                             text=body, status=status, truncated=cut, priority=2,
                             note="; ".join(notes))]
        for r in missing:
            out.append(evidence_item(f"Required capture {r}", path=r, revision=revision,
                                     status="missing", priority=0,
                                     note=f"required by the plan; not in the tree at "
                                          f"{revision[:12]}"))
        return out

    def _checklist_evidence(self, spec: TaskSpec, revision: str, rel: str, blob: str,
                            plan: str, cap: int) -> dict:
        main = _checklist_lines(plan)
        section = bool(main)
        if not main:
            main = [(i, l) for i, l in enumerate(_lines(plan)) if _TICK.match(l)]
        ticked = sum(1 for _, l in main if re.match(r"^\s*-\s\[x\]", l, re.I))
        body, cut, omitted = bound_text(
            f"# {rel}@{revision} blob {blob}\n" + "\n".join(f"{i + 1}: {l}" for i, l in main), cap)
        notes = [f"{ticked} of {len(main)} items ticked"
                 + ("" if section else " (no '## Checklist' section; all checkbox lines shown)")]
        if main:
            notes.append(f"the final item (line {main[-1][0] + 1}) is the CLOSE step, reserved for "
                         "the CLOSE session; it is not expected ticked at BUILD")
        if cut:
            notes.append(f"{omitted} lines cut")
        return evidence_item(f"{spec.task_id} plan BUILD checklist", path=rel, revision=revision,
                             blob=blob,
                             lines=f"{main[0][0] + 1}-{main[-1][0] + 1}" if main else "",
                             text=body, status="ok" if main else "missing", truncated=cut,
                             priority=1, note="; ".join(notes) if main else
                             f"no checklist lines in {rel} at {revision[:12]}")

    def session_permissions(self, spec: TaskSpec, phase: str) -> list[dict]:
        rules = super().session_permissions(spec, phase)
        # never: credentials folder, force pushes, hooks bypass (also blocked by the hooks)
        rules += [{"permission": "read", "pattern": "C:/Roblox/_secrets/*", "action": "deny"},
                  {"permission": "bash", "pattern": "git push --force*", "action": "deny"},
                  {"permission": "bash", "pattern": "git push -f*", "action": "deny"},
                  {"permission": "bash", "pattern": "git commit --no-verify*", "action": "deny"},
                  {"permission": "bash", "pattern": "git push --tags*", "action": "deny"},
                  {"permission": "bash", "pattern": "git tag*", "action": "deny"}]
        if phase != "closeout":
            rules.append({"permission": "bash", "pattern": "git push*", "action": "deny"})
        return rules

    def doctor(self) -> list:
        out = []
        try:
            rules = self._read("AGENT_RULES.md")
            out.append((PASS if AMENDMENT_MARKER in rules else "WARN", "rule amendment",
                        "present" if AMENDMENT_MARKER in rules else
                        f"missing ({AMENDMENT_MARKER}); sprints won't start until it is applied"))
        except OSError as e:
            out.append((FAIL, "AGENT_RULES.md", str(e)))
        try:
            self._done_check_module()
            out.append((PASS, "done_check.py", "importable (plan parsing)"))
        except Exception as e:                                    # noqa: BLE001
            out.append((FAIL, "done_check.py", str(e)))
        for rel in ("kilo.jsonc", ".kilo/rules/animaluprising-adapter.md", "tools/gate.ps1",
                    "tests/GateRunner.lua", "tests/lune/run.luau"):
            ok = os.path.exists(os.path.join(self.p.workspace, rel))
            out.append((PASS if ok else ("WARN" if rel.startswith(("kilo", ".kilo")) else FAIL),
                        rel, "present" if ok else "missing"))
        hooks = subprocess.run(["git", "-C", self.p.workspace, "config", "core.hooksPath"],
                               capture_output=True, text=True).stdout.strip()
        out.append((PASS if hooks == "tools/hooks" else "WARN", "commit hooks",
                    f"core.hooksPath = {hooks or '(unset)'}"))
        out.append((PASS if shutil.which("lune") else "WARN", "lune",
                    shutil.which("lune") or "not on PATH (Lune checks will be UNKNOWN)"))
        return out


_SPRINT_ID = re.compile(r"^[A-Z][A-Za-z0-9.-]{1,30}$")
_SESSION = re.compile(r"^\W*SESSION\s+\d+")
_TICK = re.compile(r"^\s*-\s\[[ xX]\]")
_SUITE = re.compile(r"\bS\d{1,3}[a-z]?\b.*\b(?:PASS|FAIL|PASSED|FAILED)\b"
                    r"|\b(?:PASS|FAIL)\b.*\bS\d{1,3}[a-z]?\b")
_TALLY = re.compile(r"(?i)\b\d+\s+(?:passed|failed|errors?)\b|\b\d+\s*/\s*\d+\s+(?:PASS|FAIL)"
                    r"|\b(?:passed|failed)\s*[:=]\s*\d+|\bLUNE\s+\d+/\d+|^PASS: GATE HEAD\b")
_BASELINE = re.compile(r"(?i)\b(?:source|baseline)\b[^\n]{0,40}?\b(?=[0-9a-f]*\d)([0-9a-f]{7,40})\b")


def _plan_rel(task_id: str) -> str:
    if not _SPRINT_ID.match(task_id or ""):
        raise AdapterError(f"{task_id!r} is not a sprint id")
    return f"docs/progress/{task_id}_PLAN.md"


def _lines(text: str) -> list[str]:
    """Physical lines (\\n only, as grep / editors number them)."""
    return [l.rstrip("\r") for l in (text or "").split("\n")]


def _numbered(lines: list[str], idx) -> str:
    return "\n".join(f"{i + 1}: {lines[i]}" for i in idx)


def _is_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def _suite_line(line: str, task_id: str) -> bool:
    return bool(_SESSION.match(line) or _SUITE.search(line) or _TALLY.search(line)
                or re.search(r"\bGATE %s (?:PASS|FAIL)\b" % re.escape(task_id), line))


def _gate_verdict_and_heads(ev: str, task_id: str):
    """(last 'GATE <task> PASS|FAIL' match or None, ['PASS: GATE HEAD' shas before it])."""
    verdicts = list(re.finditer(r"^GATE %s (PASS|FAIL)\b" % re.escape(task_id), ev, re.M))
    if not verdicts:
        return None, re.findall(r"^PASS: GATE HEAD ([0-9a-f]{7,40})", ev, re.M)
    last = verdicts[-1]
    return last, re.findall(r"^PASS: GATE HEAD ([0-9a-f]{7,40})", ev[:last.start()], re.M)


def _checklist_lines(plan: str) -> list[tuple[int, str]]:
    """[(0-based line index, line)] of the plan's main '## Checklist' section."""
    out, on = [], False
    for i, line in enumerate(_lines(plan)):
        if re.match(r"^##\s+Checklist\b", line):
            if out:                                 # first section only
                break
            on = True
            continue
        if on and line.startswith("## "):
            break
        if on and _TICK.match(line):
            out.append((i, line))
    return out


def _close_step(plan: str) -> str:
    main = _checklist_lines(plan)
    return re.sub(r"^\s*-\s\[[ xX]\]\s*", "", main[-1][1]).strip()[:160] if main else ""


def _section(text: str, name: str) -> str:
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(name), text, re.M | re.S)
    return m.group(1).strip()[:3000] if m else ""


def _title(plan: str, task_id: str) -> str:
    first = plan.splitlines()[0] if plan else ""
    m = re.match(r"#\s*%s\s*[—-]\s*(.+?)(?:\s*[—-]\s*PLAN)?\s*$" % re.escape(task_id), first)
    return (m.group(1) if m else first.lstrip("# "))[:120]
