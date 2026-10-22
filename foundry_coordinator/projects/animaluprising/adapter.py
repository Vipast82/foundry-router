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
Approval      tier-B / visual sprints require revision-bound evidence image
              approval; the configured vision reviewer may provide it.

The coordinator refuses to start any sprint until AGENT_RULES.md carries the
coordinator amendment (RULE_AMENDMENT.md): the one-sprint-per-session rule is
never overridden silently.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

from ...adapters.base import (FAIL, PASS, UNKNOWN, EVIDENCE_ITEM_CHARS, TaskSpec,
                              VerificationResult, bound_text, checklist_mark, duty_bullets,
                              evidence_dir, evidence_item, is_binary_path, report_instructions)
from ...adapters.git import GitError
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
        self._chain_plans: dict = {}        # (task, session, suite) -> GateRunner CALL plan
        self.studio_probe = None            # optional live readiness hook (see readiness)

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

    def _ws(self):
        """Revision reads for callers that don't pass the engine's Workspace
        (verify() is called without one); same class, same workspace."""
        from ...adapters.git import Workspace
        return Workspace(self.p.workspace)

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

    def pending_build_steps(self, spec: TaskSpec, revision: str, ws) -> list[str] | None:
        """Open BUILD checklist rows at committed HEAD; the final row is CLOSE.

        Open means "[ ]" or "[~]": an in-progress row ("[~]" with notes on what
        remains) is progress information, never completion; only "[x]" is done.
        None means the plan has no structured checklist, so the engine must use
        its normal verification path instead of guessing from model prose.
        """
        plan = ws.show_at(revision, spec.data.get("plan") or _plan_rel(spec.task_id))
        if plan is None:
            return None
        rows = _checklist_lines(plan)
        if not rows:
            return None
        pending = [line.strip()[:240] for _, line in rows[:-1]
                   if checklist_mark(line) in ("open", "partial")]
        # Some older plans put the final BUILD gate and CLOSE documentation in
        # one last checkbox. The checkbox remains reserved for CLOSE, but the
        # gate itself is still mandatory BUILD work before final verification.
        last = rows[-1][1]
        if (checklist_mark(last) in ("open", "partial")
                and re.search(r"\bgate\b", last, re.I)
                and spec.data.get("tier") in ("A", "B")
                and self._gate_binding(spec, revision, ws).result != PASS):
            pending.append("Run and commit the final BUILD gate at this revision")
        return pending

    def plan_progress(self, spec: TaskSpec, revision: str, ws) -> list | None:
        """[[done|partial|open, row text]] of the committed plan's main
        checklist at `revision` (CLOSE row included); None = no checklist."""
        try:
            plan = ws.show_at(revision, spec.data.get("plan") or _plan_rel(spec.task_id))
        except Exception:                                         # noqa: BLE001
            return None
        rows = _checklist_lines(plan) if plan else []
        return [[checklist_mark(l), _CHECK_PREFIX.sub("", l).strip()[:240]]
                for _, l in rows] or None

    # tools/gate.ps1 writes these per-sprint files beside the evidence log while a
    # sprint is gated (the expected file hashes and the per-suite verdicts). Other
    # sprints commit them as closeout evidence (GF-UI1, GF-UI1b, GF-FX3a, GF-REPO1,
    # UI-01; see config/workstation/checkpoints/UI-01-*/UI-01_PLAN.md), and UI-02's
    # coordinator findings name exactly these three. Exact, task-scoped paths only:
    # never a glob over gate JSON files.
    GATE_INTERMEDIATE = ("docs/progress/{task}_gate_expect.json",
                         "docs/progress/{task}_gate_expect_full.json",
                         "docs/progress/{task}_gate_verdicts.json")

    def intermediate_outputs(self, spec: TaskSpec) -> list[str]:
        """The exact gate.ps1 output paths of THIS sprint (GATE_INTERMEDIATE;
        [animaluprising] intermediate_outputs may add exact '{task}' paths)."""
        pats = list(self.GATE_INTERMEDIATE) + [
            str(p) for p in (self.cfg.get("intermediate_outputs") or [])
            if isinstance(p, str) and "{task}" in p and not _is_glob(p)]
        return list(dict.fromkeys(p.replace("{task}", spec.task_id) for p in pats))

    def _model_visual_approval(self) -> bool:
        return (self.p.raw.get("approval") or {}).get("visual_model") is True

    def recovery_policy(self, task_id: str) -> str:
        """Current operator policy for recovery agents; historical plan notes may lag it."""
        visual = ("The coordinator's frontier vision reviewer gives final visual approval "
                  "for committed, current-revision captures; do not request Victor's visual "
                  "approval." if self._model_visual_approval() else
                  "Final visual approval still requires the configured approver.")
        physical = ("For UI-01 through UI-06, actual mouse, touch and controller operator "
                    "flows are required in UI-GT1 after UI-06, not in this sprint. Use named "
                    "ButtonActions clicks for functional handler checks here; never count them "
                    "as physical-input proof or ask Victor to perform the deferred flows now. "
                    if task_id in {f"UI-0{i}" for i in range(1, 7)} else
                    "For UI-GT1 and other tasks with physical-input acceptance, attempt the "
                    "available Roblox Studio input tools in each required mode and record the "
                    "result. Escalate to Victor only the exact mode or action the tools cannot "
                    "perform, with attempted tools and evidence. ")
        return (f"Current policy for {task_id}: {visual} The primary must first use the "
                "enabled vision helper to inspect each actual capture and record its "
                f"OBSERVATION. {physical}Older plan NEXT/NEEDS_HUMAN lines are "
                "historical evidence, not authority to bypass these attempts. Never waive "
                "a check or change a baseline without observed raw runs and a recorded "
                "scope/test decision.")

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
                  or "COMPLETE-PENDING-VISUAL-REVIEW" in _accept_section(plan))
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
        acceptance = _accept_section(plan)
        nxt = [l for l in plan.splitlines() if l.startswith("NEXT:")]
        checklist = "\n".join(l for l in plan.splitlines() if checklist_mark(l))
        visual_gate = (" Visual sprint: final screenshot sign-off is by the "
                       "coordinator's vision reviewer after closeout."
                       if self._model_visual_approval() else
                       " Visual sprint: status is at most COMPLETE-PENDING-VISUAL-REVIEW "
                       "until Victor approves the evidence images.")
        spec = "\n".join(x for x in [
            f"Plan: {rel} (read it first; it is the sprint's memory, AGENT_RULES §34).",
            f"Tier: {tier}." + (visual_gate if visual else ""),
            "## Goal", goal.strip(), "## Acceptance lines", acceptance.strip(),
            "## Checklist", checklist, ("## Last NEXT line\n" + nxt[-1]) if nxt else ""] if x)
        return TaskSpec(project_id=self.p.id, task_id=task_id, title=_title(plan, task_id),
                        spec=spec, scope=scope, tags=tags, deps=deps,
                        data={"tier": tier, "visual": visual, "plan": rel,
                              "close_step": _close_step(plan)})

    _FILES_HEADING = re.compile(r"^#{2,4}\s+Files this sprint may change", re.I)

    def grant_scope(self, spec: TaskSpec, path: str, reason: str) -> str:
        """revamp B10: add `path` to the plan's "Files this sprint may change" list
        (one "- `path` -- reason" line after the last entry) for a coordinator
        scope grant. The plan is re-parsed with done_check's own parser; if the
        path still isn't in scope the file is restored and AdapterError raised.
        Returns the plan's workspace-relative path ('' = already listed)."""
        rel = _plan_rel(spec.task_id)
        full = os.path.join(self.p.workspace, rel)
        with open(full, encoding="utf-8", newline="") as f:
            text = f.read()
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(nl)
        start = next((i for i, l in enumerate(lines) if self._FILES_HEADING.match(l.strip())),
                     None)
        if start is None:
            raise AdapterError(f"{rel} has no 'Files this sprint may change' section")
        last = start
        for i in range(start + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("#"):
                break
            if re.match(r"^[-*]\s+`", s):
                last = i
                if s.split("`")[1].strip() == path:
                    return ""
        why = re.sub(r"\s+", " ", reason or "").strip()[:200]
        lines.insert(last + 1, f"- `{path}` -- {why}" if why else f"- `{path}`")
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(nl.join(lines))
        if path not in self._parse_plan(spec.task_id, rel, nl.join(lines)).scope:
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            raise AdapterError(f"{rel}: done_check does not read {path} from the edited "
                               f"file list; nothing changed")
        return rel

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

    # -- fast lane (UI-T0, [fast_lane] in the profile) ---------------------------------

    FAST_LANE_RULES = "docs/rules/FAST_LANE_RULES.md"

    def _fast_lane(self, task_id: str) -> bool:
        fn = getattr(self.p, "fast_lane_task", None)
        return bool(fn(task_id)) if callable(fn) else False

    def fast_lane_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        """The BUILD prompt of a fast-lane sprint: the rules card and the
        au-fast-lane skill instead of the AGENT_RULES ceremony, the one-page plan,
        the studio_gate runner loop. The coordinator runs the full gate itself as
        the verify step and sends red gates / vision findings into this session."""
        from types import SimpleNamespace
        from ...fast_lane import is_local_primary, settings
        t = spec.task_id
        P = SimpleNamespace(profile=self.p)
        gm = settings(P).get("gate_module") or "foundry_coordinator.projects.animaluprising.studio_gate"
        runner = f"py -m {gm} --repo {self.p.workspace}"
        local = is_local_primary(P, t)
        authorship = (
            "This sprint's primary is LOCAL (training-eligible): helpers give guidance, "
            "interfaces, stories, fixtures, wiring checks, API notes and reviews; you write the "
            "implementation. Never request, copy or paraphrase a helper's implementation. A "
            "helper-drafted step (ladder step 3) is its own commit with `(helper:<unit>)` in the "
            "message. After repeated red full gates the coordinator may hand the sprint to the "
            "frontier primary in this same session; you never switch models yourself."
            if local else
            "This sprint's primary is FRONTIER: helper PREWORK patches may be applied after you "
            "verify each one (plan files only, compiles at HEAD, its story/spec passes).")
        return f"""BUILD session for fast-lane sprint {t} — {spec.title} (coordinator run {ctx.get('run_id')}, attempt {ctx.get('attempt')}).

Fast lane: load the `au-fast-lane` skill first (plus `au-ui-v3` and `au-roblox-ui-development` for UI work). The rules card {self.FAST_LANE_RULES}, that skill and the one-page plan replace the AGENT_RULES ceremony for this task; AGENT_RULES.md still governs anything the card doesn't mention. You run exactly one sprint in this session: {t}.
Plan (the whole spec, read all of it): docs/progress/{t}_PLAN.md. Gate manifest (stories, sizes, wiring checks): docs/progress/{t}_gate.json. Helper drafts and notes: .plan/tmp/prework/{t}/.

{spec.spec}

{authorship}

Runner (flags fixed by UI-T0; run `--help` once if one is rejected):
  {runner} doctor
  {runner} stories --sprint {t} [--story NAME ...]      # Edit-mode stories, the fast inner loop
Inner loop: write code -> stories -> fix -> repeat; Lune specs for the models you touched. Run stories and Lune only: never run `sprint` (or `regression`) yourself. The coordinator runs the one full gate when you report DONE, re-runs only checks whose inputs changed, reads the verdict JSON under .plan/tmp/gate/{t}/ and sends any red check or vision finding back into THIS session. Commit by explicit path from the plan's file list, message starting "{t}: " (no --no-verify, force, rebase, amend or tag). The runner writes captures under docs/evidence/UI-v3/{t}/ and its evidence log; the coordinator commits those itself, so leave them uncommitted.
While a gate holds Studio, do not call Studio tools yourself. Put scratch files under `.plan/tmp/` (git-ignored).
Do NOT write {t}_SUMMARY.md, the STATE.md row or the MASTER_ORDER status, and do NOT push: a short CLOSE session does that after a green gate, the vision approval and the review.
The coordinator refreshes `{self.LIVE_CHECKPOINT}` (read-only) while you work; never call the Kilo server or port 4096 yourself.
Stop (BLOCKED, with 2-4 `options` and the one you `recommended`; nobody is asked mid-session, the coordinator decides) only for a contract the plan doesn't settle, a product decision, a doctor failure you can't fix, or a §24 safety stop. A needed file outside the file list is a SCOPE_REQUEST block, not a stop. A routine red check is not a stop. Vision CHANGES come back into this session once; after that the coordinator accepts with notes.

{report_instructions()}
"""

    def implement_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        if self._fast_lane(spec.task_id):
            return self.fast_lane_prompt(spec, ctx)
        planning = ""
        menu_actions = ""
        if spec.task_id.startswith("UI-") or any(
                path.startswith("src/client/UI/") for path in spec.scope):
            menu_actions = """
For Studio client menu setup and functional button tests, use
`require(game.Players.LocalPlayer.PlayerScripts.Client.UI.ButtonActions)`:
`List()` gives current names/paths, `Click(nameOrPath)` invokes the same handler,
and `Unbound()` exposes buttons missing a callable handler. Open each relevant
menu, inspect `List()` and require `Unbound()` to be empty; never guess an
ambiguous path or bypass a hidden/disabled button. Every new menu click handler
must use `ButtonActions.Bind` or the kit's `Widgets.button` callback, with a
stable descriptive name. The headless ButtonActions spec guards this contract.
Named handler calls are for setup and functional menu tests. They do not prove
pointer hit testing, dragging, or actual mouse/touch/controller input.
"""
            if spec.task_id in {f"UI-0{i}" for i in range(1, 7)}:
                menu_actions += """
Victor's 2026-10-19 phase decision transfers actual mouse, touch and
controller operator flows to required UI-GT1 after UI-06. In this sprint,
finish the named-handler, authoritative-state, input-mode-state, layout,
Studio suite, visual, gate and closeout checks. UI-02 c11 handler results may
use named actions; c11 physical target testing and c12 operator flows belong
to UI-GT1. Do not request Victor's physical clicks here. Historical NEXT lines
in pre-coded plans do not restore the old physical-input requirement.
"""
            elif spec.task_id == "UI-GT1":
                menu_actions += """
UI-GT1 is the required physical-input phase. Use actual Studio mouse, touch
and controller input for every transferred matrix row, with raw before/after
evidence. ButtonActions.Click may prepare state or isolate a handler, but it
never satisfies a physical-input row.
"""
        if self.p.roles.get("planner"):
            planning = """
The approved plan and its checklist are this attempt's plan: do not spawn planner or vision subagents (only the read-only `explore` subagent is available; the coordinator plans, and its own vision reviewer checks every committed capture). Integrate any advice into the approved BUILD work in this Code session; advice is never acceptance evidence or permission to widen scope. The primary alone writes and controls Studio. Record measured validity lines for each capture and recapture invalid, stale, occluded or wrong-subject frames; an inability of this primary model to see images is not a human blocker. Use Studio MCP to attempt physical input tests only when this sprint's current plan requires them; UI-01 through UI-06 operator flows are deferred to UI-GT1. {SIGN_OFF}Stop for Victor only for a genuinely unavailable human-only interaction, credentials/OS action, a product decision outside the plan, or an unresolved blocker after configured recovery. Never waive a failed/unknown gate or weaken a test to avoid a stop.
"""
            # final sign-off is delegated only when [approval] visual_model is on;
            # otherwise the operator's visual approval still closes the sprint
            planning = planning.replace("{SIGN_OFF}", (
                "Victor delegated final screenshot sign-off to the coordinator vision "
                "reviewer; submit current committed frames for that review instead of "
                "waiting for his visual approval. ") if self._model_visual_approval() else "")
        return f"""BUILD session for sprint {spec.task_id} — {spec.title} (coordinator run {ctx.get('run_id')}, attempt {ctx.get('attempt')}).

{self.rules_text(spec)}
Read docs/progress/STATE.md header + last 5 ledger rows, then docs/progress/{spec.task_id}_PLAN.md, and resume from its checklist and NEXT line (§34 "Plan = memory"). Do not redo ticked steps unless their evidence is missing.

{spec.spec}
{planning}
{menu_actions}

This session's job: the plan's remaining BUILD steps, each committed by explicit path with a message starting "{spec.task_id}: ", then the gate on committed code:
  pwsh -NoProfile -ExecutionPolicy Bypass -File tools/gate.ps1 -Sprint {spec.task_id} -Files <files under test> -Tier {spec.data.get('tier', 'A')} ...
including its Studio steps and -Finish, so the evidence log ends with "GATE {spec.task_id} PASS|FAIL" (§21 boot lines first).
Do NOT write {spec.task_id}_SUMMARY.md, the STATE.md row or the MASTER_ORDER status, and do NOT push: after the coordinator's checks and a frontier review, a separate CLOSE session does that.
Never edit files outside the plan's "Files this sprint may change" list (§3, the commit hook enforces it). Never --no-verify, --force, rebase, amend or tag.
Helper PREWORK notes in the coordinator memory below were prepared ahead of time for your next steps: when one matches the step you are on and its HEAD is current, `git apply --check` its draft patch, apply it, run the listed checks and commit, instead of re-deriving the change; verify it against the plan first (it is advisory, never authority). Put verification scratch and one-off generated files under `.plan/tmp/` (git-ignored), never elsewhere in the repo.
§34 checkpoints: the coordinator audits this session itself and refreshes `{self.LIVE_CHECKPOINT}` (read-only) about every two minutes while you work; read it for the KILO-CHECKPOINT verdict and act on CONTINUE/STOP-AND-REPLAN. Do not run kilo_checkpoint against the Kilo server or call port 4096 (it needs credentials you don't have); if the file is missing or its header time is more than 10 minutes old, note "KILO-CHECKPOINT pending (coordinator audit)" and continue.
Before reporting NEEDS_HUMAN for a visual or interaction step, use the available Studio MCP tools as described above. If the step still needs Victor (an ACTION NEEDED FROM VICTOR block, §22) or a decision the plan doesn't settle (§10), stop and report NEEDS_HUMAN or BLOCKED with the exact attempted tools and blocker text, plus 2-4 `options` and the one you `recommended` (the coordinator decides; nobody is asked mid-session).

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
        approval_tail = ("recorded visual approval" if self._model_visual_approval()
                         else "Victor's approval")
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
                     f"issues tags after DONE / {approval_tail}).",
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
            approver = ("the coordinator's vision reviewer" if self._model_visual_approval()
                        else "Victor")
            reviewed.append(f"The required evidence captures under docs/evidence/{t}/ at the "
                            f"reviewed commit (final visual approval is recorded later by "
                            f"{approver})")
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

    def verify(self, spec: TaskSpec, phase: str, revision: str, base: str, ws=None) -> list:
        out = self._done_check(spec, phase, revision)
        if phase == "verify" and spec.data.get("tier") in ("A", "B"):
            # tier C has no gate (§30): its proof is the empty src/tests diff in done_check
            out.append(self._gate_binding(spec, revision, ws))
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

    def _gate_binding(self, spec: TaskSpec, revision: str, ws=None) -> VerificationResult:
        """The last gate verdict must be PASS and the gate must have run at the
        reviewed code: only closeout-exempt files (the evidence log itself,
        studio lines) may change between the gate's HEAD and this revision.
        The log is read at `revision` (git show), never from the working tree:
        a dirty, uncommitted log line can't make the committed code pass."""
        rel = f"docs/progress/{spec.task_id}_EVIDENCE_RAW.log"
        ev = (ws or self._ws()).show_at(revision, rel)
        if ev is None:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, "",
                                      f"{rel} missing at {revision[:12]}", revision)
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

    LIVE_CHECKPOINT = ".plan/tmp/kilo_checkpoint.txt"

    def live_session_audit(self, session_id: str, transcript: list, revision: str) -> str:
        """The same audit while the session works, written for the implementer
        to read (git-ignored, read-only): it never calls the Kilo server itself,
        which needs the coordinator's password. Returns the path written."""
        from .kilo_checkpoint import audit
        text, _level = audit(self.p.workspace, session_id, transcript)
        path = os.path.join(self.p.workspace, *self.LIVE_CHECKPOINT.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = (f"# KILO-CHECKPOINT written by the coordinator for session kilo:{session_id}\n"
                f"# at {stamp} (HEAD {revision[:12]}); refreshed while the session works.\n"
                f"# Read-only: do not edit, and do not call the Kilo server for this.\n\n"
                + text.rstrip() + "\n")
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        if os.path.exists(path):
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)   # Windows: a read-only file can't be replaced
        os.replace(tmp, path)
        os.chmod(path, stat.S_IREAD)
        return path

    def closeout_mode(self) -> str:
        return "session"

    # B7 (2026-10-21): the coordinator templates the CLOSE duties itself; the CLOSE
    # session above runs only when this fails (closeout_template.py, engine._closing)
    def closeout_template(self, spec: TaskSpec, ctx: dict) -> dict:
        from . import closeout_template
        return closeout_template.build(self, spec, ctx)

    def closeout_template_finish(self, spec: TaskSpec, ctx: dict) -> dict:
        from . import closeout_template
        return closeout_template.finish(self, spec, ctx)

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
            # gate.ps1 may append the current session's Studio transcript between
            # its FINISH rows and final verdict. Admit that one explicitly named
            # transcript only when its task and HEAD agree with this gate; a
            # generic/new session remains a boundary, never a source of a later
            # verdict for an incomplete finish block.
            preamble = max((i for i in range(start)
                            if re.match(r"^=== GATE (?:RUN|START|FINISH)\b", lines[i])
                            or re.match(r"^GATE RUN\b", lines[i])
                            or re.match(r"^GATE \S+ (?:PASS|FAIL)\b", lines[i])), default=-1)
            gate_heads = re.findall(r"^PASS: GATE HEAD ([0-9a-f]{7,40})",
                                    "\n".join(lines[preamble + 1:start]), re.M)
            inline_studio = re.compile(
                r"^SESSION\s+\d+\s+studio lines\s+\(coordinator run "
                + re.escape(t) + r"-a\d+-[\w-]+ attempt \d+\), HEAD ([0-9a-f]{7,40})\b")
            admitted_studio = False
            end = min(len(lines) - 1, start + 300)
            for j in range(start + 1, end + 1):
                if verdict_re.match(lines[j].strip()):
                    end = j
                    break
                if _SESSION.match(lines[j]):
                    # Inline Studio transcripts label quoted historical verdict
                    # lines with dashed headings; those are attribution inside
                    # the current transcript, not a new session/run boundary.
                    if admitted_studio and re.match(r"^--- SESSION\b.*---\s*$", lines[j]):
                        continue
                    embedded = inline_studio.match(lines[j])
                    named = embedded.group(1) if embedded else ""
                    bound = gate_heads[-1] if gate_heads else ""
                    if (not admitted_studio and named and bound
                            and (named.startswith(bound) or bound.startswith(named))):
                        admitted_studio = True
                        continue
                    end = j - 1
                    break
                if (lines[j].startswith("=== GATE")
                        or re.match(r"^GATE RUN\b", lines[j])
                        or re.match(r"^PASS: GATE HEAD\b", lines[j])
                        or re.match(r"^GATE \S+ (?:PASS|FAIL)\b", lines[j])):
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
        # the gate's own captures: the post-PASS capture commit always lands after the gate
        exempt = self.exempt_paths(spec) + [f"docs/progress/{t}_*", f"docs/evidence/UI-v3/{t}/**"]
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
                                 _accept_section(plan), re.I):
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
        ticked = sum(1 for _, l in main if checklist_mark(l) == "done")
        partial = sum(1 for _, l in main if checklist_mark(l) == "partial")
        body, cut, omitted = bound_text(
            f"# {rel}@{revision} blob {blob}\n" + "\n".join(f"{i + 1}: {l}" for i, l in main), cap)
        notes = [f"{ticked} of {len(main)} items ticked"
                 + (f"; {partial} in progress ([~]: partly done, still open)" if partial else "")
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

    # -- evidence / lifecycle contract (v0.122; every hook optional, engine uses getattr) ----
    #
    # Check ids:   suite:<SUITE>       GateRunner.tally verdict (SUITE .. VERDICT) or the
    #                                  explicit "PASS:|FAIL:|SKIP: GATE suite <SUITE>" line
    #              regression:<SUITE>  "GATE regression <SUITE> - pass= skip= fail=" tally
    #              gate:<TASK>         gate.ps1 "GATE <TASK> PASS|FAIL" (+ GATE HEAD before it)
    #              boot:server|client  GateRunner.boot() "BOOT(vm) loaded= errors=" + PASS:/FAIL:
    #              call:<SUITE>:<seq>  one GateRunner CALL of a stateful chain (seq from the
    #                                  run(<SUITE>) CALL plan; call:<SUITE>:<fn> when unknown)
    #              plan:<SUITE>        the run(<SUITE>) CALL plan itself (result NA)
    #              runtime:verify_live GateRunner.verifyLive VERIFY tally (loaded source vs map)
    #              lune                "LUNE n/m PASS|FAIL"
    #              capture:<path>      capture-window.ps1 "Saved: <path>" (+ VALID=)
    # A result comes only from such verdict / tally lines. Individual PASS:/FAIL: lines give
    # UNKNOWN with counts, never PASS. A PASS its own counts contradict (fail > 0, or
    # pass != expect) is UNKNOWN. Verdict sources (_source_exclusion): a Studio/runtime MCP
    # tool, or a shell chain (&& ; | and powershell/pwsh/cmd/sh wrappers unwrapped) that
    # ran tools/gate.ps1 (or `lune run`, for the lune tally only) and reads no file outside
    # a pipe; anything else is read_only_command / not_verdict_source. A verdict line found
    # in the input, verbatim or as string literals holding all its verdict words, is
    # verdict_in_input. Boot and verifyLive use the last report in the output.

    def classify_tool_result(self, view: dict, ctx: dict | None = None) -> list[dict]:
        """One completed tool call -> check records (pure parsing; never infers a
        verdict from prose or from individual PASS lines)."""
        ctx = ctx or {}
        view = view or {}
        task = ctx.get("task_id") or ""
        inp = _view_input(view)
        tool = str(view.get("tool") or "")
        lines, _structured = _decode_output(view.get("output"))
        text = "\n".join(lines)
        recs: list[dict] = []

        def rec(check_id, kind, result=UNKNOWN, **kw):
            r = {"check_id": check_id, "check_kind": kind, "chain_id": None, "seq": None,
                 "prereqs": [], "result": result, "counts": None, "exclusion": None,
                 "runtime_hint": None, "artifact_path": None, "lines": []}
            r.update(kw)
            if kind in ("suite", "regression", "lune"):          # counts must agree with it
                r["result"], why = _count_check(r["result"], r["counts"])
                if why:
                    r["note"] = why
            recs.append(r)
            return r

        # 1. explicit suite verdicts (GateRunner.tally) and GATE suite lines
        suites: dict[str, dict] = {}
        for i, l in enumerate(lines):
            s = l.strip()
            m = _SUITE_VERDICT.match(s)
            if m:
                counts = {"pass": int(m.group(2)), "fail": int(m.group(3))}
                if m.group(4) is not None:
                    counts["expect"] = int(m.group(4))
                if m.group(5) is not None:
                    counts["designed_fails"] = int(m.group(5))
                suites[m.group(1)] = rec(f"suite:{m.group(1)}", "suite", m.group(6),
                                         counts=counts, chain_id=m.group(1), lines=[s])
                continue
            m = _GATE_SUITE.match(s)
            if m:
                verdict = {"PASS": PASS, "FAIL": FAIL}.get(m.group(1), "NA")
                r = suites.get(m.group(2))
                if r is None:
                    suites[m.group(2)] = rec(f"suite:{m.group(2)}", "suite", verdict,
                                             chain_id=m.group(2), lines=[s])
                else:
                    r["lines"].append(s)
                    if r["result"] != verdict:            # the two verdict lines disagree
                        r["result"] = UNKNOWN
                        r["note"] = "; ".join(x for x in (
                            r.get("note"), "SUITE verdict and GATE suite line disagree") if x)
                continue
            m = _GATE_REGRESSION.match(s)
            if m:
                rec(f"regression:{m.group(1)}", "regression",
                    _regression_verdict(lines, m.group(1)),
                    counts={"pass": int(m.group(2)), "skip": int(m.group(3)),
                            "fail": int(m.group(4))}, lines=[s])
                continue
            m = _SHORT_TALLY.match(s)
            if m and m.group(1) not in suites:
                suites[m.group(1)] = rec(f"suite:{m.group(1)}", "suite", m.group(2),
                                         counts={"pass": int(m.group(3)),
                                                 "expect": int(m.group(4))},
                                         chain_id=m.group(1), lines=[s])
                continue
            m = _LUNE.match(s)
            if m:
                rec("lune", "lune", m.group(3),
                    counts={"pass": int(m.group(1)), "expect": int(m.group(2))}, lines=[s])

        # 2. the gate verdict (gate.ps1), bound to the GATE HEAD printed before it
        verdicts = [(i, m) for i, l in enumerate(lines)
                    for m in [_GATE_VERDICT.match(l.strip())]
                    if m and m.group(1) not in _GATE_WORDS and (not task or m.group(1) == task)]
        if verdicts:
            i, m = verdicts[-1]
            heads = [h.group(1) for h in (_GATE_HEAD.match(l.strip()) for l in lines[:i]) if h]
            rec(f"gate:{m.group(1)}", "gate", m.group(2), lines=[m.group(0)],
                gate_head=heads[-1] if heads else None,
                note="" if heads else "no 'PASS: GATE HEAD <sha>' before the verdict")

        # 3. boot (CALL 1-2 of every GateRunner plan); the LAST report in the output wins
        for vm in ("server", "client"):
            boot = ([m for m in (_BOOT.match(l.strip()) for l in lines)
                     if m and m.group(1) == vm] or [None])[-1]
            if not boot:
                continue
            verdict = ([v.group(1) for v in (_BOOT_VERDICT.match(l.strip()) for l in lines)
                        if v and v.group(2) == vm] or [None])[-1]
            rec(f"boot:{vm}", "boot", verdict or UNKNOWN, chain_id="boot",
                seq=1 if vm == "server" else 2, prereqs=[] if vm == "server" else ["boot:server"],
                counts={"loaded": int(boot.group(2)), "errors": int(boot.group(3))},
                lines=[boot.group(0)])

        # 4. the loaded-source check (GateRunner.verifyLive); the LAST report wins
        vl = ([m for m in (_VERIFY_LIVE.match(l.strip()) for l in lines) if m] or [None])[-1]
        if vl:
            ok = int(vl.group(3)) == 0 and int(vl.group(4)) == 0 and vl.group(1) == vl.group(2)
            rec("runtime:verify_live", "runtime", PASS if ok else FAIL,
                counts={"matched": int(vl.group(1)), "total": int(vl.group(2)),
                        "mismatched": int(vl.group(3)), "unresolved": int(vl.group(4)),
                        "skipped": int(vl.group(5) or 0)}, lines=[vl.group(0)])

        # 5. a GateRunner CALL plan (run(<SUITE>)): remembered for seq of later calls
        plan = [m for m in (_CALL_PLAN.match(l.strip()) for l in lines) if m]
        if plan:
            suite = _plan_suite(inp, text)
            steps = [{"seq": int(m.group(1)), "vm": m.group(2), "tool": m.group(4),
                      "code": _norm_code(m.group(5))} for m in plan]
            if suite:
                self._chain_plans[(task, view.get("session_id") or "", suite)] = steps
                rec(f"plan:{suite}", "plan", "NA", chain_id=suite, chain_plan=steps)

        # 6. one CALL of a stateful chain (a <SUITE>_check module function)
        step = _check_call(inp)
        if step and not suites and not plan:
            suite, fn = step
            steps = ((ctx.get("chain_plans") or {}).get(suite)
                     or self._chain_plans.get((task, view.get("session_id") or "", suite))
                     or self._latest_plan(task, suite))
            seq = _match_call(steps, inp, tool)
            p_, f_ = _step_counts(lines)
            rec(f"call:{suite}:{seq if seq else fn}", "call", UNKNOWN, chain_id=suite, seq=seq,
                prereqs=([f"call:{suite}:{seq - 1}"] if seq and seq > 3 else
                         ["boot:server", "boot:client"]),
                counts={"pass": p_, "fail": f_}, step=fn,
                note="individual PASS/FAIL lines: the suite verdict comes from GateRunner.tally")

        # 7. captures (capture-window.ps1's own record)
        from ...artifacts import parse_capture_output
        for c in parse_capture_output(text):
            path = self._rel_path(c["path"])
            rec(f"capture:{path}", "capture",
                PASS if c.get("valid") == "ok" else (FAIL if c.get("valid") == "invalid"
                                                     else UNKNOWN),
                artifact_path=path, lines=c.get("lines", [])[:10],
                note=c.get("reason") or "")

        # 8. individual PASS/FAIL lines with no verdict at all: UNKNOWN with counts
        if not recs:
            p_, f_ = _step_counts(lines)
            tags = sorted({m.group(1) for m in (_TAGGED_STEP.match(l.strip()) for l in lines)
                           if m})
            if p_ or f_:
                sid = tags[0] if len(tags) == 1 else None
                rec(f"suite:{sid}" if sid else "unattributed", "steps", UNKNOWN,
                    chain_id=sid, counts={"pass": p_, "fail": f_},
                    note="individual PASS/FAIL lines without a suite verdict")
        if not recs:
            return []

        # exclusions and runtime binding, applied to every record of this call
        exclusion = self._exclusion(view, inp, tool, text, recs, ctx)
        hint = self.runtime_identity(view) or ctx.get("runtime_hint")
        for r in recs:
            r["exclusion"] = exclusion or _source_exclusion(tool, inp, r["check_kind"])
            r["runtime_hint"] = hint
            r["truncated"] = bool(view.get("truncated"))
            if r["truncated"] and r["check_kind"] != "capture":
                r["note"] = ((r.get("note") or "") + "; output truncated: never credited above "
                             "observed").lstrip("; ")
            if r["lines"]:
                r["lines"] = r["lines"][:20]
        return recs

    def _latest_plan(self, task: str, suite: str):
        for (t, _s, su), steps in reversed(list(self._chain_plans.items())):
            if t == task and su == suite:
                return steps
        return None

    def _rel_path(self, path: str) -> str:
        p = (path or "").replace("\\", "/").strip().strip('"')
        root = (self.p.workspace or "").replace("\\", "/").rstrip("/")
        if root and p.lower().startswith(root.lower() + "/"):
            p = p[len(root) + 1:]
        return p

    def _exclusion(self, view, inp, tool, text, recs, ctx) -> str | None:
        status = str(view.get("status") or "completed")
        if status == "error":
            return "tool_error"
        if status not in ("completed",):
            return "aborted"
        if _read_only(tool, inp):
            return "read_only_command"
        if any(_built_in_input(l, inp) for r in recs if r["check_kind"] in _VERDICT_KINDS
               for l in r["lines"]):
            return "verdict_in_input"           # the "result" was typed / built in the input
        if ctx.get("runtime_void") or self.void_runtime(view):
            return "void_runtime"
        if ctx.get("parallel") or _overlaps(view, ctx.get("concurrent") or []):
            return "parallel"
        if ctx.get("duplicate"):
            return "duplicate"
        return None

    def void_runtime(self, view: dict) -> bool:
        """This call reports that its Studio runtime is not a valid test VM: a
        playtest that didn't start or tear down cleanly, a bridge/VM error, or no
        player in the world. Calls made earlier in that VM are the ledger's to
        void (it knows the session order); this answers for one view only."""
        view = view or {}
        tool = str(view.get("tool") or "")
        if not re.search(r"(?i)studio|roblox|luau|runtime|playtest", tool):
            return False
        raw = view.get("output")
        body = None
        if isinstance(raw, dict):
            body = raw
        elif isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        if isinstance(body, dict):
            if body.get("timedOut") or body.get("playtestTaskPending") or \
                    body.get("editModeReady") is False:
                return True
            if "playtest" in tool and body.get("success") is False:
                return True
            if body.get("bridge") not in (None, "ok") or (body.get("ok") is False and
                                                          "bridge" in body):
                return True
        lines, _ = _decode_output(raw)
        # quoted code (a CALL plan, a NOTE, an error("...") literal) is not a report
        said = [l for l in lines if not re.match(r"^\s*(?:CALL|NOTE)\b", l)
                and "error(" not in l]
        text = "\n".join(said) + "\n" + str(view.get("error") or "")
        return bool(_VOID_TEXT.search(text))

    def runtime_identity(self, view_or_text) -> dict | None:
        """What the Studio runtime reported about itself: BOOT counts per VM, the
        place id, the GATE HEAD, and GateRunner.verifyLive's per-file live
        source hashes (FNV-1a 32 of each ModuleScript's Source). `loaded_fp`
        is a manifest hash over those live per-file hashes, i.e. a fingerprint of
        the code the VM actually loaded, for the files it was asked about. None
        when the text carries no runtime line."""
        if isinstance(view_or_text, dict):
            lines, _ = _decode_output(view_or_text.get("output"))
        else:
            lines, _ = _decode_output(view_or_text)
        ident: dict = {}
        boots = {}
        for l in lines:
            s = l.strip()
            m = _BOOT.match(s)
            if m:
                boots[m.group(1)] = {"loaded": int(m.group(2)), "errors": int(m.group(3))}
            m = _GATE_HEAD.match(s)
            if m:
                ident["gate_head"] = m.group(1)
            m = _PLACE.search(s)
            if m:
                ident["place_id"] = m.group(1)
        files, inconsistent = {}, []
        for l in lines:
            m = _LIVE_FILE.match(l.strip())
            if m and m.group(1) in ("MATCH", "MISMATCH"):
                vals = set(m.group(3).split("/"))
                if len(vals) > 1:
                    inconsistent.append(m.group(2))
                files[m.group(2)] = m.group(3)
        vl = next((m for m in (_VERIFY_LIVE.match(l.strip()) for l in lines) if m), None)
        if boots:
            ident["boot"] = boots
        if vl:
            ident["verify"] = {"matched": int(vl.group(1)), "total": int(vl.group(2)),
                               "mismatched": int(vl.group(3)), "unresolved": int(vl.group(4))}
        if files:
            ident["files"] = files
            ident["loaded_fp"] = None if inconsistent else _manifest_fp(files)
            if inconsistent:
                ident["inconsistent"] = inconsistent    # server and client VMs differ
        if not ident:
            return None
        ident["kind"] = "roblox_studio"
        # a stable id for the evidence ledger (ev_runtimes): what the VM loaded
        # when known, else its place/boot identity; never a dict repr
        import hashlib
        if ident.get("loaded_fp"):
            basis = "fp:" + str(ident["loaded_fp"])
        else:
            basis = "id:" + json.dumps({k: ident.get(k) for k in ("place_id", "boot", "gate_head")
                                        if ident.get(k)}, sort_keys=True, default=str)
        ident["runtime_id"] = "roblox_studio:" + hashlib.sha256(basis.encode()).hexdigest()[:16]
        return ident

    def runtime_expected_files(self, spec: TaskSpec, revision: str, ws=None,
                               paths=None) -> dict | None:
        """{path: fnv1a32 of the committed blob} for the runtime manifest (or
        `paths`). Assumption, unconfirmed offline: GateRunner hashes the
        ModuleScript Source bytes exactly as committed (no line-ending
        rewrite). Callers must treat a mismatch caused by that assumption as
        stale, never as fresh."""
        ws = ws or self._ws()
        paths = list(paths) if paths is not None else self._runtime_manifest(spec)
        if not paths:
            return None
        out = {}
        for rel in paths:
            data = _blob_bytes(ws, revision, rel)
            if data is not None:
                out[rel] = "%08x" % _fnv1a32(data)
        return out or None

    def runtime_expected(self, spec: TaskSpec, revision: str) -> str | None:
        """The loaded_fp `revision` should produce, over the runtime manifest
        (the plan's non-glob src/ and tests/ Lua files + tests/GateRunner.lua).
        Returned only when the profile opts in ([animaluprising]
        runtime_fingerprint = "fnv1a32-blob"): the hash rule is not verifiable
        offline, and it equals runtime_identity's loaded_fp only when the
        agent's verifyLive map covered exactly that manifest. Otherwise None:
        freshness then rests on the dependency fingerprint ("runtime
        unverified"). Use runtime_matches for a per-file comparison."""
        if self.cfg.get("runtime_fingerprint") != "fnv1a32-blob":
            return None
        files = self.runtime_expected_files(spec, revision)
        return _manifest_fp(files) if files else None

    def runtime_matches(self, identity: dict | None, spec: TaskSpec, revision: str,
                        ws=None) -> tuple:
        """(True|False|None, reason): every file the runtime reported hashes the
        same as at `revision`. None when the runtime reported no files."""
        files = (identity or {}).get("files") or {}
        if not files:
            return None, "the runtime reported no per-file source hashes (runtime unverified)"
        want = self.runtime_expected_files(spec, revision, ws, paths=list(files)) or {}
        bad = [p for p, live in files.items()
               if p in want and want[p] not in str(live).split("/")]
        absent = [p for p in files if p not in want]
        if bad:
            return False, "runtime loaded other code for: " + ", ".join(sorted(bad)[:8])
        if absent:
            return None, "not at the revision (cannot compare): " + ", ".join(sorted(absent)[:8])
        return True, f"{len(files)} loaded files match {revision[:12]}"

    def _runtime_manifest(self, spec: TaskSpec) -> list[str]:
        out = [s for s in spec.scope if not _is_glob(s) and s.startswith(("src/", "tests/"))
               and s.lower().endswith((".lua", ".luau"))]
        if out and "tests/GateRunner.lua" not in out:
            out.append("tests/GateRunner.lua")
        return sorted(out)

    # -- required checks / dependencies / obligations, from the plan at the revision ----

    def _plan_text(self, spec: TaskSpec, ws=None) -> str | None:
        rel = spec.data.get("plan") or f"docs/progress/{spec.task_id}_PLAN.md"
        rev = spec.data.get("spec_revision")
        if rev:
            return (ws or self._ws()).show_at(rev, rel)
        try:
            return self._read(rel)
        except OSError:
            return None

    def _plan_requirements(self, spec: TaskSpec, ws=None) -> dict:
        """Suites, gate and captures the plan requires. Read from the plan's
        Acceptance / Checklist / test-gate sections and its file list, with
        every NEXT: line removed: a stop note never adds, drops or moves one."""
        t = spec.task_id
        plan = self._plan_text(spec, ws) or ""
        body = _drop_next(plan)
        secs = _sections(body)
        wanted = "\n".join(v for k, v in secs if _REQ_SECTION.search(k))
        own = re.sub(r"[-.]", "_", t)
        # a suite is required by list / table structure or a run / verdict verb, never by a
        # bare mention in prose (_required_suites has the rule)
        suites = [s for s in _required_suites(wanted) if s != own]
        if (re.search(r"\b%s\b" % re.escape(own), wanted)
                or any(s.startswith(f"tests/{own}_check.") for s in spec.scope)):
            suites.insert(0, own)
        captures = self._required_captures(spec, body)
        tier = spec.data.get("tier") or "A"
        return {"suites": suites, "gate": tier in ("A", "B"), "captures": captures,
                "acceptance": _accept_section(body), "plan": plan,
                "lune": bool(re.search(r"\bLUNE\b|\blune\b", wanted))}

    def _required_captures(self, spec: TaskSpec, plan_body: str) -> list[str]:
        """The same rule as _capture_evidence: non-glob image paths under
        docs/evidence/<task>/ in the file list, plus those the Acceptance lines name."""
        t = spec.task_id
        d = f"docs/evidence/{t}"
        out = [s for s in spec.scope if s.startswith(d + "/") and not _is_glob(s)
               and s.lower().endswith((".png", ".jpg", ".jpeg"))]
        for m in re.finditer(r"docs/evidence/%s/[\w./-]+\.(?:png|jpe?g)" % re.escape(t),
                             _accept_section(plan_body), re.I):
            if m.group(0) not in out:
                out.append(m.group(0))
        return out

    def required_checks(self, spec: TaskSpec, phase: str = "BUILD", ws=None) -> list[dict]:
        """[{check_id, chain_id, seq, prereqs, runtime, min_tier}] for `phase`,
        derived from the plan at the spec's revision (describe_at) and the
        acceptance lines; never from NEXT lines or summaries."""
        phase = (phase or "BUILD").upper()
        t = spec.task_id
        if phase == "BUILD" and self._fast_lane(t):
            # fast lane: the studio_gate verdict (one `fast_lane.gate` record bound to
            # the revision) is the BUILD evidence; the runner covers suites,
            # captures and boot itself
            # runtime False: the runner verifies the live Studio fingerprint itself (a mock
            # sprint has none) and the record never carries a ledger runtime identity
            return [{"check_id": "fast_lane.gate", "chain_id": None, "seq": None,
                     "prereqs": [], "runtime": False, "min_tier": "verified"}]
        if phase == "CLOSE":
            return [{"check_id": f"done_check:{t}", "chain_id": None, "seq": None,
                     "prereqs": [], "runtime": False, "min_tier": "verified"}]
        if phase != "BUILD":
            return []
        req = self._plan_requirements(spec, ws)
        out: list[dict] = []
        studio = [s for s in req["suites"]]
        boot = ["boot:server", "boot:client"] if (studio or req["captures"]) else []
        if boot:
            out.append({"check_id": "boot:server", "chain_id": "boot", "seq": 1, "prereqs": [],
                        "runtime": True, "min_tier": "verified"})
            out.append({"check_id": "boot:client", "chain_id": "boot", "seq": 2,
                        "prereqs": ["boot:server"], "runtime": True, "min_tier": "verified"})
        for s in studio:
            out.append({"check_id": f"suite:{s}", "chain_id": s, "seq": None,
                        "prereqs": list(boot), "runtime": True, "min_tier": "verified"})
        for c in req["captures"]:
            out.append({"check_id": f"capture:{c}", "chain_id": None, "seq": None,
                        "prereqs": list(boot), "runtime": True, "min_tier": "observed"})
        if req["gate"]:
            out.append({"check_id": f"gate:{t}", "chain_id": None, "seq": None,
                        "prereqs": [f"suite:{s}" for s in studio], "runtime": False,
                        "min_tier": "verified"})
        return out

    def check_dependencies(self, spec: TaskSpec, check_id: str) -> list[str] | None:
        """Path globs whose content a check's result depends on (resolved at a
        revision by the ledger). None = unknown (freshness 'unknown')."""
        kind, _, ref = (check_id or "").partition(":")
        code = ["src/**", "tests/**", "default.project.json"]
        if kind == "fast_lane.gate":
            return code                    # captures/evidence commits don't stale it
        if kind in ("suite", "regression", "boot", "call", "plan", "runtime"):
            return code
        if kind == "lune":
            return ["src/**", "tests/**"]
        if kind == "gate":
            return code + ["tools/gate.ps1"]
        if kind == "capture":
            src = [s for s in spec.scope if s.startswith("src/")]
            ui = [s for s in src if re.search(r"(?i)/ui/|ui[^/]*\.lua", s)]
            deps = ui or src
            if not deps:
                return None
            own = [s for s in spec.scope if s.startswith("tests/") and "_check." in s]
            return sorted(set(deps + own + ["default.project.json"]))
        if kind == "done_check":
            return None                    # reads the whole repo state: not path-bounded
        return None

    def obligations(self, spec: TaskSpec, ws=None) -> list[dict]:
        """BUILD: acceptance lines, required captures, suites, gate. CLOSE: the
        _close_duties (close.<id>). APPROVAL: recorded visual approval for
        visual sprints. Ids come from plan structure (paths, suite ids, duty
        ids, explicit acceptance labels); unlabeled acceptance lines use a hash
        of their text. A NEXT line never changes a phase."""
        t = spec.task_id
        req = self._plan_requirements(spec, ws)
        plan_rel = spec.data.get("plan") or f"docs/progress/{t}_PLAN.md"
        log_rel = f"docs/progress/{t}_EVIDENCE_RAW.log"
        out: list[dict] = []

        def ob(oid, phase, title, source, required_evidence, deps=(), acceptance="", **kw):
            d = {"obligation_id": oid, "phase": phase, "title": title, "source": source,
                 "deps": list(deps), "acceptance": acceptance,
                 "required_evidence": list(required_evidence)}
            d.update(kw)
            out.append(d)

        if self._fast_lane(t):
            # fast lane: ONE BUILD obligation, the studio gate verdict at the
            # reviewed code (the runner's own checks are its rows); CLOSE and the
            # visual approval below are unchanged
            ob("build.gate", "BUILD", f"GATE {t} PASS (studio_gate verdict) bound to the "
               f"reviewed code", f"plan:{plan_rel}",
               [{"kind": "check", "ref": "fast_lane.gate"}],
               acceptance=f"studio_gate sprint --sprint {t} verdict PASS at the revision")
            req = {**req, "suites": [], "captures": [], "acceptance": "", "gate": False}
        for s in req["suites"]:
            ob(f"build.suite.{s}", "BUILD", f"Suite {s} verdict (GateRunner.tally) at the "
               f"reviewed commit", f"plan:{plan_rel}", [{"kind": "check", "ref": f"suite:{s}"}],
               acceptance=f"SUITE {s} ... VERDICT PASS")
        for c in req["captures"]:
            ob(f"build.capture.{c}", "BUILD", f"Capture {c} from the runtime at the reviewed "
               f"code", f"plan:{plan_rel}",
               [{"kind": "check", "ref": f"capture:{c}"}, {"kind": "path", "ref": c}],
               acceptance=f"{c} committed, captured from a runtime bound to the revision")
        for line in _acceptance_items(req["acceptance"]):
            label = _accept_label(line)
            refs = [{"kind": "check", "ref": f"suite:{s}"} for s in req["suites"]
                    if re.search(r"\b%s\b" % re.escape(s), line)]
            refs += [{"kind": "path", "ref": p} for p in req["captures"] if p in line]
            deps = [f"build.capture.{p}" for p in req["captures"] if p in line]
            ob(f"build.accept.{label}", "BUILD", line[:200], f"plan:{plan_rel}#Acceptance",
               refs, deps=deps, acceptance=line[:600])
        if req["gate"]:
            ob("build.gate", "BUILD", f"GATE {t} PASS bound to the reviewed commit",
               f"plan:{plan_rel}", [{"kind": "check", "ref": f"gate:{t}"},
                                    {"kind": "log_marker", "ref": f"{log_rel}::GATE {t} PASS"}],
               deps=[o["obligation_id"] for o in out if o["obligation_id"].startswith(
                   ("build.suite.", "build.capture."))],
               acceptance=f"GATE {t} PASS with 'PASS: GATE HEAD <sha>' bound to the commit")
        evidence_for = {
            "summary": [{"kind": "path", "ref": f"docs/progress/{t}_SUMMARY.md"}],
            "state": [{"kind": "blob_changed", "ref": "docs/progress/STATE.md"}],
            "master_order": [],
            "plan_close_step": [{"kind": "blob_changed", "ref": plan_rel}],
            "done_check": [{"kind": "check", "ref": f"done_check:{t}"},
                           {"kind": "log_marker", "ref": f"{log_rel}::DONE-CHECK {t} PASS"}],
            "push": [],
        }
        build_ids = [o["obligation_id"] for o in out]
        for d in self._close_duties(spec):
            ob(f"close.{d['id']}", "CLOSE", d["duty"], f"adapter:close.{d['id']}",
               evidence_for.get(d["id"], []), deps=build_ids if d["id"] == "summary" else (),
               acceptance=d["step"] or d["duty"])
        if spec.data.get("visual"):
            model_approval = self._model_visual_approval()
            approver = "Coordinator vision reviewer" if model_approval else "Victor"
            ob("approval.visual", "APPROVAL", f"{approver}'s visual approval of the {t} "
               f"evidence images", "adapter:visual",
               [{"kind": "path", "ref": c} for c in req["captures"]],
               deps=[f"build.capture.{c}" for c in req["captures"]],
               acceptance=f"Final visual approval by {approver} on current committed images",
               status="pending_model" if model_approval else "pending_human")
        return out

    # -- live infrastructure before a session (W1.8; infra.preflight) ---------------------

    INFRA_CHECKS_DEFAULT = ("rojo",)

    def infra_checks(self) -> list[dict]:
        """[{name, ok, detail}] live checks infra.preflight runs before an
        implementer session (a failure starts nothing and costs nothing; the task
        waits with a backoff). `[animaluprising] infra_checks` lists them:
          rojo    Rojo's HTTP API on the project's servePort (default.project.json,
                  34872 by default): down = a Studio sync outage, not the code
          studio  the live Studio probe when one is configured (studio_probe); not
                  checkable offline, so ok=None (ignored) without one"""
        names = self.cfg.get("infra_checks")
        if names is None:
            names = list(self.INFRA_CHECKS_DEFAULT)
        out = []
        for name in names:
            name = str(name).strip().lower()
            if name == "rojo":
                out.append(self._rojo_check())
            elif name == "studio":
                out.append(self._studio_check())
        return out

    def _rojo_check(self) -> dict:
        import urllib.error
        import urllib.request
        from .studio_gate import rojo_port
        port = rojo_port(self.p.workspace)
        url = f"http://127.0.0.1:{port}/api/rojo"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                ok = int(getattr(r, "status", 200)) < 500
                return {"name": "rojo", "ok": ok, "detail": f"rojo serve on {port}" if ok
                        else f"{url}: HTTP {r.status}"}
        except urllib.error.HTTPError as e:
            return {"name": "rojo", "ok": e.code < 500, "detail": f"{url}: HTTP {e.code}"}
        except (OSError, ValueError) as e:
            return {"name": "rojo", "ok": False,
                    "detail": f"rojo serve not reachable on port {port}: {e}"[:300]}

    def _studio_check(self) -> dict:
        probe = self.studio_probe
        if not callable(probe):
            return {"name": "studio", "ok": None, "detail": "no live Studio probe configured"}
        try:
            live = probe(None) or {}
        except Exception as e:                                    # noqa: BLE001
            return {"name": "studio", "ok": False, "detail": f"Studio probe failed: {e}"[:300]}
        ok = live.get("connected")
        return {"name": "studio", "ok": None if ok is None else bool(ok),
                "detail": str(live.get("error") or ("Studio connected" if ok else
                                                    "Studio/Rojo not connected"))[:300]}

    # -- readiness (GAP_C design; read-only, offline-first) -------------------------------

    def readiness(self, spec: TaskSpec, phase: str = "implement", ctx: dict | None = None,
                  ws=None) -> list[dict]:
        """[{name, check, ok, cls, detail, blocking, required, remedy}] before a
        session. Classes: scope, tooling, environment, runtime_sync (never a
        product failure, never an attempt). ok=None means not checkable here."""
        ctx = ctx or {}
        ws = ws or self._ws()
        out: list[dict] = []

        def res(name, cls, ok, detail, blocking=True, remedy=""):
            out.append({"name": name, "check": name, "cls": cls, "ok": ok,
                        "detail": str(detail)[:600], "blocking": bool(blocking),
                        "required": bool(blocking), "remedy": remedy})

        t = spec.task_id
        rel = spec.data.get("plan") or f"docs/progress/{t}_PLAN.md"
        try:
            at_head = ws.show_at("HEAD", rel)
            head_err = None
        except Exception as e:                                    # noqa: BLE001 (not a repo)
            at_head, head_err = None, e
        if head_err is not None:
            res("au.plan_at_head", "scope", None, f"HEAD not readable: {head_err}",
                remedy="run in the AU workspace git repository")
        elif at_head is None:
            res("au.plan_at_head", "scope", False, f"{rel} is not committed at HEAD",
                remedy="commit the approved plan before the sprint starts")
        else:
            try:
                files = self._done_check_module().plan_files(at_head) if at_head else []
            except Exception as e:                                # noqa: BLE001 (tooling)
                files = None
                res("au.plan_at_head", "scope", None,
                    f"{rel} is committed at HEAD; its scope can't be parsed: {e}",
                    blocking=False)
                res("au.plan_scope_parser", "tooling", False,
                    f"done_check.plan_files unavailable: {e}",
                    remedy="restore tools/guard/done_check.py in the workspace")
            if files is not None:
                res("au.plan_at_head", "scope", bool(files),
                    f"{rel} committed at HEAD with {len(files)} scope entries" if files else
                    f"{rel} at HEAD has no 'Files this sprint may change' list")
        why = self.preflight_task(spec)
        res("au.rule_amendment_and_scope", "scope", why is None, why or "ok")
        if spec.deps:
            undone = [d for d in spec.deps if not self.dependency_done(d)]
            res("au.deps_ledger", "scope", True if not undone else None,
                "all dependencies DONE in STATE.md" if not undone else
                "not DONE in the STATE.md ledger (the coordinator queue decides): "
                + ", ".join(undone), blocking=False)
        for tool in ("tools/gate.ps1", "tests/GateRunner.lua", "tests/lune/run.luau",
                     "tools/guard/done_check.py"):
            ok = os.path.isfile(os.path.join(self.p.workspace, tool))
            res(f"au.tool.{tool}", "tooling", ok, "present" if ok else "missing",
                remedy="" if ok else f"restore {tool} in the workspace")
        try:
            self._done_check_module()
            res("au.done_check_importable", "tooling", True, "importable")
        except Exception as e:                                    # noqa: BLE001
            res("au.done_check_importable", "tooling", False, str(e))
        lune = self.cfg.get("lune") or shutil.which("lune")
        res("au.lune", "tooling", bool(lune), lune or "lune not on PATH",
            blocking=spec.data.get("tier") in ("A", "B"), remedy="" if lune else "rokit install")
        ok, detail = _kilo_mcp_studio(os.path.join(self.p.workspace, "kilo.jsonc"))
        res("au.mcp.roblox_studio_configured", "environment", ok, detail,
            remedy="" if ok else "add the Roblox Studio MCP server to kilo.jsonc")
        probe = ctx.get("studio_probe") or self.studio_probe
        if probe is None:
            res("au.studio.live_sync", "runtime_sync", None,
                "Studio/Rojo connection and the loaded-source hash cannot be checked offline; "
                "the gate's VERIFY/GATE HEAD lines prove it after the fact", blocking=False)
        else:
            try:
                live = probe(spec) or {}
            except Exception as e:                                # noqa: BLE001
                live = {"connected": None, "error": str(e)}
            if not live.get("connected"):
                res("au.studio.live_sync", "runtime_sync",
                    None if live.get("connected") is None else False,
                    live.get("error") or "Studio/Rojo not connected",
                    remedy="open the AU place in Studio and start `rojo serve`")
            else:
                try:
                    head = ws.head() if hasattr(ws, "head") else "HEAD"
                    m, why = self.runtime_matches({"files": live.get("files") or {}}, spec,
                                                  head, ws)
                except GitError as e:                     # a probe, but no repository here
                    m, why = None, f"HEAD not readable: {e}"
                res("au.studio.live_sync", "runtime_sync", m, why,
                    blocking=m is False)
        return out


def _view_input(view: dict) -> str:
    inp = view.get("input")
    if isinstance(inp, dict):
        for k in ("command", "cmd", "code", "script", "filePath", "path", "pattern"):
            if isinstance(inp.get(k), str):
                return inp[k]
        return json.dumps(inp, ensure_ascii=False)
    return str(inp or view.get("command") or "")


def _decode_output(out) -> tuple[list[str], bool]:
    """A tool output as physical lines in source order: nested JSON envelopes
    (returnValue | result | content | text | output | message, string -> JSON
    again, arrays element by element) are unwrapped; nothing is sorted."""
    lines: list[str] = []
    structured = False

    def walk(v, depth):
        nonlocal structured
        if depth > 6:
            lines.extend(_lines(str(v)))
            return
        if isinstance(v, str):
            s = v.strip()
            if s[:1] in "[{" and s[-1:] in "]}":
                try:
                    parsed = json.loads(s)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (list, dict)):
                    structured = True
                    walk(parsed, depth + 1)
                    return
            lines.extend(_lines(v))
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif isinstance(v, dict):
            for k in ("returnValue", "result", "content", "text", "output", "message", "logs"):
                if v.get(k) not in (None, "", [], {}):
                    walk(v[k], depth + 1)
                    return
        elif v is not None:
            lines.append(str(v))

    walk(out, 0)
    return lines, structured


def _regression_verdict(lines: list[str], suite: str) -> str:
    for l in lines:
        m = _GATE_REG_VERDICT.match(l.strip())
        if m and m.group(2) == suite:
            return {"PASS": PASS, "FAIL": FAIL}.get(m.group(1), "NA")
    return UNKNOWN


def _step_counts(lines: list[str]) -> tuple[int, int]:
    p = f = 0
    for l in lines:
        m = _STEP_LINE.match(l.strip())
        if m and not _GATE_SUITE.match(l.strip()) and not l.strip().startswith(
                ("PASS: GATE", "FAIL: GATE", "PASS: BOOT", "FAIL: BOOT")):
            if m.group(1) == "PASS":
                p += 1
            elif m.group(1) == "FAIL":
                f += 1
    return p, f


def _norm_code(code: str) -> str:
    return re.sub(r"\s+", " ", (code or "").replace('\\"', '"')).strip()


def _check_call(code: str):
    """(SUITE, function) when the call runs one function of a <SUITE>_check
    module, else None."""
    m = _CHECK_MODULE.search(code or "")
    if not m:
        return None
    suite = m.group(1) or m.group(2)
    var = re.search(r"local\s+(\w+)\s*=\s*require\([^\n]*?%s_check" % re.escape(suite), code)
    fn = None
    if var:
        f = re.search(r"\b%s\s*[.:]\s*(\w+)\s*\(" % re.escape(var.group(1)), code)
        fn = f.group(1) if f else None
    if not fn:
        f = re.search(r"%s_check\W*\)*\s*[.:]\s*(\w+)\s*\(" % re.escape(suite), code)
        fn = f.group(1) if f else None
    return (suite, fn) if fn else None


def _plan_suite(code: str, text: str) -> str | None:
    m = re.search(r"\.run\(\s*\\?[\"']([A-Za-z0-9_]+)\\?[\"']", code or "")
    if m:
        return m.group(1)
    m = re.search(r"^REGRESSION PLAN\s+(\S+)", text or "", re.M)
    return m.group(1) if m else None


def _view_vm(tool: str, code_or_input) -> str | None:
    if re.search(r"eval_server_runtime$", tool):
        return "server"
    if re.search(r"eval_client_runtime$", tool):
        return "client"
    if re.search(r"execute_luau$", tool):
        if isinstance(code_or_input, dict):
            return "edit" if code_or_input.get("target") == "edit" else None
        return "edit" if re.search(r"[\"']?target[\"']?\s*[:=]\s*[\"']edit",
                                   str(code_or_input or "")) else None
    return None


def _match_call(steps, code: str, tool: str):
    if not steps:
        return None
    """The plan step this call ran: an exact code match, else the step whose
    code is the longest prefix of the call's, else the one step whose code the
    (truncated) call is a prefix of. Ambiguous -> None (never the first hit)."""
    c = _norm_code(code)
    if not c:
        return None
    vm = _view_vm(tool, "")
    cand = [st for st in steps if st.get("code") and (vm is None or st.get("vm") in (None, vm))]
    exact = {st.get("seq") for st in cand if st["code"] == c}
    if exact:
        return exact.pop() if len(exact) == 1 else None
    longer = [st for st in cand if c.startswith(st["code"])]       # the call extends the step
    shorter = [st for st in cand if st["code"].startswith(c)]      # the call is a prefix of it
    if longer and shorter:
        return None
    if longer:
        best = max(len(st["code"]) for st in longer)
        seqs = {st.get("seq") for st in longer if len(st["code"]) == best}
        return seqs.pop() if len(seqs) == 1 else None
    seqs = {st.get("seq") for st in shorter}
    return seqs.pop() if len(seqs) == 1 else None


def _overlaps(view: dict, others: list) -> bool:
    s, e = view.get("started_ts"), view.get("ended_ts")
    if s is None or e is None:
        return False
    vm = _view_vm(str(view.get("tool") or ""), view.get("input"))
    for o in others:
        if o is view or (o.get("part_id") and o.get("part_id") == view.get("part_id")):
            continue
        os_, oe = o.get("started_ts"), o.get("ended_ts")
        if os_ is None or oe is None or not re.search(r"(?i)runtime|luau",
                                                     str(o.get("tool") or "")):
            continue
        ovm = _view_vm(str(o.get("tool") or ""), o.get("input"))
        if (vm is None or ovm is None or vm == ovm) and os_ < e and s < oe:
            return True
    return False


def _split_chain(cmd: str) -> list[tuple[str, str]]:
    """[(separator before, segment)] of a shell command line, split on && || ;
    | and newlines outside quotes."""
    out, buf, q, sep, i, n = [], [], None, "", 0, len(cmd)

    def flush(nsep):
        nonlocal buf, sep
        s = "".join(buf).strip()
        if s:
            out.append((sep, s))
        buf, sep = [], nsep

    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if c in "\\`" and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "\"'":
            q = c
            buf.append(c)
        elif cmd.startswith(("&&", "||"), i):
            flush(cmd[i:i + 2])
            i += 2
            continue
        elif c in ";|\n\r":
            flush(c if c != "\r" else "\n")
        else:
            buf.append(c)
        i += 1
    flush("")
    return out


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def _shell_segments(cmd: str, depth: int = 0) -> list[tuple[str, str]]:
    """The chain's commands with wrappers unwrapped: powershell/pwsh -Command
    and -File, cmd /c, sh/bash -c, '& { ... }' blocks and the & / . operators."""
    out: list[tuple[str, str]] = []
    for sep, seg in _split_chain(cmd or ""):
        s = re.sub(r"^(?:&|\.)\s+", "", seg.strip())
        b = re.match(r"^\{(.*)\}$", s, re.S)
        if b and depth < 6:
            out += _shell_segments(b.group(1), depth + 1)
            continue
        if depth < 6:
            m = re.match(r"(?i)^[\"']?(?:[\w:.\\/ -]*?[\\/])?(?:powershell|pwsh)(?:\.exe)?[\"']?"
                         r"(\s.*)?$", s, re.S)
            if m:
                rest = m.group(1) or ""
                c = re.search(r"(?i)(?:^|\s)-(c|command|f|file)\s+(.*)$", rest, re.S)
                if c and c.group(1).lower() in ("c", "command"):
                    out += _shell_segments(_unquote(c.group(2)), depth + 1)
                elif c:
                    out.append((sep, c.group(2).strip()))
                else:
                    pos = re.sub(r"(?i)^(?:\s*-(?:ExecutionPolicy|WindowStyle|WorkingDirectory"
                                 r"|OutputFormat|InputFormat|ConfigurationName|ep)\s+\S+"
                                 r"|\s*-\w+)*", "", rest).strip()
                    out += _shell_segments(pos, depth + 1) if pos else [(sep, s)]
                continue
            m = re.match(r"(?i)^(?:[\w:.\\/-]*[\\/])?cmd(?:\.exe)?\s+/[ck]\s+(.*)$", s, re.S)
            if m:
                out += _shell_segments(_unquote(m.group(1)), depth + 1)
                continue
            m = re.match(r"^(?:[\w./-]*/)?(?:ba|z|da)?sh\s+(?:-\w+\s+)*-c\s+(.*)$", s, re.S)
            if m:
                out += _shell_segments(_unquote(m.group(1)), depth + 1)
                continue
        out.append((sep, s))
    return out


def _shell_runs(cmd: str) -> set[str]:
    """What a shell command line actually ran: 'gate' (tools/gate.ps1), 'lune'
    (lune run), 'capture' (capture-window.ps1), plus 'read' when it also reads a
    file outside a pipe (cat/Get-Content/git show ... <path>: old output)."""
    runs: set[str] = set()
    for sep, s in _shell_segments(cmd):
        if _RUN_GATE.match(s):
            runs.add("gate")
        elif _RUN_LUNE.match(s):
            runs.add("lune")
        elif _RUN_CAPTURE.match(s):
            runs.add("capture")
        elif sep != "|" and _READ_CMD.match(s) and re.search(r"\s[^-\s][^\s]*[\\/.:]\S*", s):
            runs.add("read")
    return runs


def _read_only(tool: str, cmd: str) -> bool:
    """A read tool, or a shell command line that runs no gate / test runner /
    capture anywhere in its chain (cd X && cat log, git -C . show, python -c ...)."""
    t = (tool or "").lower()
    if t in _READ_TOOLS:
        return True
    if t not in _SHELL_TOOLS:
        return False
    return not (_shell_runs(cmd) & {"gate", "lune", "capture"})


def _source_exclusion(tool: str, cmd: str, kind: str) -> str | None:
    """Gate / suite / regression / lune / boot / runtime verdicts come only from
    a Studio/runtime MCP tool, or from a shell command line that actually ran
    tools/gate.ps1 (any verdict) or `lune run` (the lune tally only) and reads
    no file outside a pipe. Everything else is read_only_command (shell, read
    tools) or not_verdict_source (any other tool)."""
    if kind not in _VERDICT_KINDS:
        return None
    t = (tool or "").lower()
    if t in _READ_TOOLS:
        return "read_only_command"
    if t in _SHELL_TOOLS:
        runs = _shell_runs(cmd)
        if "read" in runs:
            return "read_only_command"
        if "gate" in runs or ("lune" in runs and kind == "lune"):
            return None
        return "read_only_command"
    if _STUDIO_TOOL.search(t):
        return None
    return "not_verdict_source"


def _built_in_input(line: str, inp: str) -> bool:
    """The verdict line is the input's own text: verbatim, or every verdict word
    of it (SUITE / GATE / VERDICT / PASS / pass= ...) sits in the input's string
    literals (built by concatenation or formatting)."""
    if not line or not inp:
        return False
    if line in inp:
        return True
    words = {w.group(0) for w in _VERDICT_WORDS.finditer(line)}
    if not words:
        return False
    lit = "\n".join(g for m in _LITERAL.finditer(inp) for g in (m.group(1), m.group(2),
                                                                 m.group(4)) if g)
    return bool(lit) and all(w in lit for w in words)


def _count_check(verdict: str, counts: dict | None) -> tuple[str, str]:
    """A PASS whose own counts contradict it is UNKNOWN: fail > 0, or pass !=
    expect where expect is known. FAIL stays FAIL."""
    if verdict != PASS or not counts:
        return verdict, ""
    p, f, e = counts.get("pass"), counts.get("fail"), counts.get("expect")
    if f:
        return UNKNOWN, f"verdict PASS with fail={f}"
    if e is not None and p is not None and p != e:
        return UNKNOWN, f"verdict PASS with pass={p} != expect={e}"
    return verdict, ""


def _manifest_fp(files: dict) -> str:
    import hashlib
    body = "\n".join(f"{p}={str(h).split('/')[0]}" for p, h in sorted(files.items()))
    return "fnv1a32-manifest:" + hashlib.sha256(body.encode()).hexdigest()[:24]


def _fnv1a32(data: bytes) -> int:
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _blob_bytes(ws, revision: str, rel: str) -> bytes | None:
    r = subprocess.run(["git", "-C", ws.path, "cat-file", "blob", f"{revision}:{rel}"],
                       capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 else None


def _sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)] of '## ' sections (deeper headings stay in their body)."""
    out, name, buf = [], "", []
    for l in _lines(text):
        m = re.match(r"^##\s+(.+?)\s*$", l)
        if m:
            out.append((name, "\n".join(buf)))
            name, buf = m.group(1), []
        else:
            buf.append(l)
    out.append((name, "\n".join(buf)))
    return [(k, v) for k, v in out if k]


def _acceptance_items(text: str) -> list[str]:
    """Acceptance lines as items: bullets / numbered lines when the section
    has them (continuations folded in), else one item per non-empty line."""
    lines = [l for l in _lines(text) if l.strip()]
    if any(re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", l) for l in lines):
        return duty_bullets(text)
    return [l.strip() for l in lines]


def _accept_label(line: str) -> str:
    m = re.match(r"^\W*([A-Za-z]{0,3}\d+[a-z]?)\s*[:.)\]—-]\s", line)
    if m:
        return m.group(1)
    import hashlib
    return "h" + hashlib.sha1(re.sub(r"\s+", " ", line.strip().lower()).encode()).hexdigest()[:10]


def _strip_jsonc(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _kilo_mcp_studio(path: str) -> tuple:
    """(ok, detail): kilo.jsonc lists an enabled Roblox Studio MCP server."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return False, "kilo.jsonc missing"
    try:
        cfg = json.loads(_strip_jsonc(raw))
    except ValueError as e:
        return False, f"kilo.jsonc does not parse: {e}"
    mcp = cfg.get("mcp") if isinstance(cfg, dict) else None
    if not isinstance(mcp, dict) or not mcp:
        return False, "kilo.jsonc has no 'mcp' servers"
    names = [k for k in mcp if re.search(r"(?i)roblox[\s_-]*studio", k)]
    if not names:
        return False, "no Roblox Studio server in kilo.jsonc mcp: " + ", ".join(sorted(mcp))[:300]
    on = [k for k in names if not (isinstance(mcp[k], dict) and mcp[k].get("enabled") is False)]
    if not on:
        return False, "Roblox Studio MCP configured but disabled: " + ", ".join(names)
    return True, "configured: " + ", ".join(on)


_SPRINT_ID = re.compile(r"^[A-Z][A-Za-z0-9.-]{1,30}$")
_SESSION = re.compile(r"^\W*SESSION\s+\d+")
_TICK = re.compile(r"^\s*-\s\[[ xX~]\]")          # [~] = in progress: open, not done
_CHECK_PREFIX = re.compile(r"^\s*-\s\[[ xX~]\]\s*")
_SUITE = re.compile(r"\bS\d{1,3}[a-z]?\b.*\b(?:PASS|FAIL|PASSED|FAILED)\b"
                    r"|\b(?:PASS|FAIL)\b.*\bS\d{1,3}[a-z]?\b")
_TALLY = re.compile(r"(?i)\b\d+\s+(?:passed|failed|errors?)\b|\b\d+\s*/\s*\d+\s+(?:PASS|FAIL)"
                    r"|\b(?:passed|failed)\s*[:=]\s*\d+|\bLUNE\s+\d+/\d+|^PASS: GATE HEAD\b")
_BASELINE = re.compile(r"(?i)\b(?:source|baseline)\b[^\n]{0,40}?\b(?=[0-9a-f]*\d)([0-9a-f]{7,40})\b")

# evidence contract: explicit verdict / tally lines printed by gate.ps1 and GateRunner
_SUITE_VERDICT = re.compile(r"^SUITE\s+(\S+)\s+pass=(\d+)\s+fail=(\d+)(?:\s+expect=(\d+))?"
                            r"(?:\s+designedFails=(\d+))?\b.*?\bVERDICT\s+(PASS|FAIL)\b")
_GATE_SUITE = re.compile(r"^(PASS|FAIL|SKIP):\s+GATE suite\s+(\S+)")
_GATE_REGRESSION = re.compile(r"^GATE regression\s+(\S+)\b.*?\bpass=(\d+)\s+skip=(\d+)\s+"
                              r"fail=(\d+)")
_GATE_REG_VERDICT = re.compile(r"^(PASS|FAIL|SKIP):\s+GATE regression\s+(\S+)")
_SHORT_TALLY = re.compile(r"^(S\d{1,3}[a-z]?|[A-Z]{1,5}_[A-Za-z0-9]+)\s+(PASS|FAIL)\s+(\d+)/(\d+)"
                          r"\s*$")
_LUNE = re.compile(r"^LUNE\s+(\d+)/(\d+)\s+(PASS|FAIL)\b")
_GATE_VERDICT = re.compile(r"^GATE\s+(\S+)\s+(PASS|FAIL)\b")
_GATE_WORDS = {"regression", "suite", "HEAD", "RUN", "FINISH", "START"}
_GATE_HEAD = re.compile(r"^PASS: GATE HEAD ([0-9a-f]{7,40})")
_BOOT = re.compile(r"^BOOT\((server|client)\)\s+loaded=(\d+)\s+errors=(\d+)")
_BOOT_VERDICT = re.compile(r"^(PASS|FAIL):\s+BOOT\b.*?\((server|client)\b")
_VERIFY_LIVE = re.compile(r"^VERIFY\s+matched=(\d+)/(\d+)\s+mismatched=(\d+)\s+unresolved=(\d+)"
                          r"(?:\s+skipped=(\d+))?")
_LIVE_FILE = re.compile(r"^(MATCH|MISMATCH|UNRESOLVED)\s+(\S+)\s+live=(\S+)(?:\s+head=(\S+))?")
_STEP_LINE = re.compile(r"^(PASS|FAIL|SKIP):\s")
_TAGGED_STEP = re.compile(r"^(?:PASS|FAIL):\s+(S\d{1,3}[a-z]?|[A-Z]{1,5}_[A-Za-z0-9]+)\b")
_CALL_PLAN = re.compile(r"^CALL\s+(\d+)\s+\[vm=(\w+)\]\s+(\S+)\s+(\S+)\s+::\s+(.*)$")
_CHECK_MODULE = re.compile(r"WaitForChild\(\s*[\"']([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_check[\"']"
                           r"|\b([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_check\b")
_PLACE = re.compile(r"placeId:\s*(\d+)")
_VOID_TEXT = re.compile(r"(?i)teardown did not (?:complete|settle)|no player in world"
                        r"|DataModel (?:was )?(?:reset|closed)|playtest (?:is )?not running"
                        r"|\bVM (?:was )?(?:restarted|reset)\b|bridge (?:not connected|disconnected)")
_READ_CMD = re.compile(r"(?i)^(?:&\s*)?(?:cat|type|more|less|head|tail|grep|egrep|rg|findstr"
                       r"|Get-Content|gc|Select-String|sls|sed\s+-n|awk|wc|echo|printf"
                       r"|Write-Output|Write-Host|git\s+(?:show|log|diff|grep|blame))\b")
# a NEXT: stop note, any case, plain / bulleted / numbered / checkbox / bold; group 1 = indent
_NEXT = re.compile(r"(?i)^([ \t]*)(?:(?:[-*+]|\d+[.)])\s+)?(?:\[[ xX~]\]\s*)?[*_]*NEXT[*_]*\s*:")
_REQ_SECTION = re.compile(r"(?i)acceptance|checklist|regress|test|gate|verif|suite")
_SUITE_ID = re.compile(r"\b(S\d{1,3}[a-z]?|[A-Z]{1,5}_[A-Z]?\d+[A-Za-z0-9]*)\b")
# a suite id in prose is required only next to a run / verdict verb ...
_SUITE_VERB = re.compile(r"(?i)\b(?:re-?run|runs?|ran|execut(?:e|es|ed|ing)|pass(?:es|ed|ing)?"
                         r"|green|verdict|tally|gate)\b")
# ... and never right after a contrast / exclusion word ("unlike S9", "except S12")
_SUITE_NEG = re.compile(r"(?i)\b(?:unlike|except|excluding|without|not|no|skip(?:ping|s)?"
                        r"|besides|other\s+than|instead\s+of|vs\.?|versus)\s+(?:the\s+)?"
                        r"(?:suites?\s+)?[`*_]*$")
_ACCEPT_HEADING = re.compile(r"(?i)^acceptance(?:\s+(?:lines|criteria))?$")
# shell tools: a gate / suite / boot verdict is credited only when the command ran a runner
_SHELL_TOOLS = ("bash", "shell", "powershell", "pwsh", "cmd", "sh")
_READ_TOOLS = ("read", "grep", "glob", "list", "ls", "view", "webfetch", "codesearch")
_STUDIO_TOOL = re.compile(r"(?i)studio|roblox|luau|runtime|playtest")
_VERDICT_KINDS = ("suite", "gate", "regression", "lune", "boot", "runtime")
_RUN_GATE = re.compile(r"(?i)^[\"']?(?:\.?[\\/])?(?:[\w:.\\/ -]*?[\\/])?tools[\\/]gate\.ps1[\"']?"
                       r"(?:\s|$)")
_RUN_LUNE = re.compile(r"(?i)^[\"']?(?:[\w:.\\/ -]*?[\\/])?lune(?:\.exe)?[\"']?\s+run\b")
_RUN_CAPTURE = re.compile(r"(?i)^[\"']?(?:\.?[\\/])?(?:[\w:.\\/ -]*?[\\/])?capture-window\.ps1"
                          r"[\"']?(?:\s|$)")
# the words a verdict line is made of: all of them in the input's string literals = typed in
_VERDICT_WORDS = re.compile(r"\b(?:SUITE|GATE|LUNE|BOOT|VERIFY|VERDICT|PASS|FAIL)\b"
                            r"|\b(?:pass|fail|expect|loaded|errors|matched|mismatched"
                            r"|unresolved)=")
_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|\[(=*)\[(.*?)\]\3\]',
                      re.S)


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
    return _CHECK_PREFIX.sub("", main[-1][1]).strip()[:160] if main else ""


def _section(text: str, name: str) -> str:
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(name), text, re.M | re.S)
    return m.group(1).strip()[:3000] if m else ""


def _accept_section(text: str) -> str:
    """The acceptance section: '## Acceptance lines', '## Acceptance criteria' or
    '## Acceptance' (exact heading, any case); the first one present."""
    m = re.search(r"^##[ \t]+acceptance(?:[ \t]+(?:lines|criteria))?[ \t]*$(.*?)(?=^## |\Z)",
                  text or "", re.M | re.S | re.I)
    return m.group(1).strip()[:3000] if m else ""


def _drop_next(text: str) -> str:
    """The text without its NEXT blocks: each NEXT: line (any case; plain,
    bulleted or numbered) and every following line indented deeper than it
    (blank lines inside the block included)."""
    out, depth = [], None
    for l in _lines(text):
        if depth is not None:
            if not l.strip() or len(l.expandtabs(4)) - len(l.expandtabs(4).lstrip()) > depth:
                continue
            depth = None
        m = _NEXT.match(l)
        if m:
            depth = len(m.group(1).expandtabs(4))
            continue
        out.append(l)
    return "\n".join(out)


def _required_suites(text: str) -> list[str]:
    """Suite ids a requirement section requires. Rule: an id counts when it is
    structural -- the leading id(s) of a bullet / numbered item, or a whole cell
    of a table row -- or when its sentence has a run / verdict verb (run, rerun,
    execute, pass, green, verdict, tally, gate) and the id isn't right after a
    contrast word (unlike, except, without, not, skip, ...). A bare mention in
    prose ("the layout differs, unlike S9") is not a requirement."""
    out: list[str] = []

    def add(sid):
        if sid not in out:
            out.append(sid)

    ids = r"S\d{1,3}[a-z]?|[A-Z]{1,5}_[A-Z]?\d+[A-Za-z0-9]*"          # as _SUITE_ID
    lead = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX~]\]\s*)?((?:[`*_]*(?:%s)(?![\w])[`*_]*"
                      r"(?:\s*(?:,|/|\+|&|\band\b)\s*)?)+)" % ids)
    for line in _lines(text):
        m = lead.match(line)
        if m:
            for s in _SUITE_ID.finditer(m.group(1)):
                add(s.group(1))
        if line.lstrip().startswith("|"):
            for cell in line.strip().strip("|").split("|"):
                c = cell.strip().strip("`*_ ")
                if _SUITE_ID.fullmatch(c):
                    add(c)
        for sentence in re.split(r"(?<=[.;!?])\s+", line):
            if not _SUITE_VERB.search(sentence):
                continue
            for s in _SUITE_ID.finditer(sentence):
                if not _SUITE_NEG.search(sentence[:s.start()]):
                    add(s.group(1))
    return out


def _title(plan: str, task_id: str) -> str:
    first = plan.splitlines()[0] if plan else ""
    m = re.match(r"#\s*%s\s*[—-]\s*(.+?)(?:\s*[—-]\s*PLAN)?\s*$" % re.escape(task_id), first)
    return (m.group(1) if m else first.lstrip("# "))[:120]
