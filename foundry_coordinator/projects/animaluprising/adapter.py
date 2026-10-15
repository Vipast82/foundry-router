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

from ...adapters.base import (FAIL, PASS, UNKNOWN, TaskSpec, VerificationResult,
                              evidence_dir, report_instructions)
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
        if not re.match(r"^[A-Z][A-Za-z0-9.-]{1,30}$", task_id):
            raise AdapterError(f"{task_id!r} is not a sprint id")
        rel = f"docs/progress/{task_id}_PLAN.md"
        if not os.path.isfile(os.path.join(self.p.workspace, rel)):
            raise AdapterError(f"{rel} does not exist; the Sprint Manager writes the plan first "
                               f"(or: py -3 tools\\guard\\plan_new.py {task_id})")
        plan = self._read(rel)
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
                        data={"tier": tier, "visual": visual, "plan": rel})

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
        return f"""BUILD session for sprint {spec.task_id} — {spec.title} (coordinator run {ctx.get('run_id')}, attempt {ctx.get('attempt')}).

{self.rules_text(spec)}
Read docs/progress/STATE.md header + last 5 ledger rows, then docs/progress/{spec.task_id}_PLAN.md, and resume from its checklist and NEXT line (§34 "Plan = memory"). Do not redo ticked steps unless their evidence is missing.

{spec.spec}

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

    def closeout_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        status = "COMPLETE-PENDING-VISUAL-REVIEW" if spec.data.get("visual") else "DONE"
        retry = (f"\nAn earlier CLOSE attempt was incomplete; the coordinator saw: "
                 f"{ctx['retry_reason']}\n") if ctx.get("retry_reason") else ""
        return f"""CLOSE session for sprint {spec.task_id} — {spec.title}.
{self.rules_text(spec)}
The code is committed, the gate passed and a frontier review approved commit {ctx.get('base', '')[:12]}. Do only closeout (§9, §31, §34 CLOSE):
1. docs/progress/{spec.task_id}_SUMMARY.md from docs/progress/_SUMMARY_TEMPLATE.md, written FROM THE SHIPPED CODE; status word {status} (R-A).
2. The STATE.md ledger row (<= 600 characters, measure it), API-surface lines, docs/progress/state/* rows the plan names, the MASTER_ORDER status line if the plan lists it. Edit, never rewrite, state files.
3. Tick the plan's last step with its evidence line.
4. py -3 tools\\guard\\done_check.py {spec.task_id} until it prints DONE-CHECK {spec.task_id} PASS; paste its output into the evidence log.
5. Commit only those closeout files by explicit path, message "{spec.task_id}: closeout"; then one plain `git push origin master` and confirm `git ls-remote origin master` equals HEAD. No tags (the Sprint Manager issues tags after DONE / Victor's approval).
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
        verdicts = list(re.finditer(r"^GATE %s (PASS|FAIL)\b" % re.escape(spec.task_id), ev, re.M))
        if not verdicts:
            return VerificationResult("au.gate_bound_to_head", FAIL, True, rel,
                                      f"no 'GATE {spec.task_id} PASS|FAIL' line", revision)
        last = verdicts[-1]
        heads = re.findall(r"^PASS: GATE HEAD ([0-9a-f]{7,40})", ev[:last.start()], re.M)
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


def _section(text: str, name: str) -> str:
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(name), text, re.M | re.S)
    return m.group(1).strip()[:3000] if m else ""


def _title(plan: str, task_id: str) -> str:
    first = plan.splitlines()[0] if plan else ""
    m = re.match(r"#\s*%s\s*[—-]\s*(.+?)(?:\s*[—-]\s*PLAN)?\s*$" % re.escape(task_id), first)
    return (m.group(1) if m else first.lstrip("# "))[:120]
