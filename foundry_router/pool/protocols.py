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

    def __init__(self, url: str, api_key: Optional[str], client: httpx.AsyncClient):
        self.url = url.rstrip("/")
        self.api_key = api_key or None
        self.client = client

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def chat(self, model: str, messages: list[dict], tools: Optional[list[dict]] = None,
                   options: Optional[dict] = None, keep_alive: Any = None,
                   max_tokens: int = 4096) -> ChatResult:
        raise NotImplementedError

    async def chat_stream(self, model: str, messages: list[dict],
                          options: Optional[dict] = None) -> AsyncIterator[dict]:
        """Token-level streaming (no tools) — used only for the raw-model
        passthrough path. Default implementation degrades to one chunk."""
        result = await self.chat(model, messages, options=options)
        yield {"content": result.content, "done": False}
        yield {"content": "", "done": True,
               "prompt_tokens": result.prompt_tokens,
               "completion_tokens": result.completion_tokens}


# --------------------------------------------------------------------------- #
# Ollama                                                                      #
# --------------------------------------------------------------------------- #

class OllamaProtocol(BaseProtocol):
    async def list_models(self) -> list[str]:
        r = await self.client.get(f"{self.url}/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def _payload(self, model, messages, tools, options, keep_alive, stream):
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
        if options:
            payload["options"] = options
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        return payload

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096) -> ChatResult:
        payload = self._payload(model, messages, tools, options, keep_alive, stream=False)
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
            raw=data,
        )

    async def chat_stream(self, model, messages, options=None) -> AsyncIterator[dict]:
        payload = self._payload(model, messages, None, options, None, stream=True)
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
                if data.get("done"):
                    yield {"content": "", "done": True,
                           "prompt_tokens": data.get("prompt_eval_count") or 0,
                           "completion_tokens": data.get("eval_count") or 0}
                else:
                    yield {"content": (data.get("message") or {}).get("content") or "",
                           "done": False}


# --------------------------------------------------------------------------- #
# OpenAI-compatible (OpenRouter, LiteLLM)                                     #
# --------------------------------------------------------------------------- #

class OpenAIProtocol(BaseProtocol):
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

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096) -> ChatResult:
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
        payload: dict = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
        for k in ("temperature", "top_p"):
            if options and k in options:
                payload[k] = options[k]
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
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            raw=data,
        )


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

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096) -> ChatResult:
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
        if options and "temperature" in options:
            payload["temperature"] = options["temperature"]

        r = await self.client.post(f"{self.url}/v1/messages",
                                   json=payload, headers=self._headers())
        if r.status_code >= 400:
            raise ProtocolError(f"anthropic-compat {self.url} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        content_text = ""
        tool_calls = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                content_text += block.get("text") or ""
            elif block.get("type") == "tool_use":
                tool_calls.append({"id": block.get("id") or _new_id(),
                                   "name": block.get("name"),
                                   "arguments": block.get("input") or {}})
        usage = data.get("usage") or {}
        return ChatResult(
            content=content_text,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("input_tokens") or 0,
            completion_tokens=usage.get("output_tokens") or 0,
            raw=data,
        )


PROTOCOLS = {
    "ollama": OllamaProtocol,
    "openai-compatible": OpenAIProtocol,
    "anthropic-compatible": AnthropicProtocol,
}


def make_protocol(backend_type: str, url: str, api_key: Optional[str],
                  client: httpx.AsyncClient) -> BaseProtocol:
    try:
        cls = PROTOCOLS[backend_type]
    except KeyError:
        raise ValueError(f"unknown backend type {backend_type!r}") from None
    return cls(url, api_key, client)
