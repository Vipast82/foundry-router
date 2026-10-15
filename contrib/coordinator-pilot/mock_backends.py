"""Stand-in model backends for the coordinator pilot. NOT real models.

  strata    OpenAI-compatible (llama.cpp flavour) serving qwen3.8-flash-next-iq3_s:
            a SCRIPTED coder. It reads the task text Kilo sends and follows the
            ```pilot-actions:<phase>``` JSON block in it (phase = implement |
            repair | closeout), one tool call per turn, using Kilo's real tools
            (write, bash, read). A CLOSE session for an AnimalUprising sprint is
            scripted generically (summary, STATE row, done_check, commit, push).
  meridian  Anthropic Messages API serving claude-{haiku,sonnet,opus}-5-5, with
            /health reporting a Max subscription: a SCRIPTED reviewer. It
            reviews exactly the head commit named in the packet; a task whose
            text contains REVIEW-SCRIPT: changes-first gets CHANGES_REQUIRED
            once, then APPROVED.

Every request is appended to --log as JSON (backend, path, model, tools,
session header) so the pilot can show which backend served what.

  python mock_backends.py --strata 18601 --meridian 18602 --log calls.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = None
LOCK = threading.Lock()
REVIEWS: dict[str, int] = {}
CODER = "qwen3.8-flash-next-iq3_s"
CLAUDE = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


def log(rec: dict) -> None:
    rec["ts"] = time.time()
    with LOCK, open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


# ----------------------------------------------------------------------- the coder

def coder_turn(body: dict) -> tuple[dict | None, str]:
    """(tool call or None, final text) for this request."""
    msgs = body.get("messages") or []
    if not body.get("tools"):
        return None, "Pilot session"                      # title / summary requests
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"
                     and "pilot" not in str(m.get("name", ""))
                     and not (isinstance(m.get("content"), list) and
                              all(p.get("type") == "tool_result" for p in m["content"]
                                  if isinstance(p, dict)))), default=0)
    task_text = "\n".join(_text(m.get("content")) for m in msgs if m.get("role") == "user")
    prompt = _text(msgs[last_user].get("content"))
    wd = re.search(r"Working directory: (\S+)", task_text)
    wd = wd.group(1) if wd else "."
    done = sum(len(m.get("tool_calls") or []) for m in msgs[last_user + 1:]
               if m.get("role") == "assistant")
    if prompt.startswith("Continue task"):
        # resumed after a pause: carry on from the original prompt's script
        first = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), 0)
        prompt = _text(msgs[first].get("content"))
        done = sum(len(m.get("tool_calls") or []) for m in msgs if m.get("role") == "assistant")
    phase = ("closeout" if re.search(r"(?i)^(CLOSE session|You are closing out)", prompt.strip())
             else "repair" if re.search(r"(?i)repairing task|has found|found \(fix these", prompt)
             else "implement")
    actions = _actions(prompt, phase, wd)
    if done < len(actions):
        a = actions[done]
        return {"name": a["tool"], "arguments": json.dumps(a["args"])}, ""
    status = "DONE"
    m = re.search(r"pilot-report:%s\s+(\w[\w ]*)" % phase, prompt)
    if m:
        status = m.group(1).strip()
    return None, (f"Finished the {phase} steps.\n```coordinator-report\n"
                  + json.dumps({"status": status, "summary": f"{phase} done by the scripted pilot coder",
                                "blocker": "needs a decision" if status != "DONE" else ""})
                  + "\n```")


def _actions(prompt: str, phase: str, wd: str) -> list[dict]:
    m = re.search(r"```pilot-actions:%s\s*\n(.*?)```" % phase, prompt, re.S)
    if m:
        acts = json.loads(m.group(1))
    elif phase == "closeout":
        acts = _au_closeout(prompt)
    else:
        acts = []
    for a in acts:
        fp = a["args"].get("filePath")
        if fp and not fp.startswith("/"):
            a["args"]["filePath"] = f"{wd}/{fp}"
        if a["tool"] == "bash":
            a["args"].setdefault("description", "pilot step")
    return acts


def _au_closeout(prompt: str) -> list[dict]:
    """A generic AnimalUprising CLOSE session (AGENT_RULES §9): summary from
    the template's required fields, a STATE ledger row, done_check, commit by
    explicit path, one plain push."""
    m = re.search(r"CLOSE session for sprint (\S+)", prompt)
    if not m:
        return []
    sid = m.group(1)
    status = "COMPLETE-PENDING-VISUAL-REVIEW" if "COMPLETE-PENDING-VISUAL-REVIEW" in prompt else "DONE"
    summary = (f"# {sid} — pilot sprint\n\n**Status:** {status}\n\n## Files created\n"
               f"- `docs/progress/{sid}_NOTES.md`\n\n## Public API added\n(none)\n\n"
               f"## Test results\n```\nGATE not applicable (tier C)\n```\n")
    row = (f"| {sid} | Pilot tier-C sprint run end to end by the coordinator (scripted coder). "
           f"{status}. Summary docs/progress/{sid}_SUMMARY.md |")
    py = "python3"
    return [
        {"tool": "write", "args": {"filePath": f"docs/progress/{sid}_SUMMARY.md", "content": summary}},
        {"tool": "bash", "args": {"command": f"{py} - <<'EOF'\nimport re\np='docs/progress/STATE.md'\n"
                                             f"s=open(p).read()\nrow={row!r}\n"
                                             "i=s.index('## Sprint ledger')\nj=s.index('\\n|', i)\n"
                                             "k=s.index('\\n\\n', j+1)\ns=s[:k]+'\\n'+row+s[k:]\n"
                                             "open(p,'w').write(s)\nEOF"}},
        {"tool": "bash", "args": {"command": f"sed -i 's/^- \\[ \\] CLOSE/- [x] CLOSE/' "
                                             f"docs/progress/{sid}_PLAN.md"}},
        {"tool": "bash", "args": {"command": f"git add docs/progress/{sid}_SUMMARY.md "
                                             f"docs/progress/STATE.md docs/progress/{sid}_PLAN.md && "
                                             f"git commit -q -m '{sid}: closeout' && "
                                             f"{py} tools/guard/done_check.py {sid} "
                                             f">> docs/progress/{sid}_EVIDENCE_RAW.log; "
                                             f"git add docs/progress/{sid}_EVIDENCE_RAW.log && "
                                             f"git commit -q -m '{sid}: done_check evidence'; "
                                             f"git push -q origin master && git ls-remote origin master"}},
    ]


class Strata(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json({"data": [{"id": CODER, "object": "model"}]})
        if self.path.endswith("/props"):
            return self._json({"default_generation_settings": {"n_ctx": 65536}, "total_slots": 1})
        if self.path.endswith("/health"):
            return self._json({"status": "ok"})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        call, text = coder_turn(body)
        log({"backend": "strata", "path": self.path, "model": body.get("model"),
             "tools": len(body.get("tools") or []), "stream": bool(body.get("stream")),
             "action": call["name"] if call else "final"})
        usage = {"prompt_tokens": 900 + 40 * len(body.get("messages") or []), "completion_tokens": 25,
                 "total_tokens": 925}
        if not body.get("stream"):
            msg = {"role": "assistant", "content": text or None}
            if call:
                msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                      "function": call}]
            return self._json({"id": "c", "object": "chat.completion", "model": body.get("model"),
                               "choices": [{"index": 0, "message": msg,
                                            "finish_reason": "tool_calls" if call else "stop"}],
                               "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def send(delta, finish=None, use=None):
            ch = {"id": "c", "object": "chat.completion.chunk", "created": int(time.time()),
                  "model": body.get("model"), "choices": [{"index": 0, "delta": delta,
                                                           "finish_reason": finish}]}
            if use:
                ch["usage"] = use
            self.wfile.write(("data: " + json.dumps(ch) + "\n\n").encode())
            self.wfile.flush()
        if call:
            cid = "call_" + uuid.uuid4().hex[:8]
            send({"role": "assistant", "tool_calls": [{"index": 0, "id": cid, "type": "function",
                                                       "function": {"name": call["name"],
                                                                    "arguments": ""}}]})
            send({"tool_calls": [{"index": 0, "function": {"arguments": call["arguments"]}}]})
            send({}, "tool_calls", usage)
        else:
            send({"role": "assistant", "content": text})
            send({}, "stop", usage)
        self.wfile.write(b"data: [DONE]\n\n")


# ----------------------------------------------------------------------- the reviewer

def review_for(packet: str) -> str:
    head = re.search(r"Head commit \(review exactly this\): ([0-9a-f]{40})", packet)
    first = packet.splitlines()[0] if packet else ""
    proj = re.search(r"project ([\w.-]+),", first)
    task = re.search(r"task ([\w.-]+):", first)
    key = f"{proj.group(1) if proj else '?'}/{task.group(1) if task else '?'}"
    with LOCK:
        REVIEWS[key] = REVIEWS.get(key, 0) + 1
        n = REVIEWS[key]
    verdict, findings = "APPROVED", []
    if "REVIEW-SCRIPT: changes-first" in packet and n == 1:
        verdict = "CHANGES_REQUIRED"
        findings = [{"severity": "major", "file": "src/greeting.txt", "line": 1,
                     "message": "the greeting must also say 'friend'"}]
    return json.dumps({
        "sprint_id": task.group(1) if task else "?", "project_id": proj.group(1) if proj else "?",
        "reviewed_commit_sha": head.group(1) if head else "?", "verdict": verdict,
        "findings": findings, "evidence_inspected": ["diff", "mechanical verification"],
        "missing_evidence": [], "required_repairs": [f["message"] for f in findings],
        "residual_risks": ["scripted pilot reviewer, not a real model"]})


class Meridian(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json({"data": [{"id": m, "type": "model"} for m in CLAUDE]})
        if self.path.startswith("/health"):
            return self._json({"status": "healthy", "version": "pilot", "auth": {
                "loggedIn": True, "subscriptionType": "max"}})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        packet = "\n".join(_text(m.get("content")) for m in body.get("messages") or [])
        log({"backend": "meridian", "path": self.path, "model": body.get("model"),
             "x-session-id": self.headers.get("x-session-id"), "stream": bool(body.get("stream"))})
        text = review_for(packet) if "Review packet" in packet else "ok"
        usage = {"input_tokens": 1200, "output_tokens": 180}
        if not body.get("stream"):
            return self._json({"id": "msg_1", "type": "message", "role": "assistant",
                               "model": body.get("model"),
                               "content": [{"type": "text", "text": text}],
                               "stop_reason": "end_turn", "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for e in ({"type": "message_start", "message": {"usage": {"input_tokens": 1200}}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta",
                                                                       "text": text}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 180}},
                  {"type": "message_stop"}):
            self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
            self.wfile.flush()


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--strata", type=int, default=18601)
    ap.add_argument("--meridian", type=int, default=18602)
    ap.add_argument("--log", default="calls.jsonl")
    a = ap.parse_args()
    LOG = a.log
    servers = [ThreadingHTTPServer(("127.0.0.1", a.strata), Strata),
               ThreadingHTTPServer(("127.0.0.1", a.meridian), Meridian)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"strata :{a.strata}  meridian :{a.meridian}  log {a.log}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
