"""Generic verification: run a profile's [[verify.checks]] commands.

A check PASSes when its command exits 0, its output matches pass_regex (if
given), nothing matches fail_regex, and the evidence file it must leave (if
given) exists. A missing executable or a timeout is UNKNOWN, never PASS.
Raw output is saved under the coordinator's evidence folder for that project,
task and revision, and that path is the check's evidence pointer.

Placeholders in a command: {task} {sha} {base} {workspace} {python}.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from .base import FAIL, PASS, UNKNOWN, VerificationResult, evidence_dir


def run_check(check, workspace: str, task_id: str, sha: str, base: str,
              state_dir: str, project_id: str) -> VerificationResult:
    subs = {"task": task_id, "sha": sha, "base": base or "", "workspace": workspace,
            "python": sys.executable}
    cmd = [c.format(**subs) for c in check.command]
    out_dir = evidence_dir(state_dir, project_id, task_id, sha)
    log = os.path.join(out_dir, re.sub(r"[^\w.-]", "_", check.name) + ".log")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True,
                           timeout=check.timeout_seconds, encoding="utf-8", errors="replace",
                           env=dict(os.environ, FOUNDRY_COORD_TASK=task_id, FOUNDRY_COORD_SHA=sha))
        output = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        code = r.returncode
    except FileNotFoundError as e:
        return _res(check, UNKNOWN, log, f"command not found: {e}", sha, t0, "")
    except subprocess.TimeoutExpired:
        return _res(check, UNKNOWN, log, f"timed out after {check.timeout_seconds}s", sha, t0, "")
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"$ {' '.join(cmd)}\n# cwd {workspace}\n# revision {sha}\n{output}\n# exit {code}\n")
    problems = []
    if code != 0:
        problems.append(f"exit code {code}")
    if check.pass_regex and not re.search(check.pass_regex, output, re.M):
        problems.append(f"output has no line matching {check.pass_regex!r}")
    if check.fail_regex:
        m = re.search(check.fail_regex, output, re.M)
        if m:
            problems.append(f"output matches {check.fail_regex!r}: {m.group(0)[:120]!r}")
    if check.evidence:
        ev = check.evidence.format(**subs)
        if not os.path.exists(os.path.join(workspace, ev)):
            problems.append(f"evidence file {ev} missing")
    tail = "\n".join(output.strip().splitlines()[-15:])
    detail = ("; ".join(problems) + "\n" + tail) if problems else tail
    return _res(check, FAIL if problems else PASS, log, detail, sha, t0, output)


def _res(check, result, log, detail, sha, t0, output) -> VerificationResult:
    return VerificationResult(check=check.name, result=result, required=check.required,
                              evidence=log if output else "", detail=detail[:3000], revision=sha,
                              executed_ts=time.time(), duration_ms=int((time.time() - t0) * 1000))
