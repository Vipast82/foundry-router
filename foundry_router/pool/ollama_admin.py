"""Ollama model-lifecycle management, driven from the admin UI.

Foundry already holds each Ollama backend's URL and a shared httpx client, so
model administration (list / show / copy / rename / delete / pull / create /
push) is just proxying the official Ollama REST endpoints to a chosen backend.
This keeps everything manageable from the one UI instead of SSHing to TrueNAS.

Long-running streaming ops (pull / create / push) run as background tasks with
in-memory progress the UI polls; the quick ops (tags/show/copy/delete) are plain
awaits. Destructive ops (delete) are gated in the UI, not here.

Ollama's request key changed over versions (`name` -> `model`) and some deploys
still expect the old one, so every payload sends BOTH keys — harmless to the
side that ignores the extra field, and it means this works across versions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from ..db import Database, utcnow
from ..errors import describe_exception

log = logging.getLogger("foundry.ollama_admin")

# Streaming pulls can run for many minutes; disable the read timeout for those
# (progress frames arrive continuously, so a stalled stream still fails via the
# connection dropping) while keeping a fast connect timeout.
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
_QUICK_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)


def _coerce(v: str):
    v = v.strip().strip('"')
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def parse_modelfile(text: str) -> dict:
    """Translate a Modelfile into the STRUCTURED fields modern Ollama's
    /api/create wants (from / system / template / parameters / adapters).

    Modern Ollama removed the legacy raw `modelfile` request field — a create
    with only that now 400s with "neither 'from' or 'files' was specified". So
    a pasted Modelfile has to be parsed. Handles FROM, PARAMETER (numeric
    coercion; repeated keys -> list, e.g. stop), SYSTEM / TEMPLATE / LICENSE
    (single line or triple-quoted multi-line), and ADAPTER; other directives
    are ignored for pseudo-model creation.
    """
    out: dict = {}
    params: dict = {}
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        directive = parts[0].upper()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if directive == "FROM":
            out["from"] = rest
        elif directive == "PARAMETER":
            kv = rest.split(None, 1)
            if len(kv) == 2:
                key, val = kv[0], _coerce(kv[1])
                if key in params:
                    cur = params[key]
                    params[key] = cur + [val] if isinstance(cur, list) else [cur, val]
                else:
                    params[key] = val
        elif directive in ("SYSTEM", "TEMPLATE", "LICENSE"):
            if rest.startswith('"""'):
                body = rest[3:]
                if body.endswith('"""') and len(body) >= 3:
                    value = body[:-3]
                else:
                    collected = [body]
                    while i < n:
                        ln = lines[i]
                        i += 1
                        if ln.rstrip().endswith('"""'):
                            collected.append(ln.rstrip()[:-3])
                            break
                        collected.append(ln)
                    value = "\n".join(collected)
                value = value.strip("\n")
            else:
                value = rest.strip('"')
            out[directive.lower()] = value
        elif directive == "ADAPTER":
            out.setdefault("adapters", []).append(rest)
    if params:
        out["parameters"] = params
    return out


class OllamaAdmin:
    def __init__(self, client: httpx.AsyncClient, pool: Any, db: Database):
        self.client = client
        self.pool = pool
        self.db = db
        # job key "backend:op:model" -> progress dict the UI polls
        self.jobs: dict[str, dict] = {}

    # -- backend resolution --------------------------------------------------------

    def backends(self) -> list[dict]:
        """Ollama-type backends only — the ones these operations can target."""
        out = []
        for s in getattr(self.pool, "backends", {}).values():
            if s.config.type == "ollama":
                out.append({"name": s.config.name, "url": s.config.url,
                            "healthy": getattr(s, "healthy", False)})
        return sorted(out, key=lambda b: b["name"])

    def _url(self, backend: str) -> str:
        for s in getattr(self.pool, "backends", {}).values():
            if s.config.name == backend and s.config.type == "ollama":
                return s.config.url.rstrip("/")
        raise ValueError(f"no Ollama backend named {backend!r}")

    async def _refresh_pool(self) -> None:
        """After a mutation, re-run backend discovery so the model map (and the
        registry) reflect the change without waiting for the health interval."""
        check = getattr(self.pool, "check_all", None)
        if check:
            try:
                await check()
            except Exception:
                log.exception("post-mutation pool refresh failed")

    # -- quick ops -----------------------------------------------------------------

    async def tags(self, backend: str) -> list[dict]:
        r = await self.client.get(f"{self._url(backend)}/api/tags", timeout=_QUICK_TIMEOUT)
        r.raise_for_status()
        return r.json().get("models", []) or []

    async def loaded(self, backend: str) -> list[str]:
        """Model names currently resident in VRAM on this backend (/api/ps)."""
        r = await self.client.get(f"{self._url(backend)}/api/ps", timeout=_QUICK_TIMEOUT)
        r.raise_for_status()
        return [m.get("name") or m.get("model") for m in r.json().get("models", [])
                if m.get("name") or m.get("model")]

    async def show(self, backend: str, model: str) -> dict:
        r = await self.client.post(f"{self._url(backend)}/api/show",
                                   json={"model": model, "name": model},
                                   timeout=_QUICK_TIMEOUT)
        r.raise_for_status()
        return r.json()

    async def copy(self, backend: str, source: str, destination: str) -> None:
        r = await self.client.post(f"{self._url(backend)}/api/copy",
                                   json={"source": source, "destination": destination},
                                   timeout=_QUICK_TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"copy HTTP {r.status_code}: {r.text[:300]}")
        self.db.log_event("info", "ollama_admin",
                          f"copied {source} -> {destination} on {backend}")
        await self._refresh_pool()

    async def delete(self, backend: str, model: str) -> None:
        r = await self.client.request(
            "DELETE", f"{self._url(backend)}/api/delete",
            json={"model": model, "name": model}, timeout=_QUICK_TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"delete HTTP {r.status_code}: {r.text[:300]}")
        self.db.log_event("warning", "ollama_admin", f"deleted {model} on {backend}")
        await self._refresh_pool()

    async def rename(self, backend: str, source: str, destination: str) -> None:
        """No native rename in Ollama — copy then delete the source."""
        await self.copy(backend, source, destination)
        await self.delete(backend, source)
        self.db.log_event("info", "ollama_admin",
                          f"renamed {source} -> {destination} on {backend}")

    # -- streaming (background) ops -------------------------------------------------

    def _new_job(self, backend: str, op: str, model: str) -> str:
        key = f"{backend}:{op}:{model}"
        self.jobs[key] = {"backend": backend, "op": op, "model": model,
                          "state": "running", "percent": 0.0, "status_text": "",
                          "error": "", "ts": utcnow()}
        return key

    def start_pull(self, backend: str, model: str) -> str:
        self._url(backend)                    # validate up front (raises if bad)
        key = self._new_job(backend, "pull", model)
        asyncio.ensure_future(self._stream(
            key, "POST", f"{self._url(backend)}/api/pull",
            {"model": model, "name": model, "stream": True}))
        return key

    def start_push(self, backend: str, model: str) -> str:
        self._url(backend)
        key = self._new_job(backend, "push", model)
        asyncio.ensure_future(self._stream(
            key, "POST", f"{self._url(backend)}/api/push",
            {"model": model, "name": model, "stream": True}))
        return key

    def start_create(self, backend: str, model: str, *, from_model: str = "",
                     system: str = "", parameters: Optional[dict] = None,
                     modelfile: str = "") -> str:
        """Create a model (a 'pseudo-model' with a baked-in system prompt /
        params) from a base model, or from a raw Modelfile. Always sends the
        STRUCTURED create fields — a raw Modelfile is parsed into them, since
        modern Ollama no longer accepts the legacy `modelfile` field."""
        self._url(backend)
        if modelfile.strip():
            fields = parse_modelfile(modelfile)        # raw Modelfile -> structured
        else:
            fields = {}
            if from_model.strip():
                fields["from"] = from_model.strip()
            if system.strip():
                fields["system"] = system
            if parameters:
                fields["parameters"] = parameters
        if not (fields.get("from") or fields.get("files") or fields.get("adapters")):
            raise ValueError("create needs a base model — a FROM line in the "
                             "Modelfile, or the 'from (base)' field")
        payload: dict[str, Any] = {"model": model, "name": model, "stream": True, **fields}
        key = self._new_job(backend, "create", model)
        asyncio.ensure_future(self._stream(
            key, "POST", f"{self._url(backend)}/api/create", payload))
        return key

    async def _stream(self, key: str, method: str, url: str, payload: dict) -> None:
        job = self.jobs[key]
        op, model, backend = job["op"], job["model"], job["backend"]
        self.db.log_event("info", "ollama_admin", f"{op} started: {model} on {backend}")
        try:
            async with self.client.stream(method, url, json=payload,
                                          timeout=_STREAM_TIMEOUT) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {r.status_code}: {body[:300]}")
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    if data.get("status"):
                        job["status_text"] = str(data["status"])[:120]
                    total, done = data.get("total"), data.get("completed")
                    if total:
                        job["percent"] = round(100.0 * (done or 0) / total, 1)
                    job["ts"] = utcnow()
            job["state"] = "done"
            job["percent"] = 100.0
            self.db.log_event("info", "ollama_admin", f"{op} complete: {model} on {backend}")
            await self._refresh_pool()
        except Exception as e:   # noqa: BLE001 — surface, don't crash the loop
            job["state"] = "error"
            job["error"] = describe_exception(e)
            job["ts"] = utcnow()
            self.db.log_event("warning", "ollama_admin",
                              f"{op} failed: {model} on {backend}", describe_exception(e))

    def job_snapshot(self) -> list[dict]:
        """Newest first; the UI polls this to drive progress bars. Terminal jobs
        stay until a new op with the same key replaces them (cheap history)."""
        return sorted(self.jobs.values(), key=lambda j: j["ts"], reverse=True)
