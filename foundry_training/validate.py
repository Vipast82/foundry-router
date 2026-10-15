"""Export-time validation of a dataset file.

Checked for every line: valid JSON; the target's structure (chatml-tools:
role order, every tool call answered, JSON arguments, ends on an assistant
turn; dpo: prompt/chosen/rejected present; canonical: schema version and
provenance); no secret-looking text left (redact.scan); no example whose
episode or content belongs to an evaluation set (leakage); no reasoning
fields. A dataset that fails validation is recorded as invalid and must not
be used.
"""

from __future__ import annotations

import json

from . import SCHEMA_VERSION
from .redact import scan
from .schema import content_hash


def validate_dataset(path: str, target: str, known_secrets=(), eval_hashes=frozenset(),
                     store=None) -> dict:
    from .export import check_chatml
    errors, warnings = [], []
    n = 0
    eval_episodes = set()
    if store is not None and eval_hashes:
        for r in store.cur.q("SELECT episode_id, group_id, content_hash FROM episodes"):
            if r["group_id"] in eval_hashes or r["content_hash"] in eval_hashes:
                eval_episodes.add(r["episode_id"])
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            n += 1
            try:
                ex = json.loads(line)
            except ValueError as e:
                errors.append(f"line {i}: not JSON ({e})")
                continue
            if {"reasoning", "reasoning_content", "thinking"} & set(_keys(ex)):
                errors.append(f"line {i}: has a reasoning field")
            if target == "chatml-tools":
                for p in check_chatml(ex):
                    errors.append(f"line {i}: {p}")
                ep = (ex.get("meta") or {}).get("episode_id")
            elif target == "dpo":
                for k in ("prompt", "chosen", "rejected", "basis"):
                    if not ex.get(k):
                        errors.append(f"line {i}: missing {k}")
                ep = None
            else:
                if ex.get("schema_version") != SCHEMA_VERSION:
                    errors.append(f"line {i}: schema_version {ex.get('schema_version')}")
                if not (ex.get("provenance") or {}).get("event_ids"):
                    errors.append(f"line {i}: no provenance")
                ep = ex.get("episode_id")
            if ep and ep in eval_episodes:
                errors.append(f"line {i}: episode {ep} belongs to an evaluation set (leakage)")
            f_ = scan(ex.get("messages") or ex.get("prompt") or ex, known_secrets)
            if f_.count:
                errors.append(f"line {i}: secret-looking content: {f_.kinds()}")
    if n == 0:
        warnings.append("dataset is empty")
    return {"ok": not errors, "examples": n, "errors": errors[:50], "warnings": warnings,
            "target": target, "file_hash": _file_hash(path)}


def _keys(o, prefix=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _keys(v)


def _file_hash(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


__all__ = ["validate_dataset", "content_hash"]
