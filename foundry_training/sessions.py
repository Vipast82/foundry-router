"""Keep-everything datasets: one example per conversation, labelled, not filtered
(v0.181.145).

The curated kinds (sft, correction) select by policy and, on a live store, kept 2
of 1,169 episodes. The `calls` kind writes one example per model call, each
repeating the conversation before it. This module writes what a fine-tune of the
whole body of work needs:

* **One example per conversation.** A session's model calls are grouped into
  conversations: a call whose messages are a prefix of a later call's is part of
  that later conversation, so only the last call of each conversation is written
  (the end of the session, and the call before each compaction). Every assistant
  turn in it is a training target.
* **Nothing is dropped for who wrote it or how it was judged.** Each example
  carries its labels in `meta`: the models that wrote it, the persona and role,
  the task, a quality `tier` (verified / ok / unknown / failed) with the labels
  behind it, and skill `tags` (luau, roblox_ui, roblox_studio, blender, tool_use).
  A trainer filters on those; the dataset does not.
  Not written: a call that never completed (there is no reply to learn from).
  Text that still looks like a secret is blanked in place and the example kept.
* **A long conversation becomes several examples** of at most `max_tokens`, cut
  between turns, each starting with the system prompt; a later piece opens with a
  line saying earlier turns are omitted.
* **Only the tools an example uses are listed in it** (`--tools used`, the
  default). A coding session carries 60 to 180 tool definitions, about 40,000
  tokens before the first word: measured on the live store, that alone was over a
  32,768-token example for 131 of 220 conversations, and the first run left 5,540
  conversations out. `--tools all` keeps every definition (use a larger
  `--max-tokens` with it).
* **A turn larger than a whole example is shortened in the middle**, with a line
  saying how many characters were left out, instead of being dropped.
* **Two files per split**, the same examples:
    chat   `{"messages": [...], "tools": [...], "meta": {...}}`  OpenAI-style
           messages with tool calls (TRL, Unsloth, Axolotl)
    text   `{"text": "...", "meta": {...}}`  the conversation rendered in Qwen
           ChatML with `<tool_call>` blocks, for any trainer that takes plain text

Output folder: `<store>/datasets/<name>/` with train.jsonl, validation.jsonl,
train.text.jsonl, validation.text.jsonl, README.md and manifest.json.

Provider terms: the manifest and README repeat that outputs of hosted models are
governed by their providers' terms. Including them is the operator's decision.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from typing import Iterator, Optional

from .export import RESTRICTIONS, _render_chatml, _tokens, to_chatml
from .labels import NEGATIVE_LABELS, POSITIVE_LABELS
from .messages import neutral, neutral_tools

OMITTED = "(The conversation continues from earlier turns that are not shown here.)"
ELIDED = "\n… [{n:,} characters left out of this message to fit the example] …\n"
TIERS = ("verified", "ok", "unknown", "failed")
_TAGS = (
    ("blender", re.compile(r"blender|\bbpy\b|\.blend\b|\.fbx\b|\.glb\b", re.I)),
    ("roblox_studio", re.compile(r"execute_luau|playtest|roblox[_ ]?studio|rojo|\.rbxl|\.rbxm", re.I)),
    ("roblox_ui", re.compile(r"ScreenGui|UIListLayout|UICorner|UIStroke|TextLabel|ImageLabel|"
                             r"Fusion\.|Roact|/ui/|\\ui\\|StarterGui", re.I)),
    ("luau", re.compile(r"\.luau?\b|--!strict|\blocal function\b|game:GetService", re.I)),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tier_of(outcomes: list, labels: list) -> str:
    """verified: a positive label and no negative one; failed: a negative label,
    or the conversation's last call did not end ok; ok: the router saw it end ok;
    unknown: the router has no record of how it ended."""
    names = {l for l in labels if l}
    if any(n in NEGATIVE_LABELS for n in names):
        return "failed"
    if any(n in POSITIVE_LABELS for n in names):
        return "verified"
    last = outcomes[-1] if outcomes else None
    if last == "ok":
        return "ok"
    return "unknown" if last in (None, "") else "failed"


def tags_of(messages: list, tools: list, task_id: Optional[str], role: Optional[str]) -> list:
    blob_parts = []
    used = False
    for m in messages:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                used = True
                blob_parts.append(tc["name"] + " " + json.dumps(tc.get("arguments"), default=str)[:20000])
            blob_parts.append((m.get("content") or "")[:20000])
    blob = "\n".join(blob_parts)
    out = [name for name, rx in _TAGS if rx.search(blob)]
    if used:
        out.append("tool_use")
    if task_id and re.match(r"(?i)^ui[-_]", task_id) and "roblox_ui" not in out:
        out.append("roblox_ui")
    if role:
        out.append("role:" + str(role))
    return out


def _calls(store, pid: str) -> Iterator[tuple]:
    """(session_id, [call rows]) with facts still packed (references, not text)."""
    sessions = store.norm.q(
        "SELECT session_id, MIN(timestamp) AS t FROM events WHERE project_id=? AND deleted=0 "
        "AND event_type='model_call' AND session_id IS NOT NULL GROUP BY session_id ORDER BY t",
        (pid,))
    for s in sessions:
        rows = store.norm.q(
            "SELECT event_id, task_id, session_id, request_id, timestamp, actual_model, "
            "actual_backend, requested_persona, capture_status, facts FROM events WHERE "
            "project_id=? AND session_id=? AND deleted=0 AND event_type='model_call' "
            "ORDER BY timestamp, seq", (pid, s["session_id"]))
        for r in rows:
            r["facts"] = json.loads(r["facts"] or "{}")
        yield s["session_id"], rows


def conversations(store, calls: list) -> list:
    """The calls that END a conversation, each with the calls it contains.
    [(last_call, [(position of its reply, call)...])]"""
    usable = []
    for c in calls:
        f = c["facts"]
        if f.get("status") != "ok" or c["capture_status"] != "complete" or not f.get("messages"):
            continue
        if not (f.get("content") or f.get("tool_calls")):
            continue
        usable.append((c, store.blobs.keys(f["messages"])))
    out = []
    for i, (c, k) in enumerate(usable):
        covered = any(len(k2) > len(k) and k2[:len(k)] == k for _, k2 in usable[i + 1:])
        if covered:
            continue
        inside = [(len(k1), c1) for c1, k1 in usable[:i] if len(k1) < len(k) and k[:len(k1)] == k1]
        out.append((c, inside))
    return out


def build_example(store, last: dict, inside: list, outcome_labels: dict) -> Optional[dict]:
    """One conversation as neutral messages with authors, reasoning and labels."""
    f = store.unpack(last["facts"])
    raw = [m for m in (f.get("messages") or []) if isinstance(m, dict)]
    if len(raw) != len(f.get("messages") or []):
        inside = []                                   # positions would not line up
    msgs = [neutral(m) for m in raw]
    models = Counter()
    for pos, c in inside:
        if pos < len(msgs) and msgs[pos]["role"] == "assistant":
            msgs[pos]["author"] = {"kind": "model", "model": c["actual_model"]}
            r = c["facts"].get("reasoning")
            if isinstance(r, dict):
                r = store.unpack(r)
            if r:
                msgs[pos]["reasoning"] = r
            models[c["actual_model"] or "?"] += 1
    for m in msgs:
        if m["role"] == "assistant" and (m.get("author") or {}).get("kind") != "model":
            m["author"] = {"kind": "model", "model": last["actual_model"]}
    final = neutral({"role": "assistant", "content": f.get("content") or "",
                     "tool_calls": f.get("tool_calls")},
                    {"kind": "model", "model": last["actual_model"]})
    if f.get("reasoning"):
        final["reasoning"] = f["reasoning"]
    msgs.append(final)
    models[last["actual_model"] or "?"] += 1
    if not any(m["role"] == "user" for m in msgs):
        return None
    every = [c for _, c in inside] + [last]
    labels, outcomes = [], []
    for c in every:
        q = c["facts"].get("quality") or {}
        outcomes.append(q.get("outcome"))
        labels += [l.get("label") for l in (q.get("labels") or [])]
        labels += outcome_labels.get(c.get("request_id") or "", [])
    tools = neutral_tools(f.get("tools"))
    return {"messages": msgs, "tools": tools,
            "meta": {"project_id": None, "task_id": last["task_id"],
                     "session_id": last["session_id"], "conversation_id": last["event_id"],
                     "role": f.get("role"), "persona": last["requested_persona"],
                     "models": dict(models), "backend": last["actual_backend"],
                     "calls": len(every), "first_ts": every[0]["timestamp"],
                     "last_ts": last["timestamp"],
                     "tier": tier_of(outcomes, labels),
                     "labels": sorted({l for l in labels if l}),
                     "open_tool_calls": bool(final.get("tool_calls")),
                     "tags": tags_of(msgs, tools, last["task_id"], f.get("role"))}}


def used_tools(messages: list, tools: list) -> list:
    """The definitions of the tools these messages call, in their original order."""
    names = {tc["name"] for m in messages for tc in (m.get("tool_calls") or [])}
    return [t for t in tools if t.get("name") in names]


def _shorten(unit: list, over_tokens: int) -> list:
    """The unit with its longest message cut in the middle by about `over_tokens`."""
    i = max(range(len(unit)), key=lambda k: len(unit[k].get("content") or ""))
    text = unit[i].get("content") or ""
    cut = min(len(text) - 200, int(over_tokens * 3.2) + 400)
    if cut <= 0:
        return unit
    keep = len(text) - cut
    head, tail = text[:keep * 2 // 3], text[len(text) - keep // 3:]
    out = list(unit)
    out[i] = {**unit[i], "content": head + ELIDED.format(n=len(text) - len(head) - len(tail)) + tail}
    return out


def pieces(messages: list, tools: list, max_tokens: int, tok=None) -> list:
    """The conversation as it is when it fits; otherwise consecutive pieces of at
    most max_tokens, cut before a user turn or an assistant turn (never between a
    tool call and its result). A turn too large for a piece of its own is
    shortened in the middle (`_shorten`); nothing is left out for its size unless
    the system prompt and tools alone fill the example."""
    def size(ms):
        return _tokens(_render_chatml(to_chatml(ms, tools)["messages"], tools), tok)[0]

    if size(messages) <= max_tokens:
        return [messages]
    head = [m for m in messages[:1] if m["role"] == "system"]
    body = messages[len(head):]
    # units: [user], or [assistant + the tool results that answer it]
    units, cur = [], []
    for m in body:
        if m["role"] in ("user", "assistant") and cur:
            units.append(cur)
            cur = []
        cur.append(m)
    if cur:
        units.append(cur)
    out, piece = [], []

    def opener():
        return head + ([] if not out else [{"role": "user", "content": OMITTED,
                                            "author": {"kind": "router", "model": None}}])
    for u in units:
        trial = opener() + piece + u
        if size(trial) <= max_tokens:
            piece += u
            continue
        if piece and any(m["role"] == "assistant" for m in piece):
            out.append(opener() + piece)
        piece = u
        for _ in range(6):                     # too large even alone: shorten it
            over = size(opener() + piece) - max_tokens
            if over <= 0:
                break
            piece = _shorten(piece, over)
        if size(opener() + piece) > max_tokens:
            piece = []                         # the system prompt and tools fill the example
    if piece and any(m["role"] == "assistant" for m in piece):
        out.append(opener() + piece)
    return out


def _split(group: str, seed: str, fraction: float) -> str:
    h = int(_sha(f"{seed}:{group}")[:8], 16)
    return "validation" if (h / 0xFFFFFFFF) < fraction else "train"


def export(store, projects: list, name: str = "", max_tokens: int = 32768,
           validation_fraction: float = 0.05, seed: str = "sessions-v1",
           include_reasoning: bool = True, tools: str = "used", tags: Optional[list] = None,
           tiers: Optional[list] = None, tokenizer_path: Optional[str] = None,
           known_secrets=(), out_root: Optional[str] = None, log=print) -> dict:
    """Write the keep-everything dataset for `projects`. `tags` / `tiers` narrow
    it only when given (the default is everything)."""
    from .export import load_tokenizer
    from .redact import Redactor
    redactor = Redactor(known_secrets)
    tok = load_tokenizer(tokenizer_path)
    name = name or f"all-sessions-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = (store.root / "datasets" if not out_root else __import__("pathlib").Path(out_root)) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {(sp, kind): open(out_dir / f"{sp}{'.text' if kind == 'text' else ''}.jsonl", "w",
                              encoding="utf-8")
             for sp in ("train", "validation") for kind in ("chat", "text")}
    stats = {"sessions": 0, "conversations": 0, "examples": Counter(), "tokens": Counter(),
             "tiers": Counter(), "tags": Counter(), "models": Counter(), "skipped": Counter(),
             "pieces_of_long": 0, "redacted_again": 0, "shortened": 0}
    seen: set = set()
    try:
        for pid in projects:
            outcome_labels: dict = defaultdict(list)
            for e in store.events(pid, "event_type='router_outcome'"):
                if e.get("request_id"):
                    outcome_labels[e["request_id"]].append(e["facts"].get("label"))
            for sid, calls in _calls(store, pid):
                stats["sessions"] += 1
                for last, inside in conversations(store, calls):
                    ex = build_example(store, last, inside, outcome_labels)
                    if ex is None:
                        stats["skipped"]["no_user_turn"] += 1
                        continue
                    meta = ex["meta"]
                    meta["project_id"] = pid
                    if tags and not set(tags) <= set(meta["tags"]):
                        stats["skipped"]["tag_filter"] += 1
                        continue
                    if tiers and meta["tier"] not in tiers:
                        stats["skipped"]["tier_filter"] += 1
                        continue
                    stats["conversations"] += 1
                    group = f"{pid}:{meta['task_id'] or ('session:' + (sid or ''))}"
                    sp = _split(group, seed, validation_fraction)
                    # cut with no tool list (each piece gets only the tools it uses),
                    # or with all of them
                    cut_tools = ex["tools"] if tools == "all" else []
                    reserve = 0 if tools == "all" else min(max_tokens // 4, 6000)
                    parts = pieces(ex["messages"], cut_tools, max_tokens - reserve, tok)
                    if not parts:
                        stats["skipped"]["system_prompt_fills_the_example"] += 1
                        continue
                    if len(parts) > 1:
                        stats["pieces_of_long"] += len(parts)
                    full_tools = ex["tools"]
                    for i, ms in enumerate(parts):
                        ex["tools"] = full_tools if tools == "all" else used_tools(ms, full_tools)
                        if any(ELIDED.split("{")[0].strip() in (x.get("content") or "") for x in ms):
                            stats["shortened"] += 1
                        # anything that still looks like a secret is blanked in place and
                        # the example is kept. Done on the messages BEFORE tool-call
                        # arguments become a JSON string: blanking inside that string
                        # broke the JSON (found on the live store).
                        ms, found = redactor.obj(ms)
                        if found.count:
                            stats["redacted_again"] += 1
                        chat = to_chatml(ms, ex["tools"], None, reasoning=include_reasoning)
                        text = _render_chatml(chat["messages"], ex["tools"])
                        key = _sha(text)
                        if key in seen:
                            stats["skipped"]["duplicate"] += 1
                            continue
                        seen.add(key)
                        n = _tokens(text, tok)[0]
                        m = {**meta, "piece": i + 1, "pieces": len(parts), "tokens": n}
                        files[(sp, "chat")].write(json.dumps({**chat, "meta": m},
                                                             ensure_ascii=False) + "\n")
                        files[(sp, "text")].write(json.dumps({"text": text, "meta": m},
                                                             ensure_ascii=False) + "\n")
                        stats["examples"][sp] += 1
                        stats["tokens"][sp] += n
                        stats["tiers"][meta["tier"]] += 1
                        for t in meta["tags"]:
                            stats["tags"][t] += 1
                        for mdl, k in meta["models"].items():
                            stats["models"][mdl] += k
                if stats["sessions"] % 200 == 0:
                    log(f"  {stats['sessions']:,} sessions, {sum(stats['examples'].values()):,} examples")
    finally:
        for f in files.values():
            f.close()
    manifest = {
        "dataset": name, "kind": "sessions", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "projects": projects, "max_tokens": max_tokens,
        "token_count": "tokenizer" if tok is not None else "estimate (chars/3.2)",
        "validation_fraction": validation_fraction, "seed": seed,
        "reasoning_included": include_reasoning, "filters": {"tags": tags or [], "tiers": tiers or []},
        "sessions": stats["sessions"], "conversations": stats["conversations"],
        "examples": dict(stats["examples"]), "tokens": dict(stats["tokens"]),
        "tiers": dict(stats["tiers"]), "tags": dict(stats["tags"].most_common()),
        "models": dict(stats["models"].most_common()), "skipped": dict(stats["skipped"]),
        "examples_from_long_conversations": stats["pieces_of_long"],
        "examples_redacted_again": stats["redacted_again"],
        "examples_with_a_shortened_turn": stats["shortened"], "tools": tools,
        "files": {f"{sp}{'.text' if kind == 'text' else ''}.jsonl":
                  os.path.getsize(out_dir / f"{sp}{'.text' if kind == 'text' else ''}.jsonl")
                  for sp in ("train", "validation") for kind in ("chat", "text")},
        "frontier_targets_allowed": True, "restrictions": RESTRICTIONS,
    }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(out_dir / "README.md", "w", encoding="utf-8") as f:
        f.write(card(manifest))
    manifest["path"] = str(out_dir)
    return manifest


def card(m: dict) -> str:
    def table(d):
        return "\n".join(f"| {k} | {v:,} |" for k, v in d.items()) or "| - | 0 |"
    return f"""# {m['dataset']}

Every captured conversation of {', '.join(m['projects'])}, one example per conversation,
nothing filtered out. Built {m['created']} by `foundry_training.sessions`.

## Files

| File | Format |
|---|---|
| `train.jsonl`, `validation.jsonl` | `{{"messages": [...], "tools": [...], "meta": {{...}}}}`: OpenAI-style chat with tool calls |
| `train.text.jsonl`, `validation.text.jsonl` | `{{"text": "...", "meta": {{...}}}}`: the same examples rendered as Qwen ChatML with `<tool_call>` blocks |

In Unsloth Studio pick the chat files with format `chatml` (or `auto`). If the trainer
does not apply tool calls from `messages`, use the text files and train on the `text` column.

Examples: {m['examples'].get('train', 0):,} train, {m['examples'].get('validation', 0):,} validation,
from {m['conversations']:,} conversations in {m['sessions']:,} sessions.
Longest example: {m['max_tokens']:,} tokens ({m['token_count']}); a longer conversation was cut
between turns into several examples ({m['examples_from_long_conversations']:,} examples came from such cuts).
Tool definitions per example: {'only the tools that example calls' if m.get('tools') != 'all' else 'all of them'}.
{m.get('examples_with_a_shortened_turn', 0):,} examples hold a turn that was shortened in the middle to fit.
Validation is split by task, so a task is never on both sides.

## Labels (in `meta` of every line)

`tier`: how the work was judged. Filter on it if you want only proven work.

| tier | examples |
|---|---|
{table(m['tiers'])}

`tags`: what the conversation did.

| tag | examples |
|---|---|
{table(m['tags'])}

`models`: who wrote the assistant turns (turns written, not examples).

| model | turns |
|---|---|
{table(m['models'])}

Also in `meta`: `task_id`, `session_id`, `role`, `persona`, `labels`, `calls`, `tokens`, `piece` / `pieces`.

## Left out

{table(m['skipped']) if m['skipped'] else 'Nothing.'}

## Use

- This dataset INCLUDES turns written by hosted models as training targets (see `models`
  above). That was the operator's decision; the providers' terms below still apply.
""" + "\n".join(f"- {r}" for r in m["restrictions"]) + "\n"


def main(argv=None) -> int:
    import argparse
    import sys

    from .store import TrainingStore
    ap = argparse.ArgumentParser(prog="foundry_training.sessions", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=os.environ.get("FOUNDRY_TRAINING_STORE", "~/.foundry-training"))
    ap.add_argument("--project", action="append", help="default: every project in the store")
    ap.add_argument("--name", default="")
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--validation", type=float, default=0.05)
    ap.add_argument("--tag", action="append")
    ap.add_argument("--tier", action="append", choices=TIERS)
    ap.add_argument("--no-reasoning", action="store_true")
    ap.add_argument("--tools", choices=("used", "all"), default="used",
                    help="tool definitions per example: the ones it calls (default), or all")
    ap.add_argument("--tokenizer")
    ap.add_argument("--out", help="write the dataset folder under this directory")
    a = ap.parse_args(argv)
    store = TrainingStore(a.store, allow_in_repo=True)
    projects = a.project or store.projects()
    m = export(store, projects, name=a.name, max_tokens=a.max_tokens,
               validation_fraction=a.validation, include_reasoning=not a.no_reasoning,
               tools=a.tools, tags=a.tag, tiers=a.tier, tokenizer_path=a.tokenizer, out_root=a.out,
               log=lambda s: print(time.strftime("%H:%M:%S"), s, flush=True))
    print(json.dumps({k: m[k] for k in ("path", "sessions", "conversations", "examples", "tokens",
                                        "tiers", "tags", "skipped", "files")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
