"""Mixed tool turns on the direct path (audit A4).

A model turn can call Foundry-owned tools (persona MCP tools) AND the client's
own tools at once. Foundry runs its own calls right away and hands the client
calls back unchanged, so no client call is dropped and no extra model round is
spent. The client's history then holds the assistant turn with only ITS calls,
so the persona calls and their results are saved here and put back into that
turn on every later request:

  assistant(tool_calls = persona + client calls, in the order the model made them)
  tool results for each call, in the same order (persona results from here,
  client results from the client's history)

Every call is paired with a result in both dialects (OpenAI by tool_call_id,
Ollama by order + tool name), and the rebuilt history is the same on every
later turn — no prompt-cache flip. A missing entry (expired, never saved) just
leaves the history as the client sent it.

Entries live in the `kv` table under "mixed_round:<hash>", keyed by persona +
the client calls' names and arguments, and expire after TTL_SECONDS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

PREFIX = "mixed_round:"
TTL_SECONDS = 7 * 86400


def _args(a) -> str:
    if isinstance(a, str):
        try:
            a = json.loads(a) if a.strip() else {}
        except ValueError:
            return a
    try:
        return json.dumps(a if a is not None else {}, sort_keys=True, ensure_ascii=False,
                          default=str)
    except (TypeError, ValueError):
        return str(a)


def _fn(tc: dict) -> tuple[str, object]:
    """(name, arguments) of a tool call in either shape: Foundry's flat
    {"name", "arguments"} or the wire {"function": {...}}."""
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
    if fn is not None:
        return fn.get("name") or "", fn.get("arguments")
    return tc.get("name") or "", tc.get("arguments")


def key_for(persona: str, client_calls: list[dict]) -> str:
    sig = [[n, _args(a)] for n, a in (_fn(tc) for tc in client_calls)]
    raw = json.dumps([persona, sig], ensure_ascii=False)
    return PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:32]


def save(db, persona: str, calls: list[dict], own_names: set, own_results: list[dict],
         client_calls: list[dict]) -> Optional[str]:
    """Remember one mixed turn. `calls` = every call of the turn in model
    order (flat shape), `own_results` = the tool messages for the Foundry-owned
    ones, `client_calls` = the calls handed back to the client (after any
    pre-judge fix). Returns the key, or None when there is nothing to keep."""
    own = [tc for tc in calls if _fn(tc)[0] in own_names]
    if not own or not client_calls:
        return None
    by_id = {r.get("tool_call_id"): r for r in own_results}
    entry_calls, results = [], []
    for i, tc in enumerate(own):
        name, args = _fn(tc)
        cid = tc.get("id") or ("foundry_" + hashlib.sha1(
            f"{i}:{name}:{_args(args)}".encode()).hexdigest()[:16])
        r = (by_id.get(tc["id"]) if tc.get("id") else None) or (
            own_results[i] if i < len(own_results) else None)
        entry_calls.append({"id": cid, "name": name,
                            "arguments": args if args is not None else {}})
        results.append({"role": "tool", "tool_call_id": cid, "name": name,
                        "content": str((r or {}).get("content") or "(no result)"),
                        **({"images": r["images"]} if r and r.get("images") else {})})
    # model order: "own" / "client" slots
    order = ["own" if _fn(tc)[0] in own_names else "client" for tc in calls]
    n_client_slots = order.count("client")
    if n_client_slots != len(client_calls):      # a pre-judge fix dropped/merged calls
        order = ["own"] * len(own) + ["client"] * len(client_calls)
    key = key_for(persona, client_calls)
    now = time.time()
    try:
        db.kv_set(key, json.dumps({"ts": now, "persona": persona, "order": order,
                                   "calls": entry_calls, "results": results},
                                  ensure_ascii=False, default=str))
        db.execute("DELETE FROM kv WHERE key LIKE ? AND "
                   "CAST(json_extract(value, '$.ts') AS REAL) < ?",
                   (PREFIX + "%", now - TTL_SECONDS))
    except Exception:                                            # noqa: BLE001
        log.exception("could not save a mixed tool round")
        return None
    return key


def _wire_call(c: dict) -> dict:
    return {"id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": c.get("arguments") or {}}}


def restore(db, persona: str, messages: list[dict]) -> tuple[list[dict], int]:
    """Put saved persona rounds back into the history. Returns (messages,
    number of turns rebuilt). Idempotent: a turn that already holds the
    persona calls is left alone."""
    out: list[dict] = []
    n = 0
    i = 0
    msgs = list(messages or [])
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            out.append(m)
            i += 1
            continue
        entry = None
        try:
            raw = db.kv_get(key_for(persona, m["tool_calls"]))
            entry = json.loads(raw) if raw else None
        except Exception:                                        # noqa: BLE001
            entry = None
        if not entry or entry.get("persona") != persona:
            out.append(m)
            i += 1
            continue
        own_ids = {c["id"] for c in entry["calls"]}
        if own_ids & {tc.get("id") for tc in m["tool_calls"]}:
            out.append(m)                       # already rebuilt
            i += 1
            continue
        # the client's results for this turn
        j = i + 1
        client_results = []
        while j < len(msgs) and msgs[j].get("role") == "tool":
            client_results.append(msgs[j])
            j += 1
        own_calls = list(entry["calls"])
        own_res = {r["tool_call_id"]: r for r in entry["results"]}
        client_calls = list(m["tool_calls"])
        calls, results = [], []
        unmatched = list(client_results)
        for slot in entry.get("order") or []:
            if slot == "own" and own_calls:
                c = own_calls.pop(0)
                calls.append(_wire_call(c))
                results.append(own_res.get(c["id"]) or {"role": "tool", "tool_call_id": c["id"],
                                                        "name": c["name"], "content": "(no result)"})
            elif slot == "client" and client_calls:
                tc = client_calls.pop(0)
                calls.append(tc)
                hit = next((r for r in unmatched if tc.get("id")
                            and r.get("tool_call_id") == tc.get("id")), None) \
                    or (unmatched[0] if unmatched else None)
                if hit is not None:
                    unmatched.remove(hit)
                    results.append(hit)
        for c in own_calls:                    # order list shorter than expected
            calls.append(_wire_call(c))
            results.append(own_res.get(c["id"]))
        calls.extend(client_calls)
        results.extend(unmatched)
        out.append({**m, "tool_calls": calls})
        out.extend(r for r in results if r)
        n += 1
        i = j
    return out, n
