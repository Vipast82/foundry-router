"""Validate a persona end to end through Foundry's own HTTP route.

Each check is a real request to /v1/chat/completions (the OpenAI dialect Kilo
uses), made in-process with Foundry's internal identity, so it exercises the
same client auth, strict routing, budget checks, translation and backend call
a client gets. Discovery or a healthy endpoint is not treated as proof.

Checks (each: ok | warn | fail | skip, with detail and time):
  route        the strict route resolves inside the allowlist/backend, budget allows it
  text         a plain answer; the served model/backend is the expected one
  stream       streamed deltas, a finish reason and [DONE]
  tools        a tool call with a BOOLEAN argument parsed as a boolean, then
               the tool result round trip
  image_input  a generated red image arrives intact and is recognised
  length       a tiny max_tokens ends with finish_reason "length"
  image_job    (image personas) one real low-quality generation job

The result is stored on the persona (`validation`) and shown in the UI and in
/v1/models metadata. Frontier personas spend a few subscription calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
import zlib

import httpx

from .db import utcnow

_TOOL = {"type": "function", "function": {
    "name": "set_flag", "description": "Turn a named feature flag on or off.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "flag name"},
        "enabled": {"type": "boolean", "description": "true = on, false = off"}},
        "required": ["name", "enabled"]}}}


def red_png(size: int = 64) -> str:
    row = b"\x00" + b"\xd0\x10\x10" * size
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _jl(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _err_text(r: httpx.Response) -> str:
    try:
        e = r.json().get("error")
        return (e.get("message") if isinstance(e, dict) else str(e))[:300]
    except Exception:                                   # noqa: BLE001
        return r.text[:300]


class _Run:
    def __init__(self, svc, app, persona: dict):
        from .client_auth import INTERNAL_HEADER, INTERNAL_TOKEN
        self.svc, self.persona = svc, persona
        self.name = persona["virtual_name"]
        self.checks: list[dict] = []
        self.served: set = set()
        self.backends: set = set()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://foundry.internal",
            headers={INTERNAL_HEADER: INTERNAL_TOKEN, "x-foundry-session": "validate:" + self.name},
            timeout=httpx.Timeout(900.0, connect=10.0))

    def add(self, name, status, detail, ms=None):
        self.checks.append({"name": name, "status": status, "detail": detail,
                            "ms": round(ms) if ms is not None else None})

    async def post(self, path: str, body: dict) -> tuple[httpx.Response, float]:
        t = time.monotonic()
        # Own task: per-request context (backend pin, route record) stays isolated.
        r = await asyncio.create_task(self.client.post(path, json=body))
        h = r.headers
        if h.get("x-foundry-served-model"):
            self.served.add(h["x-foundry-served-model"])
        if h.get("x-foundry-served-backend"):
            self.backends.add(h["x-foundry-served-backend"])
        return r, (time.monotonic() - t) * 1000

    def body(self, messages, **kw) -> dict:
        b = {"model": self.name, "messages": messages, "stream": False}
        b.update(kw)
        return b

    def check_identity(self, r: httpx.Response) -> str:
        """'' when the served model/backend is the expected one, else why not."""
        from .facade.ollama_api import is_strict
        if not is_strict(self.persona):
            return ""
        allow = [str(a) for a in _jl(self.persona.get("model_allowlist"))]
        m, b = r.headers.get("x-foundry-served-model"), r.headers.get("x-foundry-served-backend")
        if m and allow and m not in allow and m.split(":")[0] not in {a.split(":")[0] for a in allow}:
            return f"served by {m}, which is not in the allowlist"
        want = (self.persona.get("backend") or "").strip()
        if want and b and b != want:
            return f"served by backend {b}, expected {want}"
        return ""


async def _route(run: _Run) -> bool:
    from .facade.ollama_api import is_strict, strict_candidates
    from .guardrails import RequestGuardState
    svc, p = run.svc, run.persona
    if not is_strict(p):
        run.add("route", "warn", "flexible routing: any model this persona's policy allows "
                                 "may answer (set routing=strict to pin one route)")
        return True
    cands = strict_candidates(svc, p)
    allow = [str(a) for a in _jl(p.get("model_allowlist"))]
    backend = (p.get("backend") or "").strip()
    if not cands:
        where = f" on backend {backend}" if backend else ""
        run.add("route", "fail", f"no model in the allowlist ({', '.join(allow) or 'empty'}) "
                                 f"is reachable{where}; requests are refused")
        return False
    eff = svc.guardrails.effective(p)
    denied = []
    for c in cands:
        v = await svc.guardrails.check_paid_call(c, svc.pool.backend_info(c),
                                                 svc.registry.get(c), RequestGuardState(), eff)
        if v.allowed:
            run.add("route", "ok", f"{c}" + (f" on {backend}" if backend else "")
                    + (f" (after {', '.join(denied)} denied)" if denied else ""))
            return True
        denied.append(f"{c}: {v.reason}")
    run.add("route", "fail", "budget denies every model in the route: " + "; ".join(denied))
    return False


async def _text(run: _Run) -> bool:
    r, ms = await run.post("/v1/chat/completions", run.body(
        [{"role": "user", "content": "Reply with exactly the word FOUNDRY_OK and nothing else."}]))
    if r.status_code != 200:
        run.add("text", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return False
    wrong = run.check_identity(r)
    if wrong:
        run.add("text", "fail", wrong, ms)
        return False
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    if "FOUNDRY_OK" in text.upper():
        run.add("text", "ok", f"answered by {r.headers.get('x-foundry-served-model', '?')}", ms)
    elif text.strip():
        run.add("text", "warn", f"answered but didn't follow the instruction: {text[:80]!r}", ms)
    else:
        run.add("text", "fail", "empty answer (reasoning may have used the whole output budget)", ms)
        return False
    return True


async def _stream(run: _Run) -> None:
    r, ms = await run.post("/v1/chat/completions", run.body(
        [{"role": "user", "content": "Say hello in five words."}], stream=True))
    if r.status_code != 200:
        run.add("stream", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return
    content, finish, done, chunks = "", None, False, 0
    for line in r.text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        for ch in ev.get("choices") or []:
            chunks += 1
            content += (ch.get("delta") or {}).get("content") or ""
            finish = ch.get("finish_reason") or finish
    if content.strip() and finish and done:
        run.add("stream", "ok", f"{chunks} chunks, finish_reason {finish}", ms)
    else:
        run.add("stream", "fail", f"content={'yes' if content.strip() else 'none'}, "
                                  f"finish_reason={finish}, [DONE]={'yes' if done else 'no'}", ms)


async def _tools(run: _Run) -> None:
    msgs = [{"role": "user", "content": "Use the set_flag tool to turn the flag named "
                                        "alpha on. Do not answer in text."}]
    r, ms = await run.post("/v1/chat/completions", run.body(msgs, tools=[_TOOL]))
    if r.status_code != 200:
        run.add("tools", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    if not calls:
        run.add("tools", "fail", f"no tool call (answered: {(msg.get('content') or '')[:80]!r})", ms)
        return
    fn = calls[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        run.add("tools", "fail", f"arguments are not valid JSON: {fn.get('arguments')!r}", ms)
        return
    if fn.get("name") != "set_flag":
        run.add("tools", "fail", f"called {fn.get('name')!r} instead of set_flag", ms)
        return
    if args.get("enabled") is not True:
        kind = type(args.get("enabled")).__name__
        run.add("tools", "fail", f"boolean argument arrived as {kind} {args.get('enabled')!r}", ms)
        return
    # round trip: the tool result goes back, the model must continue
    msgs += [{"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls[:1]},
             {"role": "tool", "tool_call_id": calls[0].get("id") or "call_1",
              "content": json.dumps({"ok": True, "flag": "alpha", "enabled": True})}]
    r2, ms2 = await run.post("/v1/chat/completions", run.body(msgs, tools=[_TOOL]))
    if r2.status_code != 200:
        run.add("tools", "fail", f"tool result round trip: HTTP {r2.status_code}: {_err_text(r2)}",
                ms + ms2)
        return
    run.add("tools", "ok", f"set_flag(name={args.get('name')!r}, enabled=true) and the tool "
                           f"result round trip", ms + ms2)


async def _image_input(run: _Run) -> None:
    r, ms = await run.post("/v1/chat/completions", run.body([{"role": "user", "content": [
        {"type": "text", "text": "What single colour fills this image? Answer with one word."},
        {"type": "image_url", "image_url": {"url": red_png()}}]}]))
    if r.status_code != 200:
        run.add("image_input", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return
    text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if "red" in text.lower():
        run.add("image_input", "ok", "the image arrived and was recognised as red", ms)
    else:
        run.add("image_input", "fail", f"answer {text[:80]!r}: the image didn't arrive intact "
                                       f"or the model can't see images", ms)


async def _length(run: _Run) -> None:
    from .facade.ollama_api import strict_candidates
    cands = strict_candidates(run.svc, run.persona)
    info = run.svc.pool.backend_info(cands[0]) if cands else None
    if info and info.get("flavor") == "cliproxyapi":
        run.add("length", "skip", "Codex models ignore max_tokens (CLIProxyAPI)")
        return
    r, ms = await run.post("/v1/chat/completions", run.body(
        [{"role": "user", "content": "Count from 1 to 300, separated by spaces."}],
        max_tokens=16, reasoning_effort="none"))
    if r.status_code != 200:
        run.add("length", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return
    fr = (r.json().get("choices") or [{}])[0].get("finish_reason")
    run.add("length", "ok" if fr == "length" else "warn",
            f"finish_reason {fr!r}" + ("" if fr == "length" else " (expected 'length')"), ms)


async def _image_job(run: _Run) -> None:
    r, ms = await run.post("/media/jobs", {"prompt": "A plain red square, flat colour.",
                                           "model": run.name, "quality": "low",
                                           "size": "1024x1024", "allow_duplicate": True,
                                           "wait_seconds": 60})
    if r.status_code not in (200, 202):
        run.add("image_job", "fail", f"HTTP {r.status_code}: {_err_text(r)}", ms)
        return
    job = r.json()
    t0 = time.monotonic()
    while job.get("status") in ("queued", "running") and time.monotonic() - t0 < 900:
        rr = await asyncio.create_task(run.client.get(f"/media/jobs/{job['job_id']}?wait=60"))
        job = rr.json()
    ms += (time.monotonic() - t0) * 1000
    if job.get("status") == "succeeded":
        a = (job.get("artifacts") or [{}])[0]
        run.add("image_job", "ok", f"{job['model']} produced {a.get('width')}×{a.get('height')} "
                                   f"(job {job['job_id']})", ms)
    else:
        err = (job.get("error") or {}).get("message") or job.get("status")
        run.add("image_job", "fail", f"job {job.get('job_id')}: {err}", ms)


async def validate(svc, app, name: str) -> dict:
    persona = svc.personas.get(name)
    if persona is None:
        raise KeyError(name)
    run = _Run(svc, app, persona)
    caps = [str(c) for c in _jl(persona.get("capabilities"))]
    try:
        if (persona.get("kind") or "chat") == "media":
            from . import client_auth
            tok = client_auth._client.set({"client_id": "foundry:validate", "internal": True,
                                           "scopes": ["media"], "personas": []})
            try:
                model, _p = svc.media.resolve_model(persona["virtual_name"])
                state, _info = svc.media.resolve_backend(
                    model, svc.media.persona_backend(persona["virtual_name"]))
                route_ok = True
            except Exception as e:                       # noqa: BLE001 (MediaError)
                run.add("route", "fail", getattr(e, "message", str(e)))
                route_ok = False
            finally:
                client_auth._client.reset(tok)
            if route_ok:
                run.add("route", "ok", f"{model} on {state.config.name}")
                await _image_job(run)
        elif await _route(run):
            if await _text(run):
                await _stream(run)
                if "tools" in caps or not caps:
                    await _tools(run)
                else:
                    run.add("tools", "skip", "persona doesn't declare tool calling")
                if "image_input" in caps:
                    await _image_input(run)
                else:
                    run.add("image_input", "skip", "persona doesn't declare image input")
                await _length(run)
    finally:
        await run.client.aclose()
    statuses = {c["status"] for c in run.checks}
    overall = "failed" if "fail" in statuses else "warnings" if "warn" in statuses else "passed"
    result = {"status": overall, "at": utcnow(), "served_models": sorted(run.served),
              "backends": sorted(run.backends), "checks": run.checks}
    svc.db.execute("UPDATE personas SET validation=? WHERE virtual_name=?",
                   (json.dumps(result), persona["virtual_name"]))
    svc.db.log_event("info" if overall == "passed" else "warning", "personas",
                     f"validation of {persona['virtual_name']}: {overall}",
                     json.dumps(run.checks)[:2000])
    return result
