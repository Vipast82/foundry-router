"""Wire-protocol adapters: Ollama, OpenAI-compatible, Anthropic-compatible.

Everything above this layer speaks one canonical format (chosen to be
OpenAI/Ollama-shaped, since two of the three protocols already are):

  message      = {"role": ..., "content": str,
                  "tool_calls": [{"id", "type": "function",
                                  "function": {"name", "arguments": dict}}]?,   # assistant
                  "tool_call_id": str?, "name": str?}                            # role=tool
  tool spec    = {"type": "function",
                  "function": {"name", "description", "parameters": <JSONSchema>}}
  ChatResult   = normalized response (content + parsed tool calls + token usage)

Adapters translate at the edge. The Anthropic adapter does the real work
(system extraction, tool_use/tool_result blocks); the other two are mostly
passthrough plus small shape fixes (OpenAI stringifies tool arguments).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","name","arguments":dict}]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Model reasoning, kept OUT of content: Ollama's native message.thinking
    # when the backend separates it, plus any literal <think> blocks scrubbed
    # from content at the dispatch layer (they leak to users otherwise).
    thinking: str = ""
    # Ollama timing fields (nanoseconds), kept SEPARATE on purpose: eval_ is
    # warm-state inference time (the only latency signal safe to score);
    # load_ is cold model-load time (a shared 32GB pool means most workers
    # aren't resident, so this is contention noise, never a quality signal).
    # Zero for non-Ollama backends, which don't report them.
    eval_duration_ns: int = 0
    load_duration_ns: int = 0
    prompt_eval_duration_ns: int = 0
    # Why generation stopped: "stop" (natural end / stop token), "length" (hit
    # the max-token cap — a TRUNCATED reply, which is why a client like Cline
    # then asks to "continue"), "tool_calls", etc. Normalized across backends
    # (Ollama done_reason / OpenAI finish_reason / Anthropic stop_reason).
    finish_reason: str = ""
    # Speculative-decoding acceptance (llama.cpp with a draft model): draft_n
    # tokens were proposed by the draft model, draft_n_accepted were verified
    # and kept by the target. accepted/proposed is the acceptance rate — THE
    # signal for tuning --spec-draft-n-max (a low rate means the draft depth is
    # too deep or the draft model mismatched, so speculation is wasting compute).
    # 0 when speculative decoding is off or the backend can't report it.
    draft_n: int = 0
    draft_n_accepted: int = 0
    # KV prefix-cache reuse: how many of this request's prompt tokens the server
    # served from cache instead of re-prefilling (OpenAI usage
    # prompt_tokens_details.cached_tokens; llama.cpp reports it too). cached /
    # prompt_tokens is the cache-hit rate — a direct read on whether the chat
    # template is keeping the prefix cache warm across turns. 0 when unavailable.
    cached_tokens: int = 0
    # Prompt tokens the server actually PREFILLED this call (i.e. excluding the
    # ones served from the KV prefix cache). llama.cpp reports this separately
    # as timings.prompt_n; usage.prompt_tokens is the FULL prompt. Prefill tok/s
    # must divide the processed count by prefill time — dividing the full prompt
    # would inflate the rate by the cache-hit ratio (a 90%-cached turn read 10x
    # too fast). 0 = unknown -> callers fall back to prompt_tokens.
    prefill_tokens: int = 0
    # Hidden reasoning tokens inside completion_tokens (OpenAI-style
    # usage.completion_tokens_details.reasoning_tokens — vLLM/OpenRouter report
    # it). Shows how much of the output budget thinking is eating. 0 = unknown.
    reasoning_tokens: int = 0
    # Where the duration fields came from: "server" (the backend measured them —
    # Ollama ns timings, llama.cpp `timings`), "estimated" (the router timed the
    # stream itself because the server reports nothing — vLLM/OpenRouter; the
    # prefill figure then includes network + queue time), or "" (no timing).
    timing_source: str = ""
    raw: Any = None

    def done_frame(self) -> dict:
        """The canonical streaming done-frame for this result — every telemetry
        key a protocol can report, in one place so fallbacks never drop one."""
        return {"content": "", "done": True,
                "tool_calls": [{"id": tc["id"], "name": tc["name"],
                                "arguments": tc["arguments"]}
                               for tc in self.tool_calls] or None,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "eval_duration_ns": self.eval_duration_ns,
                "load_duration_ns": self.load_duration_ns,
                "prompt_eval_duration_ns": self.prompt_eval_duration_ns,
                "draft_n": self.draft_n,
                "draft_n_accepted": self.draft_n_accepted,
                "cached_tokens": self.cached_tokens,
                "prefill_tokens": self.prefill_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "timing_source": self.timing_source,
                "finish_reason": self.finish_reason}

    @classmethod
    def from_done_frame(cls, chunk: dict, content: str = "",
                        tool_calls: Optional[list] = None) -> "ChatResult":
        """Inverse of done_frame(): rebuild a result from a stream's done frame
        (plus the content/tool calls the caller accumulated), so streamed and
        blocking calls feed telemetry through the exact same path."""
        c = chunk or {}
        return cls(content=content,
                   tool_calls=list(tool_calls if tool_calls is not None
                                   else (c.get("tool_calls") or [])),
                   prompt_tokens=int(c.get("prompt_tokens") or 0),
                   completion_tokens=int(c.get("completion_tokens") or 0),
                   eval_duration_ns=int(c.get("eval_duration_ns") or 0),
                   load_duration_ns=int(c.get("load_duration_ns") or 0),
                   prompt_eval_duration_ns=int(c.get("prompt_eval_duration_ns") or 0),
                   finish_reason=c.get("finish_reason") or "",
                   draft_n=int(c.get("draft_n") or 0),
                   draft_n_accepted=int(c.get("draft_n_accepted") or 0),
                   cached_tokens=int(c.get("cached_tokens") or 0),
                   prefill_tokens=int(c.get("prefill_tokens") or 0),
                   reasoning_tokens=int(c.get("reasoning_tokens") or 0),
                   timing_source=c.get("timing_source") or "")


class ProtocolError(Exception):
    """A backend answered but the exchange failed (HTTP error, bad payload)."""


def _parse_arguments(args: Any) -> dict:
    """Tool-call arguments arrive as a dict (Ollama/Anthropic) or a JSON string
    (OpenAI). Small local models also occasionally emit malformed JSON — treat
    that as an empty-args call rather than failing the whole request; the brain
    sees the tool result complain and can retry."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, ValueError):
            log.warning("unparseable tool arguments: %.200s", args)
            return {}
    return {}


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


# Base64 magic prefixes — clients send bare base64 with no media type, but
# Anthropic/OpenAI image blocks require one. Sniff it from the first bytes.
_IMAGE_MAGIC = (("iVBOR", "image/png"), ("/9j/", "image/jpeg"),
                ("R0lGOD", "image/gif"), ("UklGR", "image/webp"))


def _image_media_type(b64: str) -> str:
    for prefix, media_type in _IMAGE_MAGIC:
        if b64.startswith(prefix):
            return media_type
    return "image/jpeg"  # most common fallback; backends tolerate mismatches


def _llamacpp_timings(timings: Any) -> dict:
    """Normalize llama.cpp's `timings` object into the same ns fields Ollama
    reports, so decode/prefill tok/s come out backend-agnostic. llama.cpp gives
    prompt_ms/predicted_ms (+ *_n token counts) on its OpenAI-compat responses
    and stream tails; anything missing stays 0. vLLM/OpenAI send no timings ->
    all zeros (those backends simply have no per-request speed telemetry)."""
    t = timings if isinstance(timings, dict) else {}

    def _ms_ns(key):
        v = t.get(key)
        return int(float(v) * 1e6) if isinstance(v, (int, float)) else 0

    return {"prompt_eval_duration_ns": _ms_ns("prompt_ms"),
            "eval_duration_ns": _ms_ns("predicted_ms"),
            "prompt_n": int(t.get("prompt_n") or 0),
            "predicted_n": int(t.get("predicted_n") or 0),
            # Speculative-decoding counters — present only when a draft model is
            # loaded; absent (0) otherwise. draft_n = tokens proposed by the
            # draft, draft_n_accepted = tokens the target verified and kept.
            "draft_n": int(t.get("draft_n") or 0),
            "draft_n_accepted": int(t.get("draft_n_accepted") or 0),
            # Prompt tokens reused from the KV cache (not re-prefilled). Newer
            # llama-server builds report it here even when usage carries no
            # prompt_tokens_details, so it's the cache-hit fallback.
            "cache_n": int(t.get("cache_n") or 0)}


def _reasoning_tokens(usage: Any) -> int:
    """Hidden reasoning tokens counted inside completion_tokens — OpenAI-style
    usage.completion_tokens_details.reasoning_tokens (vLLM, OpenRouter)."""
    u = usage if isinstance(usage, dict) else {}
    d = u.get("completion_tokens_details")
    if isinstance(d, dict):
        return int(d.get("reasoning_tokens") or 0)
    return 0


def _cached_tokens(usage: Any) -> int:
    """Prompt tokens the server served from its KV prefix cache instead of
    re-prefilling — OpenAI's usage.prompt_tokens_details.cached_tokens, which
    llama.cpp populates too. 0 when the backend doesn't report it."""
    u = usage if isinstance(usage, dict) else {}
    details = u.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details.get("cached_tokens") or 0)
    # Some builds surface it flat rather than nested.
    return int(u.get("cached_tokens") or 0)


class BaseProtocol:
    """One instance per backend. Owns no connection state beyond the shared
    httpx client passed in (connection pooling lives there)."""

    def __init__(self, url: str, api_key: Optional[str], client: httpx.AsyncClient,
                 flavor: Optional[str] = None, meridian_profile: Optional[str] = None):
        self.url = url.rstrip("/")
        self.api_key = api_key or None
        self.client = client
        # Server-software flavor (ollama / llamacpp / unsloth / vllm / openai),
        # from the backend config. Lets a protocol tailor the wire body to the
        # actual server — e.g. only send non-standard sampling controls to the
        # local runners that understand them, not to a strict OpenAI endpoint.
        self.flavor = flavor or None
        # Meridian routing profile — sent as x-meridian-profile so a backend can
        # pin its calls to a specific Claude account (anthropic path only).
        self.meridian_profile = meridian_profile or None

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def chat(self, model: str, messages: list[dict], tools: Optional[list[dict]] = None,
                   options: Optional[dict] = None, keep_alive: Any = None,
                   max_tokens: int = 4096, think: Any = None, fmt: Any = None) -> ChatResult:
        raise NotImplementedError

    async def chat_stream(self, model: str, messages: list[dict], tools=None,
                          options: Optional[dict] = None, keep_alive=None,
                          think=None, max_tokens=None, fmt=None) -> AsyncIterator[dict]:
        """Token-level streaming fallback. Concrete protocols override this with
        real SSE streaming; this degraded default (one content chunk + a done
        frame) forwards EVERY capability param to chat() so a fallback still
        honors tools/think/max_tokens/fmt and surfaces tool calls + thinking."""
        result = await self.chat(model, messages, tools=tools, options=options,
                                 keep_alive=keep_alive,
                                 max_tokens=max_tokens or 4096, think=think, fmt=fmt)
        yield {"content": result.content, "done": False,
               "thinking": result.thinking or ""}
        yield result.done_frame()


# --------------------------------------------------------------------------- #
# Ollama                                                                      #
# --------------------------------------------------------------------------- #

def context_length_from_model_info(info: dict) -> Optional[int]:
    """Ollama's /api/show model_info carries the trained context window under an
    architecture-prefixed key — qwen2.context_length, llama.context_length,
    etc. — so match by suffix rather than a hardcoded name."""
    if not isinstance(info, dict):
        return None
    for key, val in info.items():
        if key.endswith(".context_length") and isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


class OllamaProtocol(BaseProtocol):
    async def list_models(self) -> list[str]:
        r = await self.client.get(f"{self.url}/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def loaded_models(self) -> list[str]:
        """Models currently resident in VRAM (Ollama /api/ps) — lets routing
        prefer an already-loaded model over one that would force an unload/
        reload, and lets the UI show what's warm."""
        r = await self.client.get(f"{self.url}/api/ps", timeout=10)
        r.raise_for_status()
        return [m.get("name") or m.get("model") for m in r.json().get("models", [])
                if m.get("name") or m.get("model")]

    async def loaded_models_detail(self) -> list[dict]:
        """Per-model VRAM residency from /api/ps: name + size_vram (bytes in
        GPU memory), total size, and expiry. What the Live view needs to show
        how much VRAM each warm model is holding."""
        r = await self.client.get(f"{self.url}/api/ps", timeout=10)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = m.get("name") or m.get("model")
            if not name:
                continue
            out.append({"model": name,
                        "size_vram": m.get("size_vram") or 0,
                        "size": m.get("size") or 0,
                        "expires_at": m.get("expires_at")})
        return out

    async def show_context_length(self, model: str) -> Optional[int]:
        """The model's real trained context window from its GGUF metadata —
        authoritative per-model ground truth Ollama already exposes (and which
        OpenRouter, being cloud-only, never has for local pulls)."""
        r = await self.client.post(f"{self.url}/api/show",
                                   json={"model": model}, timeout=15)
        if r.status_code >= 400:
            raise ProtocolError(
                f"ollama {self.url} /api/show HTTP {r.status_code}: {r.text[:200]}")
        return context_length_from_model_info((r.json() or {}).get("model_info") or {})

    async def show_capabilities(self, model: str) -> list[str]:
        """The model's declared capabilities from /api/show — modern Ollama lists
        e.g. 'completion', 'tools', 'vision', 'thinking', 'insert', 'embedding'.
        Direct API ground truth, used to auto-tag vision and advertise real
        capabilities to clients (AnythingLLM / Open WebUI gate features on them)."""
        r = await self.client.post(f"{self.url}/api/show",
                                   json={"model": model}, timeout=15)
        if r.status_code >= 400:
            raise ProtocolError(
                f"ollama {self.url} /api/show HTTP {r.status_code}: {r.text[:200]}")
        caps = (r.json() or {}).get("capabilities") or []
        return [str(c) for c in caps] if isinstance(caps, list) else []

    def _payload(self, model, messages, tools, options, keep_alive, stream,
                 think=None, max_tokens=None, fmt=None):
        # Strip canonical-format fields Ollama doesn't know; keep tool_calls
        # (it accepts them on assistant messages) but drop OpenAI-style ids.
        msgs = []
        for m in messages:
            mm = {"role": m["role"], "content": m.get("content") or ""}
            if m.get("tool_calls"):
                mm["tool_calls"] = [
                    {"function": {"name": tc["function"]["name"],
                                  "arguments": _parse_arguments(tc["function"].get("arguments"))}}
                    for tc in m["tool_calls"]
                ]
            if m["role"] == "tool" and m.get("name"):
                mm["tool_name"] = m["name"]
            if m.get("images"):  # Ollama-native multimodal field, passthrough
                mm["images"] = m["images"]
            msgs.append(mm)
        payload: dict = {"model": model, "messages": msgs, "stream": stream}
        if tools:
            payload["tools"] = tools
        # Cap output tokens via Ollama's num_predict. Without this a generation
        # runs until the context fills (or the model stops on its own) — a real
        # runaway/timeout risk with heavy reasoning. Client-sent num_predict
        # wins; otherwise the dispatch layer's max_tokens becomes the ceiling.
        opts = dict(options or {})
        if max_tokens and "num_predict" not in opts:
            opts["num_predict"] = int(max_tokens)
        if opts:
            payload["options"] = opts
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        # Reasoning effort: Ollama's top-level `think` field. A level string
        # ("low"/"medium"/"high"/"xhigh") or a bool (True/False). The dispatch
        # layer only passes it for models that support thinking.
        if think is not None:
            payload["think"] = think
        # Structured output: Ollama's top-level `format` — "json" or a JSON
        # schema object. Constrains the model to valid JSON (tool/data reliability).
        if fmt is not None:
            payload["format"] = fmt
        return payload

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096, think=None, fmt=None) -> ChatResult:
        payload = self._payload(model, messages, tools, options, keep_alive,
                                stream=False, think=think, max_tokens=max_tokens, fmt=fmt)
        r = await self.client.post(f"{self.url}/api/chat", json=payload)
        if r.status_code >= 400:
            raise ProtocolError(f"ollama {self.url} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data.get("message", {}) or {}
        tool_calls = [
            {"id": _new_id(), "name": tc["function"]["name"],
             "arguments": _parse_arguments(tc["function"].get("arguments"))}
            for tc in (msg.get("tool_calls") or [])
        ]
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            prompt_tokens=data.get("prompt_eval_count") or 0,
            completion_tokens=data.get("eval_count") or 0,
            # Reasoning models served with think-parsing enabled put their
            # reasoning here, not in content — dropping it silently is fine
            # for correctness but wasteful for narration; carry it along.
            thinking=msg.get("thinking") or "",
            # Warm-inference vs cold-load timing, kept apart for scoring.
            eval_duration_ns=data.get("eval_duration") or 0,
            load_duration_ns=data.get("load_duration") or 0,
            prompt_eval_duration_ns=data.get("prompt_eval_duration") or 0,
            finish_reason=data.get("done_reason") or "",
            timing_source="server" if data.get("eval_duration") else "",
            raw=data,
        )

    async def chat_stream(self, model, messages, tools=None, options=None,
                          keep_alive=None, think=None, max_tokens=None,
                          fmt=None) -> AsyncIterator[dict]:
        """Token-level streaming. Now carries tools through and surfaces
        tool_calls + thinking per chunk, so direct-dispatch can stream a coding
        client's turn live — each chunk is real proof the backend is generating,
        and it resets the read timeout (no total-time wall)."""
        payload = self._payload(model, messages, tools, options, keep_alive,
                                stream=True, think=think, max_tokens=max_tokens, fmt=fmt)
        async with self.client.stream("POST", f"{self.url}/api/chat", json=payload) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise ProtocolError(f"ollama {self.url} HTTP {r.status_code}: {body[:300]!r}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = data.get("message") or {}
                tool_calls = [
                    {"id": _new_id(), "name": tc["function"]["name"],
                     "arguments": _parse_arguments(tc["function"].get("arguments"))}
                    for tc in (msg.get("tool_calls") or [])
                ] or None
                if data.get("done"):
                    yield {"content": "", "done": True, "tool_calls": tool_calls,
                           "prompt_tokens": data.get("prompt_eval_count") or 0,
                           "completion_tokens": data.get("eval_count") or 0,
                           "eval_duration_ns": data.get("eval_duration") or 0,
                           "load_duration_ns": data.get("load_duration") or 0,
                           "prompt_eval_duration_ns": data.get("prompt_eval_duration") or 0,
                           "timing_source": "server" if data.get("eval_duration") else "",
                           "finish_reason": data.get("done_reason") or ""}
                else:
                    yield {"content": msg.get("content") or "", "done": False,
                           "tool_calls": tool_calls,
                           "thinking": msg.get("thinking") or ""}


# --------------------------------------------------------------------------- #
# OpenAI-compatible (llama.cpp, vLLM, Unsloth, OpenRouter, LiteLLM)           #
# --------------------------------------------------------------------------- #

# Output-budget fitting. vLLM (and strict OpenAI) REJECT a request whose prompt
# + max_tokens exceeds the model's context instead of truncating the reply, so a
# long conversation plus the router's worker_max_tokens (8192) 400s even though
# the prompt itself fits. The error states both numbers; parse them and retry
# once with the output budget that actually fits.
_CTX_MAX_RE = re.compile(r"maximum context length is (\d+)")
_CTX_INPUT_RES = (re.compile(r"\((\d+) in the messages"),
                  re.compile(r"has (\d+) input tokens"),
                  re.compile(r"resulted in (\d+) tokens"))


def _fit_max_tokens(error_text: str, requested: int) -> Optional[int]:
    """A smaller max_tokens that fits the context the backend just told us
    about, or None when the error isn't an output-budget overflow (or the
    prompt alone doesn't fit — shrinking the reply can't fix that)."""
    m = _CTX_MAX_RE.search(error_text or "")
    if not m:
        return None
    ctx = int(m.group(1))
    for rx in _CTX_INPUT_RES:
        mi = rx.search(error_text)
        if mi:
            room = ctx - int(mi.group(1)) - 16      # small safety margin
            return room if 64 <= room < int(requested or 0) else None
    return None


def _entry_loaded(entry: dict) -> bool:
    """llama-server "router mode" (multi-model) tags each /v1/models entry with
    a status; only loaded ones are resident."""
    st = entry.get("status")
    if isinstance(st, dict):
        st = st.get("value")
    return str(st or "").lower() in ("loaded", "ready", "running")


class OpenAIProtocol(BaseProtocol):
    # Standard OpenAI sampling fields — safe to send to ANY openai-dialect
    # endpoint (including strict OpenAI / OpenRouter).
    _STD_SAMPLING = ("temperature", "top_p", "presence_penalty",
                     "frequency_penalty", "seed", "stop", "logit_bias")
    # Non-standard sampling controls understood by the LOCAL runners but
    # rejected by a strict OpenAI endpoint. Each flavor gets the set its server
    # actually implements, so a mixed fleet each gets its full knob set without
    # 400ing the strict ones. Generic set (Unsloth / unknown local runner):
    _EXTRA_SAMPLING = ("top_k", "min_p", "repeat_penalty", "repetition_penalty",
                       "typical_p", "tfs_z", "mirostat", "mirostat_tau",
                       "mirostat_eta")
    # llama-server's full /v1/chat/completions extension set (tools/server
    # README): DRY + XTC anti-repetition samplers, top-n-sigma, dynamic
    # temperature, sampler ordering, grammar / json_schema constraints, slot
    # pinning, prompt caching and per-request timings.
    _LLAMACPP_SAMPLING = (
        "top_k", "min_p", "typical_p", "repeat_penalty", "repeat_last_n",
        "tfs_z", "mirostat", "mirostat_tau", "mirostat_eta",
        "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n",
        "dry_sequence_breakers", "xtc_probability", "xtc_threshold",
        "top_n_sigma", "dynatemp_range", "dynatemp_exponent", "min_keep",
        "n_keep", "n_probs", "samplers", "cache_prompt", "id_slot", "ignore_eos",
        "t_max_predict_ms", "grammar", "json_schema", "reasoning_format",
        "post_sampling_probs", "n_indent", "timings_per_token", "lora")
    # vLLM's extra sampling / generation params (OpenAI server "extra
    # parameters"): repetition controls, min_tokens, stop-token ids, guided
    # (structured) decoding, scheduling priority, prompt truncation.
    _VLLM_SAMPLING = (
        "top_k", "min_p", "repetition_penalty", "length_penalty", "min_tokens",
        "stop_token_ids", "ignore_eos", "skip_special_tokens",
        "spaces_between_special_tokens", "include_stop_str_in_output",
        "truncate_prompt_tokens", "bad_words", "allowed_token_ids", "priority",
        "prompt_logprobs", "guided_json", "guided_regex", "guided_choice",
        "guided_grammar", "guided_decoding_backend", "structured_outputs")
    _FLAVOR_SAMPLING = {"llamacpp": _LLAMACPP_SAMPLING, "vllm": _VLLM_SAMPLING,
                        "unsloth": _EXTRA_SAMPLING}
    # Same knob, different spelling per server — clients (and persona sampling
    # defaults) speak Ollama, so translate rather than silently drop.
    _SAMPLING_ALIASES = {"llamacpp": {"repetition_penalty": "repeat_penalty",
                                      "num_keep": "n_keep"},
                         "vllm": {"repeat_penalty": "repetition_penalty"}}
    _LOCAL_FLAVORS = {"llamacpp", "unsloth", "vllm"}
    _ENTRIES_TTL = 5.0          # /v1/models metadata cache (seconds)
    _SWAP_RECHECK = 60.0        # how long a "not llama-swap" verdict is trusted

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._entries: list[dict] = []
        self._entries_ts = 0.0
        self._swap_verdict: Optional[bool] = None
        self._swap_ts = 0.0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _base(self) -> str:
        # Accept both ".../v1" and bare host urls.
        return self.url if self.url.endswith("/v1") else f"{self.url}/v1"

    def _root(self) -> str:
        """Server root (no /v1) — where llama.cpp's /props, /slots, /metrics and
        vLLM's /metrics, /version live."""
        return self.url[:-3].rstrip("/") if self.url.endswith("/v1") else self.url

    @property
    def _local(self) -> bool:
        return (self.flavor or "openai") in self._LOCAL_FLAVORS

    async def list_models(self) -> list[str]:
        r = await self.client.get(f"{self._base()}/models", headers=self._headers(), timeout=15)
        r.raise_for_status()
        data = [m for m in (r.json().get("data") or []) if isinstance(m, dict)]
        # Keep the full entries: vLLM carries max_model_len, llama.cpp carries
        # meta (n_ctx_train, n_params, size) — context + size for free.
        self._entries, self._entries_ts = data, time.monotonic()
        return [m["id"] for m in data if m.get("id")]

    async def _models_entries(self) -> list[dict]:
        if time.monotonic() - self._entries_ts > self._ENTRIES_TTL or not self._entries:
            await self.list_models()
        return self._entries

    async def _get_json(self, path: str, timeout: float = 10) -> Any:
        r = await self.client.get(f"{self._root()}{path}", headers=self._headers(),
                                  timeout=timeout)
        r.raise_for_status()
        return r.json()

    async def _props(self, model: Optional[str] = None) -> dict:
        path = "/props" + (f"?model={model}" if model else "")
        data = await self._get_json(path)
        return data if isinstance(data, dict) else {}

    async def swap_running(self) -> Optional[list[dict]]:
        """llama-swap (the model-swapping proxy in front of llama-server) lists
        resident models at GET /running. Returns that list, or None when this
        isn't llama-swap. A negative verdict is cached so plain llama-server
        isn't probed for a 404 on every Live refresh."""
        if (self.flavor or "") != "llamacpp":
            return None
        now = time.monotonic()
        if self._swap_verdict is False and now - self._swap_ts < self._SWAP_RECHECK:
            return None
        try:
            data = await self._get_json("/running", timeout=5)
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("running"), list):
            self._swap_verdict, self._swap_ts = True, now
            return [r for r in data["running"] if isinstance(r, dict)]
        self._swap_verdict, self._swap_ts = False, now
        return None

    async def loaded_models_detail(self) -> list[dict]:
        """What this server is serving, for the Live VRAM table.

        * vLLM — every /v1/models entry, context = its max_model_len.
        * llama-swap — the models /running says are resident.
        * llama-server router mode — /v1/models entries whose status is loaded.
        * plain llama-server — ONE model; /props gives the SERVING context
          (per-slot n_ctx, what actually bounds a request) and slot count.
        None of these report VRAM bytes over HTTP, so size_vram=0 ("—");
        llama.cpp's /v1/models meta.size (weights file bytes) rides as `size`.

        The model NAME must be the /v1/models id — the exact string this server
        advertises and that requests (and therefore the perf registry) are keyed
        on. Deriving a prettier name from model_path instead breaks the join, so
        the whole perf row reads null even though the numbers were recorded."""
        if not self._local:
            return []
        flavor = self.flavor or "openai"
        entries: list[dict] = []
        try:
            entries = await self._models_entries()
        except Exception:
            pass
        if flavor == "vllm":
            return [{"model": e["id"], "size_vram": 0, "size": 0,
                     "context": int(e.get("max_model_len") or 0)}
                    for e in entries if e.get("id")]

        def _row(e: dict, context: int = 0, **extra) -> dict:
            meta = e.get("meta") if isinstance(e.get("meta"), dict) else {}
            return {"model": e.get("id"), "size_vram": 0,
                    "size": int(meta.get("size") or 0), "context": int(context or 0),
                    **extra}

        running = await self.swap_running()
        if running is not None:
            by_id = {e.get("id"): e for e in entries}
            return [_row(by_id.get(r.get("model")) or {"id": r.get("model")},
                         state=r.get("state") or "")
                    for r in running if r.get("model")]
        if any("status" in e for e in entries):
            loaded = [e for e in entries if _entry_loaded(e)]
            if len(loaded) != 1:
                return [_row(e) for e in loaded]
            entries = loaded
        # Plain llama-server (or a single loaded router model).
        model_entry = entries[0] if entries else {}
        n_ctx = 0
        slots = None
        try:
            data = await self._props()
            gen = data.get("default_generation_settings") or {}
            n_ctx = gen.get("n_ctx") or data.get("n_ctx") or 0
            slots = data.get("total_slots")
            if not model_entry.get("id"):
                path = data.get("model_path") or gen.get("model") or data.get("model") or ""
                model_entry = {"id": path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]}
        except Exception:
            pass
        if not model_entry.get("id"):
            return []
        extra = {"slots": int(slots)} if isinstance(slots, int) and slots > 0 else {}
        return [_row(model_entry, n_ctx, **extra)]

    async def show_context_length(self, model: str) -> Optional[int]:
        """The context a request to `model` can actually use. vLLM: the entry's
        max_model_len. llama.cpp: the SERVING per-slot n_ctx from /props when this
        process serves just that model, else the trained window from
        /v1/models meta.n_ctx_train (never /props on llama-swap — that would
        trigger a model load)."""
        if not self._local:
            return None
        entries = await self._models_entries()
        e = next((x for x in entries if x.get("id") == model), None)
        if (self.flavor or "") == "vllm":
            v = (e or {}).get("max_model_len")
            return int(v) if v else None
        if (self.flavor or "") == "llamacpp" and len(entries) <= 1 \
                and await self.swap_running() is None:
            try:
                gen = (await self._props()).get("default_generation_settings") or {}
                if gen.get("n_ctx"):
                    return int(gen["n_ctx"])
            except Exception:
                pass
        meta = (e or {}).get("meta") if isinstance((e or {}).get("meta"), dict) else {}
        return int(meta["n_ctx_train"]) if meta.get("n_ctx_train") else None

    async def show_capabilities(self, model: str) -> list[str]:
        """llama.cpp /props capabilities for a single-model server: vision/audio
        from `modalities`, tools from the chat template's declared caps (needs
        --jinja). [] when unknown — never guessed."""
        if (self.flavor or "") != "llamacpp":
            return []
        entries = await self._models_entries()
        if len(entries) > 1 or await self.swap_running() is not None:
            return []
        data = await self._props()
        caps = ["completion"]
        mods = data.get("modalities") or {}
        if mods.get("vision"):
            caps.append("vision")
        if mods.get("audio"):
            caps.append("audio")
        tcaps = data.get("chat_template_caps") or {}
        if tcaps.get("supports_tools") or tcaps.get("supports_tool_calls"):
            caps.append("tools")
        return caps

    async def server_version(self) -> str:
        """vLLM GET /version; llama.cpp /props build_info. "" when unknown."""
        try:
            if (self.flavor or "") == "vllm":
                return str((await self._get_json("/version", timeout=5) or {}).get("version") or "")
            if (self.flavor or "") == "llamacpp" and await self.swap_running() is None:
                return str((await self._props()).get("build_info") or "")
        except Exception:
            pass
        return ""

    async def server_metrics(self) -> Optional[dict]:
        """Server-wide live telemetry from the inference engine itself — KV-cache
        fill, queue depth, busy slots, lifetime throughput, preemptions, prefix
        cache and speculative acceptance — normalized across llama.cpp and vLLM
        (see pool/prom.py). llama.cpp needs `--metrics` for /metrics; its /slots
        is read too (busy/total), so something useful shows even without it.
        None for flavors with no such surface."""
        from . import prom
        flavor = self.flavor or "openai"
        if flavor not in ("llamacpp", "vllm"):
            return None
        out: dict = {"flavor": flavor}
        paths = ["/metrics"]
        running = await self.swap_running() if flavor == "llamacpp" else None
        if running:
            # llama-swap proxies each resident llama-server under /upstream/<id>.
            paths.append(f"/upstream/{running[0].get('model')}/metrics")
        out["metrics_ok"] = False
        for path in paths:
            try:
                r = await self.client.get(f"{self._root()}{path}",
                                          headers=self._headers(), timeout=4)
                if r.status_code < 400 and "#" in (r.text or ""):
                    out.update(prom.summarize(flavor, prom.parse(r.text)))
                    out["metrics_ok"] = True
                    out.pop("metrics_error", None)
                    break
                out["metrics_error"] = f"HTTP {r.status_code}"
            except Exception as e:
                out["metrics_error"] = str(e)[:120] or type(e).__name__
        if flavor == "llamacpp" and running is None:
            try:
                data = await self._get_json("/slots", timeout=4)
                slots = data if isinstance(data, list) else (data or {}).get("slots") or []
                out.update(prom.summarize_slots(slots))
            except Exception:
                pass
        if running is not None:
            out["swap_running"] = [{"model": r.get("model"), "state": r.get("state")}
                                   for r in running]
        return out

    def _translate_messages(self, messages) -> list[dict]:
        msgs = []
        for m in messages:
            mm: dict = {"role": m["role"], "content": m.get("content") or ""}
            if m.get("tool_calls"):
                mm["tool_calls"] = [
                    {"id": tc.get("id") or _new_id(), "type": "function",
                     "function": {"name": tc["function"]["name"],
                                  "arguments": json.dumps(_parse_arguments(tc["function"].get("arguments")))}}
                    for tc in m["tool_calls"]
                ]
            if m["role"] == "tool":
                mm["tool_call_id"] = m.get("tool_call_id") or _new_id()
            if m.get("images"):
                # OpenAI-style multimodal: content becomes typed parts with
                # data-URI image_url blocks.
                parts = ([{"type": "text", "text": mm["content"]}]
                         if mm["content"] else [])
                parts += [{"type": "image_url",
                           "image_url": {"url": f"data:{_image_media_type(img)};"
                                                f"base64,{img}"}}
                          for img in m["images"]]
                mm["content"] = parts
            msgs.append(mm)
        return msgs

    def _payload(self, model, messages, tools, options, max_tokens, think, fmt) -> dict:
        payload: dict = {"model": model, "messages": self._translate_messages(messages),
                         "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
        opts = options or {}
        for k in self._STD_SAMPLING:
            if k in opts:
                payload[k] = opts[k]
        # Client sent Ollama-style num_predict? Map it onto max_tokens (the
        # OpenAI ceiling), so the same client options work across backends.
        if opts.get("num_predict"):
            payload["max_tokens"] = int(opts["num_predict"])
        flavor = self.flavor or "openai"
        allowed = self._FLAVOR_SAMPLING.get(flavor, ())
        for k in allowed:
            if k in opts:
                payload[k] = opts[k]
        for src, dst in self._SAMPLING_ALIASES.get(flavor, {}).items():
            if src in opts and dst in allowed and dst not in payload:
                payload[dst] = opts[src]
        # Reasoning. Two shapes, because openai-dialect servers disagree:
        #  * A level (low/medium/high) -> OpenAI-standard `reasoning_effort`.
        #  * Explicit OFF -> there is NO reasoning_effort="off". Qwen3/DeepSeek-R1
        #    on llama.cpp / vLLM think BY DEFAULT, so to actually disable thinking
        #    (the ACT-speed path) we send the chat-template kwarg those runners
        #    honor. It's gated to local flavors — a strict OpenAI endpoint would
        #    reject it — and is harmlessly ignored by models whose template
        #    doesn't read `enable_thinking`. This gives Ollama/llama.cpp parity:
        #    `think:false` on Ollama and this here both mean "no reasoning".
        # Operator/client chat_template_kwargs (local flavors) merge underneath.
        from .. import thinking as _thinking
        norm = _thinking.normalize(think)
        local = flavor in self._LOCAL_FLAVORS
        tkw = dict(opts.get("chat_template_kwargs") or {}) \
            if local and isinstance(opts.get("chat_template_kwargs"), dict) else {}
        if norm is False and local:
            tkw["enable_thinking"] = False
        else:
            eff = _thinking.openai_reasoning_effort(think)
            if eff:
                payload["reasoning_effort"] = eff
        if tkw:
            payload["chat_template_kwargs"] = tkw
        # Structured output → OpenAI response_format. "json" = json_object; a
        # dict is a JSON schema. The OpenAI shape is json_schema: {name, schema}
        # — llama.cpp and vLLM both read the schema from json_schema.schema, so
        # a BARE schema placed there constrained nothing (silently free-form).
        # Wrap a bare schema; pass an already-wrapped {name, schema} through.
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            if isinstance(fmt.get("schema"), dict):
                js = fmt
            else:
                js = {"name": "response", "schema": fmt}
            payload["response_format"] = {"type": "json_schema", "json_schema": js}
        return payload

    async def _post_chat(self, payload: dict) -> httpx.Response:
        """POST /chat/completions, retrying ONCE with a fitted max_tokens when the
        server rejects the output budget as overflowing its context (vLLM)."""
        r = await self.client.post(f"{self._base()}/chat/completions",
                                   json=payload, headers=self._headers())
        if r.status_code == 400:
            fitted = _fit_max_tokens(r.text, payload.get("max_tokens") or 0)
            if fitted:
                log.info("openai-compat %s: max_tokens %s overflows context — "
                         "retrying with %s", self.url, payload.get("max_tokens"), fitted)
                payload["max_tokens"] = fitted
                r = await self.client.post(f"{self._base()}/chat/completions",
                                           json=payload, headers=self._headers())
        return r

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096, think=None, fmt=None) -> ChatResult:
        payload = self._payload(model, messages, tools, options, max_tokens, think, fmt)
        r = await self._post_chat(payload)
        if r.status_code >= 400:
            raise ProtocolError(f"openai-compat {self.url} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        tool_calls = [
            {"id": tc.get("id") or _new_id(), "name": tc["function"]["name"],
             "arguments": _parse_arguments(tc["function"].get("arguments"))}
            for tc in (msg.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        tm = _llamacpp_timings(data.get("timings"))
        return ChatResult(
            content=msg.get("content") or "",
            # Reasoning models on llama.cpp/vLLM separate their chain-of-thought
            # into reasoning_content (or reasoning); carry it as .thinking so it
            # reaches the client's think pane instead of being lost.
            thinking=msg.get("reasoning_content") or msg.get("reasoning") or "",
            tool_calls=tool_calls,
            prompt_tokens=(usage.get("prompt_tokens")
                           or (tm["prompt_n"] + tm["cache_n"]) or 0),
            completion_tokens=usage.get("completion_tokens") or tm["predicted_n"] or 0,
            # llama.cpp reports per-request timings; map them onto the same ns
            # fields Ollama uses so decode/prefill tok/s are computed identically.
            eval_duration_ns=tm["eval_duration_ns"],
            prompt_eval_duration_ns=tm["prompt_eval_duration_ns"],
            finish_reason=choice.get("finish_reason") or "",
            draft_n=tm["draft_n"],
            draft_n_accepted=tm["draft_n_accepted"],
            cached_tokens=_cached_tokens(usage) or tm["cache_n"],
            prefill_tokens=tm["prompt_n"],
            reasoning_tokens=_reasoning_tokens(usage),
            timing_source="server" if tm["eval_duration_ns"] else "",
            raw=data,
        )

    async def chat_stream(self, model, messages, tools=None, options=None,
                          keep_alive=None, think=None, max_tokens=None,
                          fmt=None) -> AsyncIterator[dict]:
        """Real SSE streaming for openai-dialect backends: forwards content and
        reasoning deltas live (each chunk resets the read timeout), accumulates
        index-keyed tool-call fragments, and emits tool_calls + usage on the
        final done frame — so a mixed fleet streams to Cline exactly like Ollama.

        Servers that report no per-request timings (vLLM, OpenRouter) get them
        ESTIMATED from the stream itself: prefill ≈ time to the first delta,
        decode ≈ first delta → last. Flagged timing_source="estimated" (the
        prefill part includes network + queue) so the dashboard can say so."""
        payload = self._payload(model, messages, tools, options,
                                max_tokens or 4096, think, fmt)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        t_start = time.monotonic_ns()
        t_first = t_last = 0
        for attempt in (0, 1):
            async with self.client.stream("POST", f"{self._base()}/chat/completions",
                                          json=payload, headers=self._headers()) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace")
                    fitted = (_fit_max_tokens(body, payload.get("max_tokens") or 0)
                              if r.status_code == 400 and attempt == 0 else None)
                    if fitted:
                        log.info("openai-compat %s: max_tokens %s overflows context "
                                 "— retrying stream with %s", self.url,
                                 payload.get("max_tokens"), fitted)
                        payload["max_tokens"] = fitted
                        continue
                    raise ProtocolError(f"openai-compat {self.url} HTTP {r.status_code}: {body[:300]!r}")
                frags: dict = {}          # tool-call index -> {id,name,arguments(str)}
                pt = ct = 0
                cached = reasoning_tok = 0
                finish = ""
                tm = _llamacpp_timings(None)
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        obj = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    # llama.cpp attaches its `timings` to the stream tail — keep
                    # the last one seen so decode/prefill tok/s work streaming too.
                    if obj.get("timings"):
                        tm = _llamacpp_timings(obj.get("timings"))
                    usage = obj.get("usage") or {}
                    if usage:
                        pt = usage.get("prompt_tokens") or pt
                        ct = usage.get("completion_tokens") or ct
                        cached = _cached_tokens(usage) or cached
                        reasoning_tok = _reasoning_tokens(usage) or reasoning_tok
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        finish = choices[0]["finish_reason"]
                    delta = choices[0].get("delta") or {}
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        frag = frags.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            frag["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            frag["name"] = fn["name"]
                        if fn.get("arguments"):
                            frag["arguments"] += fn["arguments"]
                    content = delta.get("content") or ""
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if content or reasoning or delta.get("tool_calls"):
                        now = time.monotonic_ns()
                        t_first = t_first or now
                        t_last = now
                    if content or reasoning:
                        yield {"content": content, "done": False, "thinking": reasoning}
                break
        tool_calls = [{"id": f["id"] or _new_id(), "name": f["name"],
                       "arguments": _parse_arguments(f["arguments"])}
                      for f in frags.values() if f["name"]] or None
        pt = pt or (tm["prompt_n"] + tm["cache_n"])
        ct = ct or tm["predicted_n"]
        cached = cached or tm["cache_n"]
        eval_ns, prompt_ns = tm["eval_duration_ns"], tm["prompt_eval_duration_ns"]
        prefill_n = tm["prompt_n"]
        source = "server" if eval_ns else ""
        if not eval_ns and t_first and ct > 1 and t_last > t_first:
            # First delta → last delta spans ct-1 inter-token gaps; scale so
            # ct / eval_ns equals the true (ct-1)/span decode rate.
            eval_ns = int((t_last - t_first) * ct / (ct - 1))
            prompt_ns = t_first - t_start
            prefill_n = max(0, pt - cached)
            source = "estimated"
        yield {"content": "", "done": True, "tool_calls": tool_calls,
               "prompt_tokens": pt,
               "completion_tokens": ct,
               "eval_duration_ns": eval_ns,
               "prompt_eval_duration_ns": prompt_ns,
               "draft_n": tm["draft_n"],
               "draft_n_accepted": tm["draft_n_accepted"],
               "cached_tokens": cached,
               "prefill_tokens": prefill_n,
               "reasoning_tokens": reasoning_tok,
               "timing_source": source,
               "finish_reason": finish}


# --------------------------------------------------------------------------- #
# Anthropic-compatible (Meridian)                                             #
# --------------------------------------------------------------------------- #

class AnthropicProtocol(BaseProtocol):
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.api_key:
            h["x-api-key"] = self.api_key
            # Some Meridian builds expect a bearer token instead; sending both
            # is harmless and saves a config knob.
            h["Authorization"] = f"Bearer {self.api_key}"
        # Pin this backend's calls to a named Meridian profile/account when set.
        if self.meridian_profile:
            h["x-meridian-profile"] = self.meridian_profile
        return h

    async def list_models(self) -> list[str]:
        # §4.3: attempt discovery first for every backend. It is NOT confirmed
        # whether a given Meridian build implements /v1/models — the caller
        # falls back to the backend's configured `models:` list if this raises.
        r = await self.client.get(f"{self.url}/v1/models", headers=self._headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or data.get("models") or []
        out = []
        for m in items:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict) and m.get("id"):
                out.append(m["id"])
        if not out:
            raise ProtocolError("model list endpoint returned no usable entries")
        return out

    def _payload(self, model, messages, tools, options, max_tokens, think, fmt) -> dict:
        system_parts: list[str] = []
        out_msgs: list[dict] = []
        for m in messages:
            role, content = m["role"], m.get("content") or ""
            if role == "system":
                system_parts.append(content)
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in m["tool_calls"]:
                    blocks.append({"type": "tool_use",
                                   "id": tc.get("id") or _new_id(),
                                   "name": tc["function"]["name"],
                                   "input": _parse_arguments(tc["function"].get("arguments"))})
                out_msgs.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                out_msgs.append({"role": "user", "content": [
                    {"type": "tool_result",
                     "tool_use_id": m.get("tool_call_id") or _new_id(),
                     "content": content}]})
            elif m.get("images"):
                # Anthropic multimodal: base64 image blocks + optional text.
                # This is what lets Claude-via-Meridian see attached photos.
                blocks = [{"type": "image",
                           "source": {"type": "base64",
                                      "media_type": _image_media_type(img),
                                      "data": img}}
                          for img in m["images"]]
                if content:
                    blocks.append({"type": "text", "text": content})
                out_msgs.append({"role": role, "content": blocks})
            else:
                out_msgs.append({"role": role, "content": content})

        # Structured output: the Anthropic Messages API has no response_format,
        # so honor `fmt` as a system instruction (best-effort parity with the
        # Ollama/OpenAI JSON modes). A schema is included so Claude sees the shape.
        if fmt:
            if fmt == "json":
                nudge = ("Respond with a single valid JSON value and nothing "
                         "else — no prose, no markdown fences.")
            else:
                schema = fmt if isinstance(fmt, str) else json.dumps(fmt)
                nudge = ("Respond with a single valid JSON value and nothing else "
                         "(no prose, no markdown fences) that conforms to this "
                         f"JSON schema:\n{schema}")
            system_parts.append(nudge)

        payload: dict = {"model": model, "messages": out_msgs, "max_tokens": max_tokens}
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                for t in tools
            ]
        # Extended thinking (Claude via Meridian): a level -> budget_tokens block.
        # Anthropic forbids a custom temperature while thinking is enabled and
        # requires max_tokens > budget_tokens, so this both raises max_tokens and
        # skips the temperature override below.
        from .. import thinking as _thinking
        think_block = _thinking.claude_thinking(think, max_tokens)
        if think_block is not None:
            block, payload["max_tokens"] = think_block
            payload["thinking"] = block
        elif options and "temperature" in options:
            payload["temperature"] = options["temperature"]
        return payload

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096, think=None, fmt=None) -> ChatResult:
        payload = self._payload(model, messages, tools, options, max_tokens, think, fmt)
        r = await self.client.post(f"{self.url}/v1/messages",
                                   json=payload, headers=self._headers())
        if r.status_code >= 400:
            raise ProtocolError(f"anthropic-compat {self.url} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        content_text = ""
        thinking_text = ""
        tool_calls = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                content_text += block.get("text") or ""
            elif block.get("type") == "thinking":
                # Extended-thinking summary block: surface it as reasoning
                # (kept OUT of content) so clients render it in their think pane.
                thinking_text += block.get("thinking") or ""
            elif block.get("type") == "tool_use":
                tool_calls.append({"id": block.get("id") or _new_id(),
                                   "name": block.get("name"),
                                   "arguments": block.get("input") or {}})
        usage = data.get("usage") or {}
        # Anthropic splits prompt tokens three ways and `input_tokens` is ONLY the
        # fresh, uncached portion — with prompt caching on (as Meridian uses), that
        # can be a couple of tokens while the real context sits in cache_read. Sum
        # all three for the true context size, and surface cache_read as the KV
        # cache hit so the cache-hit % lights up for Claude the same as for local.
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_create = usage.get("cache_creation_input_tokens") or 0
        prompt_tokens = (usage.get("input_tokens") or 0) + cache_read + cache_create
        return ChatResult(
            content=content_text,
            thinking=thinking_text,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=usage.get("output_tokens") or 0,
            cached_tokens=cache_read,
            # Normalize Anthropic's "max_tokens" onto "length" so a truncated
            # reply reads the same across every backend.
            finish_reason=("length" if data.get("stop_reason") == "max_tokens"
                           else data.get("stop_reason") or ""),
            raw=data,
        )

    async def chat_stream(self, model, messages, tools=None, options=None,
                          keep_alive=None, think=None, max_tokens=None,
                          fmt=None) -> AsyncIterator[dict]:
        """Real SSE streaming for Claude via Meridian: forwards text and
        extended-thinking deltas live, accumulates tool_use input JSON by block
        index, and emits tool_calls + usage on the done frame — so a Claude
        backend streams to a client with the same fidelity as a local one."""
        payload = self._payload(model, messages, tools, options,
                                max_tokens or 4096, think, fmt)
        payload["stream"] = True
        blocks: dict = {}         # content-block index -> {type,name,id,json(str)}
        pt = ct = cached = 0
        finish = ""
        async with self.client.stream("POST", f"{self.url}/v1/messages",
                                      json=payload, headers=self._headers()) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise ProtocolError(f"anthropic-compat {self.url} HTTP {r.status_code}: {body[:300]!r}")
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "message_start":
                    u = ((ev.get("message") or {}).get("usage") or {})
                    cr = u.get("cache_read_input_tokens") or 0
                    cc = u.get("cache_creation_input_tokens") or 0
                    pt = (u.get("input_tokens") or 0) + cr + cc or pt
                    cached = cr or cached
                elif etype == "content_block_start":
                    cb = ev.get("content_block") or {}
                    blocks[ev.get("index")] = {"type": cb.get("type"),
                                               "name": cb.get("name"),
                                               "id": cb.get("id"), "json": ""}
                elif etype == "content_block_delta":
                    d = ev.get("delta") or {}
                    dt = d.get("type")
                    if dt == "text_delta":
                        if d.get("text"):
                            yield {"content": d["text"], "done": False}
                    elif dt == "thinking_delta":
                        if d.get("thinking"):
                            yield {"content": "", "done": False, "thinking": d["thinking"]}
                    elif dt == "input_json_delta":
                        blk = blocks.get(ev.get("index"))
                        if blk is not None:
                            blk["json"] += d.get("partial_json") or ""
                elif etype == "message_delta":
                    ct = (ev.get("usage") or {}).get("output_tokens") or ct
                    sr = (ev.get("delta") or {}).get("stop_reason")
                    if sr:
                        finish = "length" if sr == "max_tokens" else sr
                elif etype == "message_stop":
                    break
        tool_calls = [{"id": b.get("id") or _new_id(), "name": b.get("name"),
                       "arguments": _parse_arguments(b.get("json") or "{}")}
                      for b in blocks.values() if b.get("type") == "tool_use"] or None
        yield {"content": "", "done": True, "tool_calls": tool_calls,
               "prompt_tokens": pt, "completion_tokens": ct,
               "cached_tokens": cached, "finish_reason": finish}


PROTOCOLS = {
    "ollama": OllamaProtocol,
    "openai-compatible": OpenAIProtocol,
    "anthropic-compatible": AnthropicProtocol,
}


def make_protocol(backend_type: str, url: str, api_key: Optional[str],
                  client: httpx.AsyncClient,
                  flavor: Optional[str] = None,
                  meridian_profile: Optional[str] = None) -> BaseProtocol:
    try:
        cls = PROTOCOLS[backend_type]
    except KeyError:
        raise ValueError(f"unknown backend type {backend_type!r}") from None
    return cls(url, api_key, client, flavor=flavor, meridian_profile=meridian_profile)
