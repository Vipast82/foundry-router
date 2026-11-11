"""Tool calls a local model wrote as TEXT (v0.181.129).

A model served by llama.cpp asks for a tool by writing a block in its chat template's
format; the server turns that block into a structured `tool_calls` entry. When the
block is not exactly what the server's parser expects, the server leaves it in the
answer text, the router sees "no tool calls", and the text goes to the user as the
final answer: on 2026-11-11 every Foundry-Chat turn in AnythingLLM ended as

    <tool_call> <function=find_tools> <parameter=query> web search </tool_call>

and nothing ran (qwen3.8-flash-next on Strata; the model drops the closing
</parameter> and </function> tags).

`extract()` reads those blocks, in the two formats local models use:

    <tool_call>                                  <tool_call>
    <function=NAME>                              {"name": "NAME", "arguments": {...}}
    <parameter=KEY>                              </tool_call>
    value
    </parameter>          (closing tags optional)
    </function>
    </tool_call>

It is strict about what counts: tools must have been offered in the request, every
block must parse, and every name must be one of the offered tools. Otherwise the
text is returned untouched (a model explaining the syntax to a user is not calling).

`Gate` is the streaming half: it lets text through until a block starts, then holds
it so a call is never shown to the user as text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

MARKERS = ("<tool_call>", "<function=")
_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|(?=<tool_call>)|\Z)", re.S)
_BARE = re.compile(r"<function=[^>\s]+\s*>.*?(?:</function>|(?=<function=)|\Z)", re.S)
_FUNC = re.compile(r"<function=([^>\s]+)\s*>", re.S)
_PARAM = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)(?=</parameter>|<parameter=|</function>|\Z)", re.S)
_JUNK = re.compile(r"</?(?:parameter|function|tool_call)[^>]*>")


def has_marker(text: str) -> bool:
    return bool(text) and any(m in text for m in MARKERS)


def _schemas(tools: Optional[list]) -> dict:
    """{tool name: {parameter name: JSON-schema type}} for the tools offered."""
    out: dict = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or fn.get("input_schema") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        out[str(name)] = {k: (v.get("type") if isinstance(v, dict) else None)
                          for k, v in (props or {}).items()}
    return out


def _resolve(name: str, schemas: dict) -> Optional[str]:
    """The offered tool a written name means. Exact first; otherwise the ONE offered
    tool whose name ends with it after a prefix ("find_tools" for "foundry_find_tools",
    "web_search" for "searxng__web_search") or differs only in letter case."""
    if name in schemas:
        return name
    low = name.lower()
    near = [n for n in schemas
            if n.lower() == low or n.lower().endswith("_" + low) or n.lower().endswith("." + low)]
    return near[0] if len(near) == 1 else None


def _value(raw: str, kind: Any) -> Any:
    """A parameter's text as the type its schema asks for. A string stays a string;
    anything else is read as JSON when it is JSON."""
    s = raw.strip()
    if kind == "string":
        return s
    if isinstance(kind, list) and "string" in kind and not s[:1] in "{[":
        return s
    if kind is None and s[:1] not in "{[":
        return s
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


def _one(body: str, schemas: dict) -> Optional[dict]:
    body = body.strip()
    if body.startswith("{"):                              # {"name": ..., "arguments": {...}}
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        name, args = obj.get("name"), obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (ValueError, TypeError):
                return None
        name = _resolve(name, schemas) if isinstance(name, str) else None
        if name is None or not isinstance(args, dict):
            return None
        return {"name": name, "arguments": args}
    m = _FUNC.match(body)
    name = _resolve(m.group(1), schemas) if m else None
    if name is None:
        return None
    rest = body[m.end():]
    types = schemas[name]
    args = {k: _value(v, types.get(k)) for k, v in _PARAM.findall(rest)}
    # text between the tags that is not a parameter means this was not a call
    if _JUNK.sub("", _PARAM.sub("", rest)).strip():
        return None
    return {"name": name, "arguments": args}


UNRUN_NOTE = "(My reply here was a request to use a tool. It did not run, so it gave no result.)"


def only_calls(text: str) -> bool:
    """True when a message is nothing but tool-call text (no tools needed to tell):
    a past reply the user saw as raw markup, kept in the chat history by the client."""
    if "<tool_call>" not in (text or ""):
        return False
    bodies = _BLOCK.findall(text)
    return (bool(bodies) and not _BLOCK.sub("", text).strip()
            and all(b.strip().startswith(("<function=", "{")) for b in bodies))


def extract(content: str, tools: Optional[list]) -> tuple[list, str]:
    """(calls, remaining text). `calls` is [{"name", "arguments": dict}], in the order
    written; ([], content) when the text holds no well-formed call to an offered tool."""
    if not tools or not has_marker(content or ""):
        return [], content
    schemas = _schemas(tools)
    if not schemas:
        return [], content
    wrapped = "<tool_call>" in content
    bodies = _BLOCK.findall(content) if wrapped else _BARE.findall(content)
    calls = [_one(b, schemas) for b in bodies]
    if not calls or any(c is None for c in calls):
        return [], content
    rest = (_BLOCK if wrapped else _BARE).sub("", content).strip()
    return calls, rest


class Gate:
    """Streaming: text passes through until a call block starts; from there it is held.
    `finish(tools)` gives (calls, text still owed to the client)."""

    def __init__(self, active: bool = True):
        self.active = bool(active)
        self._held = ""                    # everything from the first marker on
        self._tail = ""                    # the end of the passed text that may start a marker

    @property
    def holding(self) -> bool:
        return bool(self._held)

    @property
    def held_chars(self) -> int:
        return len(self._held)

    def feed(self, text: str) -> str:
        if not self.active or not text:
            return text
        if self._held:
            self._held += text
            return ""
        text = self._tail + text
        self._tail = ""
        cut = min((i for i in (text.find(m) for m in MARKERS) if i >= 0), default=-1)
        if cut >= 0:
            self._held = text[cut:]
            return text[:cut]
        # keep back an ending that could be the start of a marker ("<tool_c")
        keep = 0
        for m in MARKERS:
            for n in range(min(len(m) - 1, len(text)), 0, -1):
                if text.endswith(m[:n]):
                    keep = max(keep, n)
                    break
        if keep:
            self._tail = text[-keep:]
            return text[:-keep]
        return text

    def finish(self, tools: Optional[list]) -> tuple[list, str]:
        held, self._held, self._tail = self._held or self._tail, "", ""
        if not held:
            return [], ""
        calls, rest = extract(held, tools)
        return calls, (rest if calls else held)
