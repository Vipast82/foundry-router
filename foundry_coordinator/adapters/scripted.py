"""ScriptedClient: a coding client that follows a Python script instead of a
model. For the engine's tests and dry runs only; it never claims to be Kilo.

The profile names it with [client] kind = "scripted", script = "module:function".
The function gets {project_id, task_id, phase, attempt, prompt, workspace,
session_id} and returns a dict:
  writes:   {path: content}         files to write (relative to the workspace)
  commit:   "message"               git add the written files + commit
  run:      [["cmd", ...], ...]     commands to run in the workspace
  final:    "text"                  the session's last message (may hold a report)
  question: {"text": .., "kind": "question"|"permission"}  the session stops waiting
                                    (an implementer question is auto-answered, W1.1)
  error:    "text"                  the session ends with this provider error
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
import uuid

from .base import CodingClient, SessionState, parse_report
from .kilo import foundry_denial


def load_callable(spec: str):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn)


class ScriptedClient(CodingClient):
    name = "scripted"

    def __init__(self, script, project_id: str = ""):
        self.script = load_callable(script) if isinstance(script, str) else script
        self.project_id = project_id
        self.sessions: dict[str, dict] = {}
        self.answers: dict[str, list] = {}

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        sid = f"scr_{uuid.uuid4().hex[:12]}"
        self.sessions[sid] = {"directory": directory, "metadata": metadata, "persona": persona,
                              "permissions": permissions, "prompts": [], "state": None}
        return sid

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        s = self.sessions[session_id]
        s["prompts"].append(text)
        s["state"] = None
        s["pending"] = True

    def state(self, session_id, *, directory, since_ts) -> SessionState:
        s = self.sessions.get(session_id)
        if s is None:
            return SessionState(status="missing", error="no such session")
        if s.get("pending"):
            s["pending"] = False
            md = s["metadata"]
            out = self.script({"project_id": md.get("project"), "task_id": md.get("task"),
                               "phase": md.get("phase"), "attempt": md.get("attempt"),
                               "prompt": s["prompts"][-1], "workspace": directory,
                               "session_id": session_id, "answers": self.answers.get(session_id)
                               }) or {}
            s["state"] = self._apply(directory, out)
        return s["state"] or SessionState(status="busy")

    def _apply(self, ws: str, out: dict) -> SessionState:
        for rel, content in (out.get("writes") or {}).items():
            p = os.path.join(ws, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        for cmd in out.get("run") or []:
            subprocess.run(cmd, cwd=ws, capture_output=True)
        if out.get("commit"):
            files = list((out.get("writes") or {}).keys()) or ["-A"]
            subprocess.run(["git", "add", "--", *files] if files != ["-A"] else
                           ["git", "add", "-A"], cwd=ws, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", out["commit"]], cwd=ws,
                           capture_output=True)
        if out.get("question"):
            # kind "permission" = a Kilo permission ask (still a person); "question" is
            # answered by the coordinator itself for implementer sessions (W1.1)
            return SessionState(status="attention", question={
                "request_id": f"q_{uuid.uuid4().hex[:8]}",
                "kind": out["question"].get("kind") or "question",
                "text": out["question"].get("text", ""), "options": []})
        if out.get("error"):
            return SessionState(status="error", error=out["error"],
                                denial=foundry_denial(out["error"]))
        text = out.get("final") or ""
        return SessionState(status="idle", final_text=text, report=parse_report(text),
                            tokens={"input": 100, "output": 20}, context=out.get("context"))

    def answer(self, session_id, request_id, answers, *, directory, kind="question") -> None:
        self.answers[session_id] = answers
        s = self.sessions[session_id]
        s["pending"] = True

    def exists(self, session_id, *, directory) -> bool:
        return session_id in self.sessions

    def transcript(self, session_id, *, directory) -> list:
        return [{"prompt": p} for p in self.sessions.get(session_id, {}).get("prompts", [])]


def now() -> float:
    return time.time()
