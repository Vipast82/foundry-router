"""Dataset export: a model-neutral canonical format plus target adapters.

  canonical     one JSON object per episode: messages with authorship, tool
                definitions, revision, outcome, flags, provenance (event ids,
                sources), split. The archival format; every target is derived
                from it.
  chatml-tools  OpenAI-style {"messages", "tools"} JSONL, the conversational
                format TRL / Unsloth / Axolotl read for chat models with tool
                use. Checked against Qwen-family ChatML + Hermes-style
                <tool_call> conventions (system first, tool results follow
                their call, ends on an assistant turn without open calls),
                token length estimated (or counted with a local tokenizer)
                against the project's max_tokens. Long examples are cut only at
                an assistant turn with every tool call answered; if no such
                prefix fits, the example is left out.
  dpo           {"prompt", "chosen", "rejected", "basis"} for preference pairs.

Training datasets go to <root>/datasets/<id>/, evaluation sets to
<root>/eval/<id>/; an episode in an evaluation set can never be exported for
training (and vice versa), checked by group and by content hash.
Reasoning channels are never exported (include_reasoning has no effect
until a target that supports them is added).

Targets per turn: with allow_frontier_targets = false only the local model's
own assistant turns are targets. A helper step (a local turn that carried out
a helper's prework patch or a harvested pre-code model) and, in a correction,
every turn up to the feedback stay in the conversation as context with
"weight": 0 (the per-message training switch TRL / Axolotl chat datasets
read); meta.context_only lists them. `tags` limits an export to episodes
carrying all the given tags (e.g. ["ui_v3"] for the Roblox-UI subset).
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections import defaultdict
from typing import Optional

from . import SCHEMA_VERSION
from .labels import is_target
from .messages import canonical_text, pairing
from .schema import content_hash

RESTRICTIONS = [
    "Captured content may include third-party material (game assets, documentation, "
    "dependencies); redistribution is not permitted by this export.",
    "Model outputs from hosted providers (e.g. Anthropic, OpenAI) are governed by their terms, "
    "which restrict using outputs to develop competing models; frontier-authored turns are "
    "excluded as training targets unless the project explicitly allows it.",
    "Check the local base model's license before fine-tuning or distributing weights.",
]


class ExportError(RuntimeError):
    pass


def _render_chatml(messages: list[dict], tools: list[dict]) -> str:
    """Qwen-family ChatML with Hermes-style tool blocks (re-implemented for
    validation and length estimates; verify against the actual tokenizer's
    chat template before training)."""
    out = []
    sys_text = ""
    rest = messages
    if messages and messages[0]["role"] == "system":
        sys_text, rest = messages[0]["content"], messages[1:]
    if tools:
        sys_text += ("\n\n# Tools\n\n<tools>\n" + "\n".join(
            json.dumps({"type": "function", "function": t}) for t in tools) + "\n</tools>")
    if sys_text:
        out.append(f"<|im_start|>system\n{sys_text}<|im_end|>")
    for m in rest:
        if m["role"] == "tool":
            out.append(f"<|im_start|>user\n<tool_response>\n{m['content']}\n</tool_response>"
                       "<|im_end|>")
            continue
        body = m.get("content") or ""
        for tc in m.get("tool_calls") or []:
            body += "\n<tool_call>\n" + json.dumps({"name": tc["function"]["name"],
                                                    "arguments": json.loads(
                                                        tc["function"]["arguments"])}) + \
                "\n</tool_call>"
        out.append(f"<|im_start|>{m['role']}\n{body}<|im_end|>")
    return "\n".join(out)


def _tokens(text: str, tokenizer=None) -> tuple[int, str]:
    if tokenizer is not None:
        return len(tokenizer.encode(text).ids), "tokenizer"
    return math.ceil(len(text) / 3.2), "estimate (chars/3.2)"


def load_tokenizer(path: Optional[str]):
    if not path:
        return None
    try:
        from tokenizers import Tokenizer               # optional dependency
        return Tokenizer.from_file(path)
    except Exception:                                  # noqa: BLE001
        return None


def weights(ep: dict, msgs: list[dict], allow_frontier: bool) -> Optional[list]:
    """Per-message weight (assistant turns only), or None when every assistant
    turn is a target (the dataset then looks exactly as before)."""
    until = ep.get("context_until") if ep.get("kind") == "correction" else None
    w = []
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            w.append(None)
            continue
        w.append(1 if is_target(m, allow_frontier) and (until is None or i > until) else 0)
    return w if any(x == 0 for x in w) else None


def to_chatml(messages: list[dict], tools: list[dict], weight: Optional[list] = None) -> dict:
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id") or "",
                        "content": m.get("content") or ""})
            continue
        o = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            o["tool_calls"] = [{"id": tc["id"], "type": "function",
                                "function": {"name": tc["name"],
                                             "arguments": json.dumps(tc["arguments"],
                                                                     ensure_ascii=False)}}
                               for tc in m["tool_calls"]]
        if weight is not None and m["role"] == "assistant":
            o["weight"] = weight[i]
        out.append(o)
    ex = {"messages": out}
    if tools:
        ex["tools"] = [{"type": "function", "function": t} for t in tools]
    return ex


def check_chatml(ex: dict) -> list[str]:
    msgs = ex["messages"]
    p = []
    if not msgs:
        return ["empty"]
    i = 1 if msgs[0]["role"] == "system" else 0
    if i >= len(msgs) or msgs[i]["role"] != "user":
        p.append("first non-system message must be from the user")
    prev = None
    open_calls: set = set()
    for k, m in enumerate(msgs[i:], start=i):
        r = m["role"]
        if r == "system":
            p.append(f"message {k}: system message after the start")
        if r == "assistant" and open_calls:
            p.append(f"message {k}: assistant before every tool call was answered")
        if r == "tool":
            if m.get("tool_call_id") not in open_calls:
                p.append(f"message {k}: tool result for an unknown call")
            open_calls.discard(m.get("tool_call_id"))
        if r == "user" and open_calls:
            p.append(f"message {k}: user turn while tool calls are open")
        if r == "assistant":
            for tc in m.get("tool_calls") or []:
                if not tc.get("id") or not tc.get("function", {}).get("name"):
                    p.append(f"message {k}: tool call without id or name")
                try:
                    json.loads(tc["function"]["arguments"])
                except (ValueError, KeyError, TypeError):
                    p.append(f"message {k}: tool call arguments aren't JSON")
                open_calls.add(tc.get("id"))
        prev = r
    if prev != "assistant" or open_calls:
        p.append("must end on an assistant turn with no open tool calls")
    if "tools" in ex:
        for t in ex["tools"]:
            fn = t.get("function") or {}
            if not fn.get("name") or not isinstance(fn.get("parameters"), dict):
                p.append(f"tool definition {fn.get('name')!r} lacks name/parameters")
    return p


def segment(messages: list[dict], tools: list[dict], limit: int, tokenizer=None):
    """Longest prefix ending at a complete assistant turn that fits."""
    for end in range(len(messages), 0, -1):
        m = messages[end - 1]
        if m["role"] != "assistant" or m.get("tool_calls"):
            continue
        cand = messages[:end]
        if not pairing(cand)["ok"]:
            continue
        ex = to_chatml(cand, tools)
        n, how = _tokens(_render_chatml(ex["messages"], tools), tokenizer)
        if n <= limit:
            return cand, n, how
    return None, None, None


def _training_groups(store) -> set:
    rows = store.cur.q("SELECT e.group_id, e.content_hash FROM dataset_members m JOIN datasets d "
                       "ON d.dataset_id=m.dataset_id JOIN episodes e ON e.episode_id=m.episode_id "
                       "WHERE d.purpose='train'")
    return {r["group_id"] for r in rows} | {r["content_hash"] for r in rows}


def _eval_groups(store) -> set:
    rows = store.cur.q("SELECT group_id, content_hash FROM episodes WHERE split='eval' OR status='eval'")
    rows += store.cur.q("SELECT e.group_id, e.content_hash FROM dataset_members m JOIN datasets d "
                        "ON d.dataset_id=m.dataset_id JOIN episodes e ON e.episode_id=m.episode_id "
                        "WHERE d.purpose='eval'")
    return {r["group_id"] for r in rows} | {r["content_hash"] for r in rows}


def export(store, projects: list, kind: str, target: str, cfgs: dict, purpose: str = "train",
           tokenizer_path: Optional[str] = None, known_secrets=(), tags=None) -> dict:
    from .validate import validate_dataset
    if purpose not in ("train", "eval"):
        raise ExportError("purpose must be train or eval")
    if purpose == "eval" and target == "dpo":
        raise ExportError("evaluation sets use canonical or chatml-tools")
    tok = load_tokenizer(tokenizer_path)
    ds_id = f"{purpose}-{kind}-{target}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    out_dir = store.root / ("eval" if purpose == "eval" else "datasets") / ds_id
    out_dir.mkdir(parents=True)
    os.chmod(out_dir, 0o700)
    lines, members, skipped = [], defaultdict(int), defaultdict(int)
    eval_guard = _eval_groups(store) if purpose == "train" else set()
    train_guard = _training_groups(store) if purpose == "eval" else set()
    token_method = None
    if kind == "preference":
        lines = _preference_lines(store, projects, target)
        for ln in lines:
            members[ln["_episode"]] += 1
    else:
        want_kind = "correction" if kind == "correction" else "trajectory"
        status = "eval" if purpose == "eval" else "selected"
        for pid in projects:
            cfg = cfgs[pid]
            rows = store.cur.q("SELECT * FROM episodes WHERE project_id=? AND kind=? AND deleted=0 "
                               "AND status=?", (pid, want_kind, status))
            limit = int((cfg.policy.get("sft") or {}).get("max_tokens") or 32768)
            for r in rows:
                ep = json.loads(r["data"])
                if purpose == "train" and (r["group_id"] in eval_guard or
                                           r["content_hash"] in eval_guard):
                    skipped["eval_leakage_blocked"] += 1
                    continue
                if purpose == "eval" and (r["group_id"] in train_guard or
                                          r["content_hash"] in train_guard):
                    skipped["already_in_training"] += 1
                    continue
                if tags and not set(tags) <= set(ep.get("tags") or []):
                    skipped["tag_filter"] += 1
                    continue
                if kind == "multimodal":
                    la = ep["flags"].get("label_artifacts") or []
                    if not any(a.get("refs") for a in (ep["flags"].get("artifacts") or []) + la):
                        skipped["no_artifacts"] += 1
                        continue
                    if any(a.get("status") != "present" for a in la):
                        skipped["missing_artifact"] += 1
                        continue
                allow = bool(cfg.export.get("allow_frontier_targets", False))
                if target == "canonical":
                    lines.append({"_episode": r["episode_id"], **canonical(ep, r, allow)})
                    members[r["episode_id"]] += 1
                    continue
                msgs, tools = ep["messages"], ep.get("tools") or []
                w = weights(ep, msgs, allow)
                if w is not None and not any(x == 1 for x in w):
                    skipped["no_target_turn"] += 1
                    continue
                ex = to_chatml(msgs, tools, w)
                n, token_method = _tokens(_render_chatml(ex["messages"], tools), tok)
                seg = False
                if n > limit:
                    cut, n, token_method = segment(msgs, tools, limit, tok)
                    if cut is None:
                        skipped["too_long"] += 1
                        continue
                    if w is not None and not any(x == 1 for x in w[:len(cut)]):
                        skipped["no_target_turn"] += 1     # the cut kept only context turns
                        continue
                    ex, seg = to_chatml(cut, tools, w[:len(cut)] if w else None), True
                ex["meta"] = {"episode_id": r["episode_id"], "project_id": pid,
                              "task_id": r["task_id"], "kind": r["kind"], "segmented": seg,
                              "tokens": n, "token_count": token_method,
                              "authors": ep["flags"].get("authors"),
                              "context_source": ep["context_source"],
                              "tags": ep.get("tags") or [],
                              "frontier_targets_allowed": allow,
                              "context_only": [i for i, x in enumerate(w or []) if x == 0
                                               and i < len(ex["messages"])]}
                lines.append({"_episode": r["episode_id"], **ex})
                members[r["episode_id"]] += 1
    data_path = out_dir / "data.jsonl"
    with open(data_path, "w", encoding="utf-8") as f:
        for ln in lines:
            ln = {k: v for k, v in ln.items() if k != "_episode"}
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    os.chmod(data_path, 0o600)
    val = validate_dataset(str(data_path), target, known_secrets,
                           eval_hashes=_eval_groups(store) if purpose == "train" else set(),
                           store=store)
    manifest = {
        "dataset_id": ds_id, "schema_version": SCHEMA_VERSION, "kind": kind, "target": target,
        "purpose": purpose, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "projects": projects, "policy_hashes": {p: cfgs[p].policy_hash() for p in projects},
        "split_policy": {p: cfgs[p].split for p in projects}, "examples": len(lines),
        "episodes": sorted(members), "skipped": dict(skipped), "validation": val,
        "token_count": token_method, "reasoning_included": False, "tags": list(tags or []),
        "restrictions": RESTRICTIONS,
        "lineage": {"episode_ids_hash": content_hash(sorted(members))},
    }
    with open(out_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    store.cur.x("INSERT INTO datasets (dataset_id, kind, target, purpose, created_ts, path, "
                "manifest, status, validation) VALUES (?,?,?,?,?,?,?,?,?)",
                (ds_id, kind, target, purpose, time.time(), str(out_dir), json.dumps(manifest),
                 "valid" if val["ok"] else "invalid", json.dumps(val)))
    for eid, n in members.items():
        store.cur.x("INSERT OR REPLACE INTO dataset_members (dataset_id, episode_id, "
                    "example_count) VALUES (?,?,?)", (ds_id, eid, n))
    return manifest


WITHHELD = "helper-authored code: kept in the store for provenance, never a training target"


def _safe_diff(d, helper: bool):
    """A revision diff for the canonical export. A diff that contains (or
    may contain) helper-authored code is withheld: its patch is dropped and
    the record says why, so it can never be read as a target."""
    if not isinstance(d, dict):
        return d
    if helper or d.get("helper_authored"):
        return {"patch": "", "truncated": bool(d.get("truncated")), "helper_authored": True,
                "withheld": WITHHELD, "target": False}
    return d


def canonical(ep: dict, row: dict, allow_frontier: bool = False) -> dict:
    helper = bool(ep["flags"].get("helper_steps")) or         any(a and a[0] == "helper" for a in ep["flags"].get("authors") or [])
    base = {"schema_version": SCHEMA_VERSION, "episode_id": ep["episode_id"],
            "project_id": ep["project_id"], "task_id": ep["task_id"], "kind": ep["kind"],
            "split": row["split"], "status": row["status"], "group_id": row["group_id"],
            "context_source": ep["context_source"], "messages": ep["messages"],
            "tools": ep.get("tools") or [], "revision": ep.get("revision"),
            "outcome": ep["outcome"], "flags": ep["flags"],
            "eval_categories": ep.get("eval_categories") or [],
            "provenance": {"sources": ep["sources"], "event_ids": ep["event_ids"]},
            "diff": _safe_diff(ep.get("diff"), helper), "labels": ep.get("labels") or {}, "tags": ep.get("tags") or [],
            "targets": [i for i, x in enumerate(weights(ep, ep["messages"], allow_frontier) or
                                                [1 if m["role"] == "assistant" else None
                                                 for m in ep["messages"]]) if x == 1]}
    if ep["kind"] == "correction":
        orig = dict(ep["original"])
        if "diff" in orig:
            orig["diff"] = _safe_diff(orig["diff"], any(
                a and a[0] == "helper" for a in orig.get("authors") or []))
        base.update(original=orig, feedback=ep["feedback"],
                    context_until=ep.get("context_until"),
                    accepted_correction={"episode_id": ep["correction"]["episode_id"],
                                         "revision": ep["correction"]["revision"],
                                         "diff": _safe_diff(ep["correction"]["diff"], helper)})
    return base


def _preference_lines(store, projects, target) -> list[dict]:
    from .messages import neutral, neutral_tools
    out = []
    for pid in projects:
        for p in store.cur.q("SELECT * FROM preferences WHERE project_id=?", (pid,)):
            ev = {r["event_id"]: r for r in store.events(pid, "event_id IN (?,?)",
                                                         (p["chosen_event"], p["rejected_event"]))}
            if len(ev) != 2:
                continue
            c, r = ev[p["chosen_event"]], ev[p["rejected_event"]]
            ctx = [neutral(m) for m in c["facts"].get("messages") or []]

            def answer(e):
                a = neutral({"role": "assistant", "content": e["facts"].get("content") or "",
                             "tool_calls": e["facts"].get("tool_calls")})
                return to_chatml([a], [])["messages"]
            tools = neutral_tools(c["facts"].get("tools"))
            ex = {"prompt": to_chatml(ctx, tools)["messages"] if target == "dpo" else ctx,
                  "tools": to_chatml([], tools).get("tools", []),
                  "chosen": answer(c), "rejected": answer(r),
                  "basis": p["basis"], "judge": p["judge"], "project_id": pid,
                  "_episode": f"pref:{p['id']}"}
            out.append(ex)
    return out


__all__ = ["export", "to_chatml", "check_chatml", "canonical_text"]
