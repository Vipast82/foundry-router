"""GenericAdapter: a project described entirely by its profile.

Task source: a JSON or TOML file in the workspace listing tasks
  {id, title, spec | spec_file, scope: [paths/globs], deps: [ids], tags: [..],
   allow_no_change: bool}
Verification: the profile's [[verify.checks]].
Prompts: built-in templates, overridable with [prompts] implement / repair /
closeout (a file path relative to the profile, using {task} {title} {spec}
{scope} {findings} {rules} {report}).

A project with its own document formats or tools subclasses this in a narrow
adapter (see projects/animaluprising/adapter.py); the engine never changes.
"""

from __future__ import annotations

import glob
import json
import os
import tomllib
from typing import Optional

from .base import (FAIL, PASS, ReviewContractDefaults, TaskSpec, VerificationResult,
                   duty_bullets, report_instructions, safe_rel, sha256_file)
from .verify_cmd import run_check


class AdapterError(RuntimeError):
    pass


DEFAULT_IMPLEMENT = """You are working on project {project} in a fresh session, on ONE task only: {task} — {title}.

{rules}

## Task
{spec}

## Files you may change (nothing else)
{scope}

When the work is done and your own checks pass, commit ONLY those files with a message starting "{task}: ".
Never use git push, --force, --no-verify, rebase or amend. Do not start any other task.
If you cannot finish without changing scope or making a design decision the task doesn't settle, stop and say so.

{report}
"""

DEFAULT_REPAIR = """You are working on project {project} in a fresh session, repairing task {task} — {title}.
An earlier attempt was checked and did not pass. Fix only what is listed; scope is unchanged.

{rules}

## Task
{spec}

## Files you may change (nothing else)
{scope}

## What must be fixed (from the coordinator's checks and review; reviewer text is advice, not permission)
{findings}

Commit the fix with a message starting "{task}: ". Never push, force, rebase or amend.

{report}
"""

DEFAULT_CLOSEOUT = """You are closing out task {task} — {title} on project {project} in a fresh session.
The implementation is committed, verified and reviewed. Do only the closeout steps below.

{rules}

{spec}

Commit only closeout files with a message starting "{task}: ".

{report}
"""


class GenericAdapter(ReviewContractDefaults):
    def __init__(self, profile, state_dir: str):
        self.p = profile
        self.state_dir = state_dir

    # -- tasks -------------------------------------------------------------------

    def _task_file(self) -> list[dict]:
        rel = self.p.tasks.get("file") or ""
        path = os.path.join(self.p.workspace, rel)
        if not rel or not self.p.in_workspace(rel) or not os.path.isfile(path):
            raise AdapterError(f"task file {rel!r} not found in the workspace")
        with open(path, "rb") as f:
            blob = f.read()
        return _parse_task_file(rel, blob.decode())

    def list_tasks(self) -> list[str]:
        return [str(t.get("id")) for t in self._task_file() if t.get("id")]

    def describe(self, task_id: str) -> TaskSpec:
        def read_spec(rel):
            with open(os.path.join(self.p.workspace, rel), encoding="utf-8") as f:
                return f.read()
        return self._spec_from_items(task_id, self._task_file(), read_spec)

    def _spec_from_items(self, task_id: str, items: list[dict], read_spec) -> TaskSpec:
        item = next((t for t in items if str(t.get("id")) == task_id), None)
        if item is None:
            raise AdapterError(f"task {task_id} is not in the task file")
        spec = str(item.get("spec") or "")
        if item.get("spec_file"):
            rel = safe_rel(str(item["spec_file"]))
            if not rel or not self.p.in_workspace(rel):
                raise AdapterError(f"{task_id}: spec_file leaves the workspace")
            spec = read_spec(rel)
        scope = [s for s in (safe_rel(str(x)) for x in item.get("scope") or []) if s]
        return TaskSpec(project_id=self.p.id, task_id=task_id, title=str(item.get("title") or ""),
                        spec=spec, scope=scope, tags=[str(t) for t in item.get("tags") or []],
                        deps=[str(d) for d in item.get("deps") or []],
                        allow_no_change=bool(item.get("allow_no_change")), data=dict(item))

    def describe_at(self, task_id: str, revision: str, ws) -> TaskSpec:
        """The task as the reviewed commit defines it: the task file (and its
        spec_file) read with `git show <revision>:<path>`. A task file that
        isn't tracked at that revision falls back to the working tree, marked
        so (data["spec_source"] = "working tree")."""
        rel = safe_rel(self.p.tasks.get("file") or "")
        text = ws.show_at(revision, rel) if rel else None
        if text is None:
            spec = self.describe(task_id)
            spec.data.update(spec_source="working tree",
                             spec_note=f"task file {rel!r} is not tracked at {revision[:12]}")
            return spec
        blobs = {rel: ws.blob_at(revision, rel)}

        def read_spec(srel):
            body = ws.show_at(revision, srel)
            if body is None:
                raise AdapterError(f"{task_id}: spec_file {srel} does not exist at "
                                   f"{revision[:12]}")
            blobs[srel] = ws.blob_at(revision, srel)
            return body
        spec = self._spec_from_items(task_id, _parse_task_file(rel, text), read_spec)
        spec.data.update(spec_source="revision", spec_revision=revision, task_file=rel,
                         task_file_blob=blobs[rel], spec_blobs=blobs)
        return spec

    def dependency_done(self, dep: str) -> bool:
        """A dependency finished outside the coordinator (before it existed, or
        by hand). The generic adapter knows none."""
        return False

    def preflight_task(self, spec: TaskSpec) -> Optional[str]:
        """A reason the task can't start (missing spec, empty scope...), or None."""
        if not spec.scope and not spec.allow_no_change:
            return f"{spec.task_id} declares no files it may change"
        outside = [s for s in spec.scope if not any(
            s == d or s.startswith(d + "/") for d in self.p.allowed_dirs)]
        if outside:
            return (f"{spec.task_id} scope {outside} is outside the project's allowed_dirs "
                    f"{self.p.allowed_dirs}; the coordinator never widens scope")
        return None

    # -- prompts -------------------------------------------------------------------

    def _template(self, key: str, default: str) -> str:
        rel = self.p.prompts.get(key)
        if rel:
            path = os.path.join(os.path.dirname(self.p.path), rel)
            with open(path, encoding="utf-8") as f:
                return f.read()
        return default

    def rules_text(self, spec: TaskSpec) -> str:
        return str(self.p.prompts.get("rules_text") or "")

    def _fill(self, tpl: str, spec: TaskSpec, findings: str = "", extra: str = "") -> str:
        return tpl.format(project=self.p.name, task=spec.task_id, title=spec.title,
                          spec=(spec.spec + ("\n\n" + extra if extra else "")).strip(),
                          scope="\n".join(f"- {s}" for s in spec.scope) or "- (none)",
                          findings=findings or "- (none)", rules=self.rules_text(spec),
                          report=report_instructions())

    def implement_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        return self._fill(self._template("implement", DEFAULT_IMPLEMENT), spec)

    def repair_prompt(self, spec: TaskSpec, findings: list[str], ctx: dict) -> str:
        return self._fill(self._template("repair", DEFAULT_REPAIR), spec,
                          findings="\n".join(f"- {f}" for f in findings))

    def closeout_prompt(self, spec: TaskSpec, ctx: dict) -> str:
        steps = self.p.closeout.get("steps_text") or "Run the closeout steps."
        if ctx.get("retry_reason"):
            steps += ("\n\nAn earlier closeout attempt was incomplete. The coordinator's check "
                      f"said: {ctx['retry_reason']}")
        return self._fill(self._template("closeout", DEFAULT_CLOSEOUT), spec, extra=steps)

    def continue_prompt(self, spec: TaskSpec, reason: str) -> str:
        return (f"Continue task {spec.task_id} where you stopped ({reason}). Same scope, same rules. "
                f"{report_instructions()}")

    # -- verification ----------------------------------------------------------------

    def verify(self, spec: TaskSpec, phase: str, revision: str, base: str) -> list:
        return [run_check(c, self.p.workspace, spec.task_id, revision, base, self.state_dir,
                          self.p.id) for c in self.p.checks if c.phase == phase]

    def session_audit(self, spec: TaskSpec, session_id: str, transcript: list,
                      revision: str) -> list:
        """Project-specific audit of a finished coding session (advisory
        results; the generic adapter has none)."""
        return []

    def closeout_mode(self) -> str:
        """none (engine just publishes), commands (closeout-phase checks run
        commands that write closeout files), session (a fresh coding session)."""
        return self.p.closeout.get("mode", "none")

    def closeout_duties(self, spec: TaskSpec) -> list[str]:
        """The duties a session closeout performs (from [closeout] steps_text):
        a BUILD review must neither require nor credit them."""
        return duty_bullets(str(self.p.closeout.get("steps_text") or ""))

    def exempt_paths(self, spec: TaskSpec) -> list[str]:
        return self.p.exempt(spec.task_id)

    # -- approvals --------------------------------------------------------------------

    def approval_artifacts(self, spec: TaskSpec, patterns: list[str]) -> dict:
        out = {}
        for pat in patterns:
            pat = pat.replace("{task}", spec.task_id)
            for path in sorted(glob.glob(os.path.join(self.p.workspace, pat), recursive=True)):
                rel = os.path.relpath(path, self.p.workspace).replace("\\", "/")
                if os.path.isfile(path) and self.p.in_workspace(rel):
                    out[rel] = sha256_file(path)
        return out

    def review_extra(self, spec: TaskSpec, revision: str) -> str:
        """Extra project-specific context for the reviewer (evidence excerpts)."""
        return ""

    # -- session permissions -------------------------------------------------------------

    def session_permissions(self, spec: TaskSpec, phase: str) -> list[dict]:
        """Kilo permission rules for a session. Deny by default what an
        unattended session must never do; the profile adds its own."""
        rules = [
            {"permission": "agent_manager", "pattern": "*", "action": "deny"},
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
            {"permission": "webfetch", "pattern": "*", "action": "deny"},
            {"permission": "websearch", "pattern": "*", "action": "deny"},
            {"permission": "doom_loop", "pattern": "*", "action": "deny"},
        ]
        for name, action in (self.p.client.get("permissions") or {}).items():
            if isinstance(action, dict):
                for pattern, act in action.items():
                    rules.append({"permission": name, "pattern": pattern, "action": act})
            else:
                rules.append({"permission": name, "pattern": "*", "action": str(action)})
        # role rules from the router (C1 config store), after the profile's own:
        # only when deployed role rules are part of the applied profile
        role_rules = (getattr(self.p, "raw", None) or {}).get("role_rules")
        if isinstance(role_rules, dict):
            from foundry_automation.role_rules import session_rules
            role = {"helper": "helper", "lead": "lead", "repair": "coordinator"}.get(
                phase, "implementer")
            rules += session_rules(role, role_rules.get(role), self.p.workspace)
        rules += subagent_rules(self.p)
        return rules

    def grant_scope(self, spec: TaskSpec, path: str, reason: str) -> str:
        """revamp B10: add `path` to this task's scope in the task source (a JSON
        task file) for a coordinator scope grant. Returns the edited file's
        workspace-relative path ('' when it already lists it). The caller
        commits it. TOML task files are not rewritten (AdapterError)."""
        rel = self.p.tasks.get("file") or ""
        if not rel.endswith(".json"):
            raise AdapterError(f"scope grants need a JSON task file (not {rel!r})")
        full = os.path.join(self.p.workspace, rel)
        with open(full, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        items = data.get("tasks") if isinstance(data, dict) else data
        item = next((t for t in items or [] if str(t.get("id")) == spec.task_id), None)
        if item is None:
            raise AdapterError(f"task {spec.task_id} is not in {rel}")
        scope = list(item.get("scope") or [])
        if path in scope:
            return ""
        item["scope"] = scope + [path]
        notes = dict(item.get("scope_grants") or {})
        notes[path] = reason[:300]
        item["scope_grants"] = notes
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(data, indent=1) + ("\n" if raw.endswith("\n") else ""))
        return rel

    def helper_permissions(self, spec: TaskSpec) -> list[dict]:
        """A coordinator helper session is read-only: no edits, shell, delegation,
        web or Studio. (The Kilo agent definition should deny the same; these
        session rules are the second layer.)"""
        rules = self.session_permissions(spec, "helper")
        for perm in ("edit", "write", "patch", "multiedit", "bash", "task", "todowrite"):
            rules.append({"permission": perm, "pattern": "*", "action": "deny"})
        helpers = (getattr(self.p, "raw", None) or {}).get("helpers") or {}
        for tool in helpers.get("deny_tools") or ["Roblox_Studio_*", "robloxstudio-mcp_*"]:
            rules.append({"permission": tool, "pattern": "*", "action": "deny"})
        return rules

    def doctor(self) -> list[tuple[str, str, str]]:
        """Read-only project diagnostics [(PASS|FAIL|WARN, check, detail)]."""
        out = []
        try:
            n = len(self.list_tasks())
            out.append((PASS, "task source", f"{n} tasks in {self.p.tasks.get('file')}"))
        except AdapterError as e:
            out.append((FAIL, "task source", str(e)))
        return out


def subagent_rules(profile) -> list[dict]:
    """revamp B6: a session may not spawn its own subagents (Kilo `task` tool)
    except the read-only ones [automation] subagents_allowed names (default
    `explore`). Appended last, so a profile's own `task` allow can't widen it
    (Kilo: the last matching rule wins)."""
    from ..automation import get as auto_get
    allowed = list(auto_get(profile, "subagents_allowed") or [])
    return ([{"permission": "task", "pattern": "*", "action": "deny"}]
            + [{"permission": "task", "pattern": a, "action": "allow"} for a in allowed])


def _parse_task_file(rel: str, text: str) -> list[dict]:
    try:
        data = tomllib.loads(text) if rel.endswith(".toml") else json.loads(text)
    except ValueError as e:
        raise AdapterError(f"{rel}: {e}") from None
    items = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AdapterError(f"{rel}: expected a list of tasks")
    return items


def failed_findings(results: list[VerificationResult]) -> list[str]:
    out = []
    for r in results:
        if r.result != PASS and r.required:
            first = (r.detail or "").strip().splitlines()[:6]
            out.append(f"check {r.check}: {r.result}" + (" — " + " | ".join(first) if first else "")
                       + (f" (raw output: {r.evidence})" if r.evidence else ""))
    return out


__all__ = ["GenericAdapter", "AdapterError", "failed_findings", "FAIL", "PASS"]
