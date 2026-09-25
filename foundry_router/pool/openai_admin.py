"""Read-only host diagnostics for openai-dialect backends (llama.cpp, Unsloth,
vLLM, generic OpenAI).

Unlike Ollama — which has a full model-lifecycle REST API (pull/copy/rename/
delete/create) that `ollama_admin.py` proxies — these servers expose only
READ-ONLY introspection, and each a different subset:

  llama.cpp (llama-server)  GET /health, /props (loaded model + sampling
                            defaults + n_ctx + build + modalities), /slots
                            (KV-cache slot state), /metrics (Prometheus, needs
                            --metrics), /v1/models. ONE model per process; model
                            CRUD needs the separate `llama-swap` proxy, whose
                            GET /running is probed too.
  Unsloth                   GET /v1/models (loaded). Model loading is a host CLI
                            (`unsloth run --model <name>`), NOT a REST call, so
                            there is nothing to add/delete over HTTP.
  vLLM                      GET /v1/models (incl. max_model_len), /health,
                            /version, /metrics (Prometheus text).
  openai (generic)          GET /v1/models.

So this module deliberately offers no mutation — the Host Admin panel renders
whatever a flavor's probes return as status cards. Every probe is individually
guarded: an unreachable or unimplemented endpoint yields a per-probe error
string, never an exception that sinks the whole diagnostics call.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ..db import Database
from ..errors import describe_exception

log = logging.getLogger("foundry.openai_admin")

_QUICK_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

# Which read-only probes each flavor supports, in display order. The Host Admin
# UI renders a card per present probe; a flavor asks only for endpoints its
# server actually implements, so we don't spam 404s.
_PROBES: dict[str, list[str]] = {
    "llamacpp": ["health", "swap", "props", "slots", "models", "metrics"],
    "unsloth":  ["models"],
    "vllm":     ["health", "version", "models", "metrics"],
    "openai":   ["models"],
}


class OpenAIAdmin:
    """Diagnostics for openai-compatible (non-Ollama) backends, driven by the
    Host Admin tab. Read-only by construction."""

    def __init__(self, client: httpx.AsyncClient, pool: Any, db: Database):
        self.client = client
        self.pool = pool
        self.db = db

    # -- backend resolution --------------------------------------------------------

    def backends(self) -> list[dict]:
        """openai-compatible backends only — the ones this diagnostics surface can
        introspect. Each carries its effective flavor so the UI picks the panel."""
        out = []
        for s in getattr(self.pool, "backends", {}).values():
            if s.config.type == "openai-compatible":
                out.append({"name": s.config.name, "url": s.config.url,
                            "flavor": s.config.effective_flavor,
                            "healthy": getattr(s, "healthy", False)})
        return sorted(out, key=lambda b: b["name"])

    def _server(self, backend: str):
        for s in getattr(self.pool, "backends", {}).values():
            if s.config.name == backend and s.config.type == "openai-compatible":
                return s
        raise ValueError(f"no openai-compatible backend named {backend!r}")

    @staticmethod
    def _root(url: str) -> str:
        """Server root (no /v1) — llama.cpp's /health, /props, /slots live here."""
        u = url.rstrip("/")
        return u[:-3].rstrip("/") if u.endswith("/v1") else u

    @staticmethod
    def _v1(url: str) -> str:
        """OpenAI base (…/v1) — mirrors OpenAIProtocol._base()."""
        u = url.rstrip("/")
        return u if u.endswith("/v1") else f"{u}/v1"

    def _headers(self, api_key: Optional[str]) -> dict:
        h = {"Content-Type": "application/json"}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        return h

    # -- individual probes (each returns {ok, ...} or {ok:False, error}) -----------

    async def _get_json(self, url: str, headers: dict) -> dict:
        r = await self.client.get(url, headers=headers, timeout=_QUICK_TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    async def _probe_health(self, root: str, headers: dict) -> dict:
        try:
            r = await self.client.get(f"{root}/health", headers=headers,
                                      timeout=_QUICK_TIMEOUT)
            body: Any
            try:
                body = r.json()
            except Exception:
                body = (r.text or "").strip()[:200]
            return {"ok": r.status_code < 400, "status_code": r.status_code,
                    "body": body}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    async def _probe_props(self, root: str, headers: dict) -> dict:
        """llama.cpp /props — loaded model path + generation defaults + n_ctx,
        build, modalities (vision/audio) and the chat template's declared caps."""
        try:
            data = await self._get_json(f"{root}/props", headers)
            gen = data.get("default_generation_settings") or {}
            params = gen.get("params") if isinstance(gen.get("params"), dict) else {}
            keep = ("temperature", "top_k", "top_p", "min_p", "typical_p",
                    "repeat_penalty", "repeat_last_n", "presence_penalty",
                    "frequency_penalty", "dry_multiplier", "xtc_probability",
                    "top_n_sigma", "n_predict", "samplers", "reasoning_format",
                    "speculative.n_max", "speculative.n_min", "speculative.p_min")
            sampling = {k: params[k] for k in keep if k in params}
            return {"ok": True,
                    "model_path": data.get("model_path") or gen.get("model")
                    or data.get("model") or "",
                    "n_ctx": gen.get("n_ctx") or data.get("n_ctx"),
                    "total_slots": data.get("total_slots"),
                    "build_info": data.get("build_info") or "",
                    "modalities": data.get("modalities") or {},
                    "chat_template_caps": data.get("chat_template_caps") or {},
                    "chat_template_present": bool(data.get("chat_template")),
                    "sampling_defaults": sampling,
                    "generation_settings": gen}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    async def _probe_slots(self, root: str, headers: dict) -> dict:
        """llama.cpp /slots — per-slot busy state + context (may be disabled
        with --no-slots)."""
        from .prom import summarize_slots
        try:
            data = await self._get_json(f"{root}/slots", headers)
            slots = data if isinstance(data, list) else data.get("slots") or []
            summ = summarize_slots(slots)
            return {"ok": True, "count": summ.get("slots_total", 0),
                    "busy": summ.get("slots_busy", 0),
                    "slots": summ.get("slots", [])}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    async def _probe_swap(self, root: str, headers: dict) -> dict:
        """llama-swap GET /running — which configured models are resident.
        Absent (404) on a plain llama-server: reported as not-a-swap, not an
        error."""
        try:
            r = await self.client.get(f"{root}/running", headers=headers,
                                      timeout=_QUICK_TIMEOUT)
            data = r.json() if r.status_code < 400 else None
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("running"), list):
            return {"ok": True, "is_swap": True,
                    "running": [{"model": x.get("model"), "state": x.get("state")}
                                for x in data["running"] if isinstance(x, dict)]}
        return {"ok": True, "is_swap": False, "running": []}

    async def _probe_version(self, root: str, headers: dict) -> dict:
        try:
            data = await self._get_json(f"{root}/version", headers)
            return {"ok": True, "version": str((data or {}).get("version") or "")}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    async def _probe_models(self, v1: str, headers: dict) -> dict:
        """/v1/models — ids plus whatever per-model metadata the server adds
        (vLLM max_model_len; llama.cpp meta: trained ctx, params, file size;
        router-mode status)."""
        try:
            data = await self._get_json(f"{v1}/models", headers)
            entries = [m for m in (data.get("data") or []) if isinstance(m, dict)]
            details = []
            for m in entries:
                if not m.get("id"):
                    continue
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                st = m.get("status")
                details.append({
                    "id": m["id"],
                    "max_model_len": m.get("max_model_len"),
                    "n_ctx_train": meta.get("n_ctx_train"),
                    "n_params": meta.get("n_params"),
                    "size": meta.get("size"),
                    "status": (st.get("value") if isinstance(st, dict) else st) or "",
                    "parent": m.get("parent") or ""})
            return {"ok": True, "models": [d["id"] for d in details],
                    "details": details}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    async def _probe_metrics(self, root: str, headers: dict, flavor: str) -> dict:
        """Prometheus /metrics (vLLM always; llama.cpp with --metrics) — parsed
        and normalized (pool/prom.py) plus the raw engine series for reference."""
        from . import prom
        try:
            r = await self.client.get(f"{root}/metrics", headers=headers,
                                      timeout=_QUICK_TIMEOUT)
            if r.status_code >= 400:
                hint = (" — start llama-server with --metrics to enable"
                        if flavor == "llamacpp" else "")
                return {"ok": False, "error": f"HTTP {r.status_code}{hint}"}
            parsed = prom.parse(r.text or "")
            prefix = "llamacpp:" if flavor == "llamacpp" else "vllm:"
            raw = {}
            for name, samples in parsed.items():
                if not name.startswith(prefix) or name.endswith("_bucket") \
                        or name.endswith("_created"):
                    continue
                raw[name] = round(sum(v for _, v in samples), 4)
            return {"ok": True, "summary": prom.summarize(flavor, parsed),
                    "metrics": dict(sorted(raw.items()))}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e)}

    # -- unified diagnostics -------------------------------------------------------

    async def diagnostics(self, backend: str) -> dict:
        """Probe the endpoints a backend's flavor supports and return a structured
        report. Read-only; per-probe failures are captured, not raised."""
        s = self._server(backend)
        cfg = s.config
        flavor = cfg.effective_flavor
        root = self._root(cfg.url)
        v1 = self._v1(cfg.url)
        headers = self._headers(cfg.api_key)
        report: dict = {"backend": backend, "flavor": flavor, "url": cfg.url,
                        "root": root}
        for probe in _PROBES.get(flavor, ["models"]):
            if probe == "health":
                report["health"] = await self._probe_health(root, headers)
            elif probe == "props":
                report["props"] = await self._probe_props(root, headers)
            elif probe == "slots":
                report["slots"] = await self._probe_slots(root, headers)
            elif probe == "models":
                report["models"] = await self._probe_models(v1, headers)
            elif probe == "metrics":
                report["metrics"] = await self._probe_metrics(root, headers, flavor)
            elif probe == "swap":
                report["swap"] = await self._probe_swap(root, headers)
            elif probe == "version":
                report["version"] = await self._probe_version(root, headers)
        return report
