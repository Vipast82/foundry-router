"""Sampling / generation options + structured-output resolution.

One place that merges the operator's global sampling defaults, a persona's
overrides, and whatever the client already sent into the final Ollama `options`
dict — and resolves a persona's requested output `format`. Precedence, weakest
to strongest: global defaults < persona sampling_options < client options.
Client-sent values always win (a coding client that pins its own temperature is
never overridden)."""

from __future__ import annotations

import json
from typing import Optional


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def resolve_options(global_defaults, persona: Optional[dict],
                    client_options) -> Optional[dict]:
    """Merged sampling options for a worker call, or None when there's nothing
    to send. global_defaults and persona['sampling_options'] are operator config;
    client_options is what the request already carried (and wins)."""
    merged: dict = {}
    merged.update(_as_dict(global_defaults))
    merged.update(_as_dict((persona or {}).get("sampling_options")))
    merged.update(_as_dict(client_options))     # client always wins
    return merged or None


def resolve_format(persona: Optional[dict]):
    """The persona's requested structured-output format, normalized:
      None  -> unset (free-form text)
      "json" -> Ollama json mode / OpenAI {"type":"json_object"}
      dict  -> a JSON schema (Ollama `format` object / OpenAI json_schema)
    Accepts a raw string that is either "json" or a JSON-schema string."""
    fmt = (persona or {}).get("output_format")
    if fmt is None:
        return None
    if isinstance(fmt, dict):
        return fmt or None
    s = str(fmt).strip()
    if not s:
        return None
    if s.lower() == "json":
        return "json"
    # a JSON-schema string -> parse to an object; if it isn't valid JSON, pass
    # the literal through (Ollama accepts "json"; anything else is the operator's
    # responsibility).
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, (dict, str)) else None
    except (json.JSONDecodeError, TypeError):
        return s
