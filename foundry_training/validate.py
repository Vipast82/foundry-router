"""Export-time validation of a dataset file.

Checked for every line: valid JSON; the target's structure (chatml-tools:
role order, every tool call answered, JSON arguments, ends on an assistant
turn; dpo: prompt/chosen/rejected present; canonical: schema version and
provenance); no secret-looking text left (redact.scan); no example whose
episode or content belongs to an evaluation set (leakage); no reasoning
fields unless the export included reasoning (`reasoning_allowed`, the
manifest's `reasoning_included`). A dataset that fails validation is recorded as invalid and must not
be used.
"""

from __future__ import annotations

import json

from . import SCHEMA_VERSION
from .redact import scan
from .schema import content_hash


def validate_dataset(path: str, target: str, known_secrets=(), eval_hashes=frozenset(),
                     store=None, reasoning_allowed: bool = False) -> dict:
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
            if not reasoning_allowed and \
                    {"reasoning", "reasoning_content", "thinking"} & set(_keys(ex)):
                errors.append(f"line {i}: has a reasoning field")
            if target == "chatml-tools":
                call = (ex.get("meta") or {}).get("kind") == "call"
                for p in check_chatml(ex, allow_open_final=call):
                    errors.append(f"line {i}: {p}")
                for p in _target_problems(ex):
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
                if ex.get("kind") == "call" and ex.get("label_strength") not in (
                        "router_ok", "router_verified"):
                    errors.append(f"line {i}: call without a label strength")
                for where, d in (("diff", ex.get("diff")),
                                 ("original.diff", (ex.get("original") or {}).get("diff")),
                                 ("accepted_correction.diff",
                                  (ex.get("accepted_correction") or {}).get("diff"))):
                    if isinstance(d, dict) and d.get("helper_authored") and d.get("patch"):
                        errors.append(f"line {i}: {where} carries helper-authored code")
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


def _target_problems(ex: dict) -> list[str]:
    """allow_frontier_targets = false: no frontier-authored turn may be a
    target. Helper steps and correction context must carry weight 0."""
    meta = ex.get("meta") or {}
    msgs = ex.get("messages") or []
    p = []
    for k in meta.get("context_only") or []:
        if k >= len(msgs) or msgs[k].get("role") != "assistant" or msgs[k].get("weight") != 0:
            p.append(f"message {k}: context-only turn is not weighted out")
    if meta.get("frontier_targets_allowed") is False or (
            "frontier_targets_allowed" not in meta and meta.get("authors")):
        kinds = {a[0] for a in meta.get("authors") or [] if isinstance(a, (list, tuple)) and a}
        if kinds - {"local", "helper"}:
            p.append(f"frontier-authored turns as targets ({sorted(kinds)})")
        if "helper" in kinds and not meta.get("context_only"):
            p.append("helper steps present but not weighted out")
    return p


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
