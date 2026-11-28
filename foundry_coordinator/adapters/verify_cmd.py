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
    ev_ok = True
    if check.evidence:
        ev_ok = os.path.exists(os.path.join(workspace, check.evidence.format(**subs)))
    return _judge(check, cmd, workspace, sha, subs, output, code, log, t0, ev_ok)


def _judge(check, cmd, workspace, sha, subs, output, code, log, t0, evidence_ok):
    """The verdict from a finished command (a local run and a run on a slot share it)."""
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
    if check.evidence and not evidence_ok:
        problems.append(f"evidence file {check.evidence.format(**subs)} missing")
    tail = "\n".join(output.strip().splitlines()[-15:])
    detail = ("; ".join(problems) + "\n" + tail) if problems else tail
    return _res(check, FAIL if problems else PASS, log, detail, sha, t0, output)


def run_check_remote(check, agent, project_id: str, lane: str, workspace: str, task_id: str,
                     sha: str, base: str, state_dir: str, sleep=None) -> VerificationResult:
    """run_check for a lane whose worktree is on a slot (Distributed Foundry phase b): the
    command runs there as the lane agent's `verify` job; the log, the verdict and the evidence
    pointer are made here exactly as for a local run. {python} is the slot's python3."""
    from ..lane_fetch import wait_job
    subs = {"task": task_id, "sha": sha, "base": base or "", "workspace": workspace,
            "python": "python3"}
    cmd = [c.format(**subs) for c in check.command]
    out_dir = evidence_dir(state_dir, project_id, task_id, sha)
    log = os.path.join(out_dir, re.sub(r"[^\w.-]", "_", check.name) + ".log")
    t0 = time.time()
    kw = {"sleep": sleep} if sleep else {}
    job = wait_job(agent, project_id, lane, "verify",
                   {"argv": cmd, "timeout": check.timeout_seconds,
                    "env": {"FOUNDRY_COORD_TASK": task_id, "FOUNDRY_COORD_SHA": sha}},
                   wait_s=float(check.timeout_seconds) + 120, **kw)
    if job["state"] in ("unreachable", "waiting"):
        return _res(check, UNKNOWN, log, f"the slot did not finish the check ({job['state']}): "
                    f"{job['output'][-300:]}", sha, t0, "")
    # newlines as a local run sees them (subprocess text mode turns \r\n into \n), so the
    # same pass_regex / fail_regex judge the same output on either machine
    output, code = job["output"].replace("\r\n", "\n"), job["rc"]
    if code == 124:
        return _res(check, UNKNOWN, log, f"timed out after {check.timeout_seconds}s", sha, t0, "")
    if code == 127 or code is None:
        return _res(check, UNKNOWN, log, f"command not found on the slot: {cmd[0]}", sha, t0, "")
    ev_ok = True
    if check.evidence:
        rel = check.evidence.format(**subs)
        try:
            st = agent.call("stat", {"project": project_id, "lane": lane, "paths": [rel]})
            ev_ok = bool(((st.get("stats") or {}).get(rel) or {}).get("exists"))
        except Exception:                                         # noqa: BLE001
            ev_ok = False
    return _judge(check, cmd, workspace, sha, subs, output, int(code), log, t0, ev_ok)


def _res(check, result, log, detail, sha, t0, output) -> VerificationResult:
    return VerificationResult(check=check.name, result=result, required=check.required,
                              evidence=log if output else "", detail=detail[:3000], revision=sha,
                              executed_ts=time.time(), duration_ms=int((time.time() - t0) * 1000))
