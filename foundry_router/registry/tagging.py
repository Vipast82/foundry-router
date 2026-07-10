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


def tags_from_name(model_id: str) -> list[str]:
    return [tag for tag, pattern in _TAG_PATTERNS if pattern.search(model_id)]


def content_policy_from_name(model_id: str) -> Optional[str]:
    return "permissive" if _PERMISSIVE_RE.search(model_id) else None
