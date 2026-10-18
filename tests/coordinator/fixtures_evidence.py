"""Fakes for the evidence ledger tests: Kilo-shaped transcripts, a scripted
transcript client, a structured-output adapter and a tiny git workspace."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

from foundry_coordinator.adapters.git import Workspace

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")


def git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          env=GIT_ENV, check=True).stdout.strip()


def make_ws(tmp_path, files: dict, name: str = "ws") -> tuple[Workspace, str]:
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    return Workspace(str(root)), commit(root, files, "init")


def commit(root, files: dict, msg: str = "c") -> str:
    root = str(getattr(root, "path", root))
    for rel, text in files.items():
        p = os.path.join(root, rel)
        if text is None:
            git(root, "rm", "-q", rel)
            continue
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        git(root, "add", rel)
    git(root, "commit", "-q", "--allow-empty", "-m", msg)
    return git(root, "rev-parse", "HEAD")


# -- transcripts -----------------------------------------------------------------------

def tool(pid, cmd="", output="", *, tool_name="bash", call=None, status="completed",
         truncated=False, start=1000.0, end=None, error=None, exit_code=0):
    st = {"status": status, "input": {"command": cmd},
          "time": {"start": int(start * 1000), "end": int((end or start + 1) * 1000)},
          "metadata": {"exit": exit_code, "truncated": truncated}}
    if status == "completed":
        st["output"] = output
    if status == "error":
        st["error"] = error or "tool failed"
    return {"id": pid, "type": "tool", "tool": tool_name, "callID": call or f"call_{pid}",
            "state": st}


def text(pid, body):
    return {"id": pid, "type": "text", "text": body}


def msg(mid, parts, role="assistant", completed=True, error=None):
    info = {"id": mid, "role": role, "time": {"created": 1, **({"completed": 2} if completed
                                                              else {})}}
    if error:
        info["error"] = error
    return {"info": info, "parts": parts}


def suite_output(check, passed, failed=0, extra=None, boot=None):
    """A structured suite result the fake adapter parses (JSON, like a Studio
    tool return value)."""
    body = {"suite": check, "pass": passed, "fail": failed,
            "lines": [f"PASS {check}.case{i}" for i in range(passed)] +
                     [f"FAIL {check}.bad{i}" for i in range(failed)]}
    if extra:
        body.update(extra)
    out = json.dumps(body)
    return (f"BOOT runtime={boot[0]} loaded={boot[1]}\n" if boot else "") + out


class FakeClient:
    def __init__(self, transcripts: dict):
        self.transcripts = transcripts
        self.fail = set()
        self.calls = []

    def transcript(self, sid, *, directory=None):
        self.calls.append((sid, directory))
        if sid in self.fail:
            raise ConnectionError("kilo down")
        return self.transcripts.get(sid, [])


class FakeAdapter:
    """classify_tool_result parses only the structured JSON a test tool returns;
    it never reads prose. Chains/dependencies/runtime come from the spec data."""

    def __init__(self, required=None, deps=None, expected=None):
        self.required = required or []
        self.deps = deps or {}               # check_id -> [globs]; absent -> unknown
        self.expected = expected or {}       # revision -> loaded fp
        self.explode_on = None               # part_id that raises once
        self.exploded = False
        self.chains = {}                     # check_id -> (chain_id, seq)

    def classify_tool_result(self, view, ctx):
        if self.explode_on and view["part_id"] == self.explode_on and not self.exploded:
            self.exploded = True
            raise RuntimeError("adapter crashed mid-scan")
        out = view["output"] or ""
        line = out.splitlines()[-1] if out else ""
        cid = None
        try:
            body = json.loads(line)
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("suite"):
            cid = body["suite"]
        elif view["status"] in ("error", "running", "pending") or view["status"] is None:
            # an errored/aborted call is attributed by its command
            for c in ("UI_01", "CORE", "S47_1", "S47_2", "S47_3"):
                if c in view["input"]:
                    cid = c
            body = None
        if not cid:
            return []
        ch = self.chains.get(cid, ("", None))
        res = "UNKNOWN"
        if isinstance(body, dict):
            res = "PASS" if body.get("fail") == 0 and body.get("pass", 0) > 0 else "FAIL"
        return [{"check_id": cid, "chain_id": ch[0], "seq": ch[1], "prereqs": [],
                 "result": res, "counts": {"pass": (body or {}).get("pass"),
                                           "fail": (body or {}).get("fail")},
                 "exclusion": (body or {}).get("exclusion") or "",
                 "runtime_hint": "", "artifact_path": (body or {}).get("artifact") or ""}]

    def runtime_identity(self, view):
        first = (view["output"] or "").splitlines()[:1]
        if first and first[0].startswith("BOOT runtime="):
            kv = dict(x.split("=", 1) for x in first[0].split()[1:])
            return {"runtime_id": kv["runtime"], "kind": "studio", "loaded_fp": kv["loaded"]}
        return None

    def void_runtime(self, view):
        return "VM_COLLIDED" in (view["output"] or "")

    def required_checks(self, spec, phase):
        return list(self.required)

    def check_dependencies(self, spec, check_id):
        return self.deps.get(check_id)

    def runtime_expected(self, spec, revision):
        return self.expected.get(revision)


class BareAdapter:
    """An adapter with none of the optional evidence methods."""


def make_P(pid, ws, transcripts, adapter=None):
    return SimpleNamespace(id=pid, client=FakeClient(transcripts),
                           profile=SimpleNamespace(workspace=ws.path if ws else "/nowhere"),
                           ws=ws, adapter=adapter or FakeAdapter())


def spec(task_id="T1"):
    return SimpleNamespace(task_id=task_id, project_id="", data={})


def add_sessions(store, pid, tid, sids, run_id="run1", head="", ended=True):
    for i, sid in enumerate(sids):
        store.add_session(sid, pid, tid, run_id, "implementer", "BUILD", i + 1, "kilo", "p",
                          data={"head": head} if head else None)
        store.update_session(sid, created_ts=1000.0 + i)
        if ended:
            store.update_session(sid, status="ended", ended_ts=2000.0 + i)
