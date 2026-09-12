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
    raw: Any = None


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


class BaseProtocol:
    """One instance per backend. Owns no connection state beyond the shared
    httpx client passed in (connection pooling lives there)."""

    def __init__(self, url: str, api_key: Optional[str], client: httpx.AsyncClient,
                 flavor: Optional[str] = None):
        self.url = url.rstrip("/")
        self.api_key = api_key or None
        self.client = client
        # Server-software flavor (ollama / llamacpp / unsloth / vllm / openai),
        # from the backend config. Lets a protocol tailor the wire body to the
        # actual server — e.g. only send non-standard sampling controls to the
        # local runners that understand them, not to a strict OpenAI endpoint.
        self.flavor = flavor or None

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
        yield {"content": "", "done": True,
               "tool_calls": [{"id": tc["id"], "name": tc["name"],
                               "arguments": tc["arguments"]} for tc in result.tool_calls] or None,
               "prompt_tokens": result.prompt_tokens,
               "completion_tokens": result.completion_tokens,
               "eval_duration_ns": result.eval_duration_ns,
               "load_duration_ns": result.load_duration_ns}


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
                           "load_duration_ns": data.get("load_duration") or 0}
                else:
                    yield {"content": msg.get("content") or "", "done": False,
                           "tool_calls": tool_calls,
                           "thinking": msg.get("thinking") or ""}


# --------------------------------------------------------------------------- #
# OpenAI-compatible (OpenRouter, LiteLLM)                                     #
# --------------------------------------------------------------------------- #

class OpenAIProtocol(BaseProtocol):
    # Standard OpenAI sampling fields — safe to send to ANY openai-dialect
    # endpoint (including strict OpenAI / OpenRouter).
    _STD_SAMPLING = ("temperature", "top_p", "presence_penalty",
                     "frequency_penalty", "seed", "stop")
    # Non-standard sampling controls understood by the LOCAL runners
    # (llama.cpp / Unsloth / vLLM) but rejected by a strict OpenAI endpoint —
    # forwarded only for those flavors so a mixed fleet each gets its full knob
    # set without 400ing the strict ones.
    _EXTRA_SAMPLING = ("top_k", "min_p", "repeat_penalty", "repetition_penalty",
                       "typical_p", "tfs_z", "mirostat", "mirostat_tau",
                       "mirostat_eta")
    _LOCAL_FLAVORS = {"llamacpp", "unsloth", "vllm"}

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _base(self) -> str:
        # Accept both ".../v1" and bare host urls.
        return self.url if self.url.endswith("/v1") else f"{self.url}/v1"

    async def list_models(self) -> list[str]:
        r = await self.client.get(f"{self._base()}/models", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

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
        if (self.flavor or "openai") in self._LOCAL_FLAVORS:
            for k in self._EXTRA_SAMPLING:
                if k in opts:
                    payload[k] = opts[k]
        # Reasoning. Two shapes, because openai-dialect servers disagree:
        #  * A level (low/medium/high) -> OpenAI-standard `reasoning_effort`.
        #  * Explicit OFF -> there is NO reasoning_effort="off". Qwen3/DeepSeek-R1
        #    on llama.cpp / vLLM think BY DEFAULT, so to actually disable thinking
        #    (the ACT-speed path) we send the chat-template kwarg those runners
        #    honor. It's gated to local flavors — a strict OpenAI endpoint would
        #    reject it — and is harmlessly ignored by models whose template
        #    doesn't read `enable_thinking`. This gives Ollama/llama.cpp parity:
        #    `think:false` on Ollama and this here both mean "no reasoning".
        from .. import thinking as _thinking
        norm = _thinking.normalize(think)
        local = (self.flavor or "openai") in self._LOCAL_FLAVORS
        if norm is False and local:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            eff = _thinking.openai_reasoning_effort(think)
            if eff:
                payload["reasoning_effort"] = eff
        # Structured output → OpenAI response_format. "json" = json_object; a
        # dict is treated as a json_schema; a string schema is passed through.
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            payload["response_format"] = {"type": "json_schema", "json_schema": fmt}
        return payload

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096, think=None, fmt=None) -> ChatResult:
        payload = self._payload(model, messages, tools, options, max_tokens, think, fmt)
        r = await self.client.post(f"{self._base()}/chat/completions",
                                   json=payload, headers=self._headers())
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
        return ChatResult(
            content=msg.get("content") or "",
            # Reasoning models on llama.cpp/vLLM separate their chain-of-thought
            # into reasoning_content (or reasoning); carry it as .thinking so it
            # reaches the client's think pane instead of being lost.
            thinking=msg.get("reasoning_content") or msg.get("reasoning") or "",
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            raw=data,
        )

    async def chat_stream(self, model, messages, tools=None, options=None,
                          keep_alive=None, think=None, max_tokens=None,
                          fmt=None) -> AsyncIterator[dict]:
        """Real SSE streaming for openai-dialect backends: forwards content and
        reasoning deltas live (each chunk resets the read timeout), accumulates
        index-keyed tool-call fragments, and emits tool_calls + usage on the
        final done frame — so a mixed fleet streams to Cline exactly like Ollama."""
        payload = self._payload(model, messages, tools, options,
                                max_tokens or 4096, think, fmt)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        async with self.client.stream("POST", f"{self._base()}/chat/completions",
                                      json=payload, headers=self._headers()) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise ProtocolError(f"openai-compat {self.url} HTTP {r.status_code}: {body[:300]!r}")
            frags: dict = {}          # tool-call index -> {id,name,arguments(str)}
            pt = ct = 0
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
                usage = obj.get("usage") or {}
                if usage:
                    pt = usage.get("prompt_tokens") or pt
                    ct = usage.get("completion_tokens") or ct
                choices = obj.get("choices") or []
                if not choices:
                    continue
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
                if content or reasoning:
                    yield {"content": content, "done": False, "thinking": reasoning}
            tool_calls = [{"id": f["id"] or _new_id(), "name": f["name"],
                           "arguments": _parse_arguments(f["arguments"])}
                          for f in frags.values() if f["name"]] or None
            yield {"content": "", "done": True, "tool_calls": tool_calls,
                   "prompt_tokens": pt, "completion_tokens": ct}


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
        return ChatResult(
            content=content_text,
            thinking=thinking_text,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("input_tokens") or 0,
            completion_tokens=usage.get("output_tokens") or 0,
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
        pt = ct = 0
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
                    pt = ((ev.get("message") or {}).get("usage") or {}).get("input_tokens") or pt
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
                elif etype == "message_stop":
                    break
        tool_calls = [{"id": b.get("id") or _new_id(), "name": b.get("name"),
                       "arguments": _parse_arguments(b.get("json") or "{}")}
                      for b in blocks.values() if b.get("type") == "tool_use"] or None
        yield {"content": "", "done": True, "tool_calls": tool_calls,
               "prompt_tokens": pt, "completion_tokens": ct}


PROTOCOLS = {
    "ollama": OllamaProtocol,
    "openai-compatible": OpenAIProtocol,
    "anthropic-compatible": AnthropicProtocol,
}


def make_protocol(backend_type: str, url: str, api_key: Optional[str],
                  client: httpx.AsyncClient,
                  flavor: Optional[str] = None) -> BaseProtocol:
    try:
        cls = PROTOCOLS[backend_type]
    except KeyError:
        raise ValueError(f"unknown backend type {backend_type!r}") from None
    return cls(url, api_key, client, flavor=flavor)
