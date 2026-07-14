"""Ollama wire-format builders (design doc §4.1: the facade is pure
translation — no routing logic lives here or in ollama_api.py).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterator, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def chat_chunk(model: str, content: str = "", done: bool = False,
               tool_calls: Optional[list] = None, stats: Optional[dict] = None,
               thinking: Optional[str] = None) -> bytes:
    """`thinking` is Ollama's NATIVE reasoning field on the message object —
    a real, separate field alongside content (confirmed from a raw qwen3.6
    response). Clients render it as a collapsible thought panel; literal
    <think> tags glued into content render as ugly raw text (found live)."""
    msg: dict = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    if tool_calls:
        msg["tool_calls"] = tool_calls
    obj: dict = {"model": model, "created_at": now_iso(), "message": msg, "done": done}
    if done:
        obj["done_reason"] = "stop"
        obj.update(_stats(stats))
    return _line(obj)


def generate_chunk(model: str, response: str = "", done: bool = False,
                   stats: Optional[dict] = None,
                   thinking: Optional[str] = None) -> bytes:
    obj: dict = {"model": model, "created_at": now_iso(), "response": response, "done": done}
    if thinking:
        obj["thinking"] = thinking
    if done:
        obj["done_reason"] = "stop"
        obj.update(_stats(stats))
    return _line(obj)


def _stats(stats: Optional[dict]) -> dict:
    """Ollama clients read these timing/count fields off the final chunk; some
    compute tokens/sec from them, so zeros are safer than absence."""
    s = stats or {}
    return {
        "total_duration": int(s.get("total_duration_ns", 0)),
        "load_duration": 0,
        "prompt_eval_count": int(s.get("prompt_tokens", 0)),
        "prompt_eval_duration": 0,
        "eval_count": int(s.get("completion_tokens", 0)),
        "eval_duration": int(s.get("total_duration_ns", 0)),
    }


def chunk_text(text: str, size: int = 400) -> Iterator[str]:
    """Final answers arrive as complete strings (worker output is collected,
    not token-streamed, in v1) — slice them so clients still render
    progressively."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


def persona_tag_entry(persona: dict) -> dict:
    """A personas row dressed as an installed Ollama model (§4.8): clients may
    expect size/digest/modified_at to exist even though they're meaningless for
    a policy entry, so stub plausible values rather than omitting them."""
    name = persona["virtual_name"]
    digest = hashlib.sha256(name.encode()).hexdigest()
    modified = persona.get("updated_at") or now_iso()
    return {
        "name": name,
        "model": name,
        "modified_at": modified,
        "size": 1_000_000_000,          # placeholder — virtual models occupy no disk
        "digest": digest,
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "foundry-router",
            "families": ["foundry-router"],
            "parameter_size": "virtual",
            "quantization_level": "none",
        },
    }


def show_response(persona: dict, context_length: Optional[int] = None) -> dict:
    """Minimal /api/show shape — some clients (Open WebUI, AnythingLLM) call it
    per model and read model_info.*.context_length to size their own token
    budget. A persona is virtual, so we surface a value derived from its real
    routable candidates; without it clients fall back to a tiny built-in guess."""
    model_info: dict = {"general.architecture": "foundry-router"}
    if context_length:
        # Both keys: clients look for either general.context_length or the
        # architecture-prefixed <arch>.context_length (real Ollama uses the
        # latter, e.g. qwen2.context_length).
        model_info["general.context_length"] = int(context_length)
        model_info["foundry-router.context_length"] = int(context_length)
    return {
        "modelfile": f"# Foundry Router virtual persona: {persona['virtual_name']}\n"
                     f"# {persona.get('description', '')}\n",
        "parameters": "",
        "template": "{{ .Prompt }}",
        "details": persona_tag_entry(persona)["details"],
        "model_info": model_info,
        # Advertising "tools" matters: coding clients (Kilo/Cline) check it
        # before sending their own tool definitions.
        "capabilities": ["completion", "chat", "tools"],
    }
