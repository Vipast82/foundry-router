"""Kilo session audit for AnimalUprising's checkpoint.py (AGENT_RULES §34).

tools/guard/checkpoint.py reads Cline's session files; it cannot see a Kilo
session. This adapter reads a Kilo session through the supported `kilo serve`
API (never Kilo's private storage), converts it to the message shape
checkpoint.py's own `audit()` and `render()` take, and runs them unchanged,
so the loop / repeat / error-streak / re-read / context rules are the
project's, not a copy.

Tool names are mapped to the Cline names checkpoint's rules look for:
  read  -> read_files {files: [{path, start_line, end_line}]}
  bash  -> run_commands {commands: [command]}
  edit / write -> editor {path}
Others keep their Kilo names (MCP tools keep theirs, so the playtest-cycling
rule still sees start_stop_play etc.).

Usage (repo root, while Kilo runs the sprint):
  py -3 -m foundry_coordinator.projects.animaluprising.kilo_checkpoint \\
      [--kilo-url http://127.0.0.1:4096] [--session ses_...] [--window 40]
Without --session it audits the busy (else newest) Kilo session whose
directory is this repository. Exit code and last line match checkpoint.py:
VERDICT CONTINUE | CAUTION | STOP-AND-REPLAN (0 / 1 / 2; 3 = no session).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from typing import Optional

TOOL_MAP = {"read": "read_files", "bash": "run_commands", "edit": "editor", "write": "editor"}


def _cline_input(tool: str, inp: dict) -> dict:
    inp = inp or {}
    if tool == "read":
        f = {"path": inp.get("filePath") or inp.get("path") or "?"}
        if inp.get("offset") is not None:
            f["start_line"] = inp.get("offset")
            if inp.get("limit") is not None:
                f["end_line"] = int(inp.get("offset") or 0) + int(inp.get("limit") or 0)
        return {"files": [f]}
    if tool == "bash":
        return {"commands": [inp.get("command") or ""]}
    if tool in ("edit", "write"):
        return {"path": inp.get("filePath") or inp.get("path") or "?",
                "old_text": inp.get("oldString"), "new_text": inp.get("newString")}
    return inp


def convert(messages: list) -> list:
    """Kilo [{info, parts}] -> checkpoint/Cline [{role, content[], metrics, ts}]."""
    out = []
    for m in messages or []:
        info = m.get("info") or {}
        if info.get("role") != "assistant":
            texts = [p.get("text", "") for p in m.get("parts") or [] if p.get("type") == "text"]
            out.append({"role": "user", "ts": (info.get("time") or {}).get("created"),
                        "content": [{"type": "text", "text": t} for t in texts]})
            continue
        tok = info.get("tokens") or {}
        in_tok = int(tok.get("input") or 0) + int(((tok.get("cache") or {}).get("read")) or 0)
        uses, results = [], []
        for p in m.get("parts") or []:
            if p.get("type") != "tool":
                continue
            st = p.get("state") or {}
            tool = p.get("tool") or "?"
            cid = p.get("callID") or p.get("id")
            uses.append({"type": "tool_use", "id": cid, "name": TOOL_MAP.get(tool, tool),
                         "input": _cline_input(tool, st.get("input") or {})})
            if st.get("status") == "error":
                results.append({"type": "tool_result", "tool_use_id": cid, "is_error": True,
                                "content": str(st.get("error") or "error")})
            elif st.get("status") == "completed":
                results.append({"type": "tool_result", "tool_use_id": cid,
                                "content": str(st.get("output") or "")})
        ts = (info.get("time") or {}).get("created")
        out.append({"role": "assistant", "ts": ts, "metrics": {"inputTokens": in_tok or None},
                    "content": uses or [{"type": "text", "text": ""}]})
        if results:
            out.append({"role": "user", "ts": ts, "content": results})
    return out


def load_checkpoint(repo: str):
    path = os.path.join(repo, "tools", "guard", "checkpoint.py")
    guard = os.path.dirname(path)
    if guard not in sys.path:
        sys.path.insert(0, guard)
    spec = importlib.util.spec_from_file_location("au_checkpoint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def audit(repo: str, session_id: str, messages: list, window: int = 40) -> tuple[str, int]:
    """(checkpoint text, level 0/1/2) for a Kilo transcript."""
    cp = load_checkpoint(repo)
    import guardlib as G                                       # from tools/guard (path added)
    msgs = convert(messages)
    calls = G.iter_calls(msgs)
    last_tok, findings = cp.audit(calls, msgs, window)
    text, level = cp.render({"session_id": f"kilo:{session_id}"}, calls, last_tok, findings, window)
    return text, level


def find_session(client, repo: str) -> Optional[str]:
    st = client._req("GET", "/session/status", query={"directory": repo}) or {}
    busy = [sid for sid, s in st.items() if (s or {}).get("type") in ("busy", "retry")]
    if busy:
        return busy[0]
    sessions = client._req("GET", "/session", query={"directory": repo}) or []
    norm = os.path.normcase(os.path.abspath(repo))
    mine = [s for s in sessions if os.path.normcase(os.path.abspath(s.get("directory") or ""))
            == norm]
    mine.sort(key=lambda s: (s.get("time") or {}).get("updated") or 0, reverse=True)
    return mine[0]["id"] if mine else None


def main(argv=None) -> int:
    from ...adapters.kilo import KiloClient, KiloError
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kilo-url", default=os.environ.get("KILO_URL", "http://127.0.0.1:4096"))
    ap.add_argument("--session")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--window", type=int, default=40)
    ap.add_argument("--from-json", help="audit an exported transcript ([{info, parts}] JSON)")
    a = ap.parse_args(argv)
    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            msgs = json.load(f)
        sid = a.session or os.path.basename(a.from_json)
    else:
        client = KiloClient(a.kilo_url, password=os.environ.get("KILO_SERVER_PASSWORD", ""))
        try:
            sid = a.session or find_session(client, a.repo)
            if not sid:
                print("CHECKPOINT no Kilo session for this repository")
                print("VERDICT UNKNOWN")
                return 3
            msgs = client.messages(sid, a.repo)
        except KiloError as e:
            print(f"CHECKPOINT cannot reach Kilo: {e}")
            print("VERDICT UNKNOWN")
            return 3
    text, level = audit(a.repo, sid, msgs, a.window)
    print(text)
    return level


if __name__ == "__main__":
    sys.exit(main())
