"""Name-based capability/policy heuristics, applied at discovery.

Model names in the local Ollama ecosystem are unusually informative
("...-Uncensored-...-Aggressive", "deepseek-coder", "llava") — enough to seed
structured tags the moment a model is discovered, before the Research Agent
ever runs. The Research Agent refines these later; manual overrides pin them.
Heuristics are deliberately conservative: a missing tag costs one research
pass, a wrong tag skews routing until someone notices.
"""

from __future__ import annotations

import re
from typing import Optional

# Capability tags (design item 3): the queryable vocabulary the prompt,
# research extraction, and UI all share.
TAG_VOCABULARY = ["coding", "vision", "tool-calling", "reasoning",
                  "creative-writing", "long-context"]

_TAG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("coding", re.compile(
        r"coder|codestral|devstral|starcoder|codellama|codeqwen|codegemma|"
        r"deepseek-coder|\bcode\b", re.I)),
    ("vision", re.compile(
        r"vision|llava|pixtral|moondream|minicpm-v|-vl\b|\bvl-", re.I)),
    ("reasoning", re.compile(
        r"deepseek-r1|\bqwq\b|-r1\b|reason|\bthink", re.I)),
    ("tool-calling", re.compile(
        r"ornith|hermes|functionary|firefunction|tool-?(use|call)", re.I)),
    ("creative-writing", re.compile(
        r"story|writer|novel|roleplay|\brp\b|fiction|mytho|creative", re.I)),
]

# Content-policy detection (design item 4): deliberately uncensored/ablated
# local models usually say so in their name. Only ever *sets* "permissive" —
# absence of a match means unknown, not "standard" (that's a manual call).
_PERMISSIVE_RE = re.compile(
    r"uncensored|abliterated|aggressive|heretic|nsfw|dolphin|unaligned|"
    r"unfiltered|liberated|\bevil\b", re.I)


# Embedding-only models expose /api/embeddings, NOT /api/chat — routing one for
# a chat request earns an immediate "does not support chat" 400. Their names are
# almost always explicit ("nomic-embed-text", "mxbai-embed", "bge-*", "*-e5-*").
# This only ever FLAGS embedding; the authoritative check is Ollama's reported
# capabilities (see OllamaProtocol.show_capabilities), with this as the offline
# seed so a fresh discovery never routes one before the capability probe runs.
_EMBEDDING_RE = re.compile(
    r"embed|(?:^|[-_/])bge[-_]|(?:^|[-_/])gte[-_]|(?:^|[-_/])e5[-_]|"
    r"all-minilm|minilm|mxbai|arctic-embed|sentence-transformer|\bnomic-embed\b",
    re.I)


def is_embedding_name(model_id: str) -> bool:
    return bool(_EMBEDDING_RE.search(model_id))


def tags_from_name(model_id: str) -> list[str]:
    return [tag for tag, pattern in _TAG_PATTERNS if pattern.search(model_id)]


def content_policy_from_name(model_id: str) -> Optional[str]:
    return "permissive" if _PERMISSIVE_RE.search(model_id) else None
