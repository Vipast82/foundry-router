"""Fake MCP stdio server for the studio gate tests (no Studio, stdlib only).

    python fake_mcp_server.py --kind official|community [--scenario rules.json] [--log calls.jsonl]

Speaks newline-delimited JSON-RPC like the real servers: initialize, tools/list,
tools/call. --kind picks the tool shapes (official: list_roblox_studios, execute_luau
with studio_id, screen_capture JPEG, get_studio_state, get_console_output;
community: capture_screenshot PNG + JSON text, eval_server_runtime,
eval_client_runtime, execute_luau, solo_playtest).
Scenario rules (first match wins): {"rules": [{"tool": "execute_luau", "match": "<regex over
json.dumps(arguments)>", "text": "...", "is_error": false, "sleep": 0, "png": [w, h],
"exit": false}]}. Every tools/call is appended to --log as JSON.
"""

import argparse
import base64
import json
import re
import struct
import sys
import time
import zlib

OFFICIAL = ["list_roblox_studios", "execute_luau", "screen_capture", "get_studio_state",
            "start_stop_play", "get_console_output", "skill", "subagent"]
COMMUNITY = ["capture_screenshot", "eval_server_runtime", "eval_client_runtime", "execute_luau",
             "solo_playtest", "capture_device_matrix", "get_connected_instances",
             "set_device_simulator"]
# The play client's viewport before set_device_simulator lands on it: the Edit-time device
# does not hold in play (GF-GATE1 2026-10-28 measured 1920x1060).
PLAY_VIEWPORT_DEFAULT = (1920, 1060)


def tally_results(code: str) -> dict:
    """The {[n] = [[\\n...]]} table a studio_gate tally call carries."""
    return {int(m.group(1)): m.group(3)
            for m in re.finditer(r"\[(\d+)\] = \[(=*)\[\n(.*?)\]\2\]", code, re.S)}


def mirror_tally(code: str, spec: dict) -> str:
    """A pure-Python GateRunner.tally (countLines + the bootBad rule, GateRunner.lua
    757-889), so tests prove the RUNNER fails closed instead of trusting a canned tally.
    spec: {"label", "boot": [1, 2], "suites": [{"id", "calls", "expect", "designedFails",
    "offstudio"}]}."""
    results = tally_results(code)
    boot_bad = any("FAIL: GATE boot" in results.get(n, "") for n in spec.get("boot", [1, 2]))
    out, ok, skipped, failed = [], 0, 0, 0
    for s in spec["suites"]:
        p = f = c = sk = 0
        for n in s["calls"]:
            raw = results.get(n)
            if raw is None:
                continue
            if s.get("offstudio"):
                sk += 1
                continue
            for line in raw.split("\n"):
                if line.startswith("PASS: "):
                    p += 1
                elif line.startswith("FAIL: "):
                    f += 1
                if line[:1] and not line[:1].isspace():
                    c += 1
        want, dfail = s.get("expect"), s.get("designedFails", 0)
        if c == 0 and sk > 0:
            v, why = "SKIP", "offstudio suite"
        elif c == 0:
            v, why = "FAIL", "no captured lines (no evidence)"
        elif p == want and f == dfail:
            v, why = "PASS", None
        else:
            v, why = "FAIL", f"pass={p} (want {want}), fail={f} (want designedFails={dfail})"
        ok, skipped, failed = ok + (v == "PASS"), skipped + (v == "SKIP"), failed + (v == "FAIL")
        out.append(f"SUITE {s['id']} pass={p} fail={f} expect={want} designedFails={dfail} VERDICT {v}"
                   + (f" — {why}" if why else ""))
        out.append(f"{v}: GATE suite {s['id']}" + (f" — {why}" if why else ""))
    label = spec.get("label", "subset:ui")
    out.append(f"GATE regression {label} — pass={ok} skip={skipped} fail={failed} of {len(spec['suites'])} suite(s)")
    if boot_bad:
        out.append(f"FAIL: GATE regression {label} — boot gate failed (see CALL 1/2 raw output)")
    elif failed == 0:
        out.append(f"PASS: GATE regression {label}")
    else:
        out.append(f"FAIL: GATE regression {label} — {failed} suite(s) failing")
    return "\n".join(out)


def png(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True)
    ap.add_argument("--scenario")
    ap.add_argument("--log")
    ap.add_argument("--slow-init", type=float, default=0)
    ap.add_argument("--state", help="JSON file shared by both fakes (simulated device size)")
    ap.add_argument("--noise", action="store_true",
                    help="print non-object JSON lines and a server->client ping before replying")
    ap.add_argument("--child-pidfile", help="spawn a sleeping grandchild, write its pid here")
    ap.add_argument("--ignore-eof", action="store_true", help="keep running after stdin closes")
    a = ap.parse_args()
    if a.child_pidfile:
        import subprocess
        ch = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(a.child_pidfile, "w") as f:
            f.write(str(ch.pid))
    rules = []
    if a.scenario:
        with open(a.scenario, encoding="utf-8") as f:
            rules = json.load(f).get("rules") or []
    tools = OFFICIAL if a.kind == "official" else COMMUNITY
    state = {"w": 1920, "h": 1080}      # the simulated device (a size seen in execute_luau code)

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue                                           # notification
        if method == "initialize":
            time.sleep(a.slow_init)
            if a.noise:                    # must not kill the client's reader (H8)
                for junk in ("42", "[1, 2]", '"text"', "null"):
                    sys.stdout.write(junk + "\n")
                send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": f"fake-{a.kind}", "version": "0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": t, "inputSchema": {"type": "object"}} for t in tools]}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if a.log:
                with open(a.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"kind": a.kind, "tool": name, "args": args}) + "\n")
            if name not in tools:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
                continue
            if a.kind == "official" and name != "list_roblox_studios" and args.get("studio_id") != "S1":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "studio_id required"}], "isError": True}})
                continue
            blob = json.dumps(args)
            m = re.search(r"AU_(\d+)x(\d+)", str(args.get("code") or ""))
            if a.state:
                try:
                    with open(a.state, encoding="utf-8") as f:
                        state.update(json.load(f))
                except (OSError, ValueError):
                    pass
            if m:                                              # device switch: remember the size
                state["w"], state["h"] = int(m.group(1)), int(m.group(2))
                if a.state:
                    with open(a.state, "w", encoding="utf-8") as f:
                        json.dump(state, f)
            if name == "solo_playtest" and args.get("action") == "stop" \
                    and (state.pop("pw", None), state.pop("ph", None)) != (None, None) and a.state:
                with open(a.state, "w", encoding="utf-8") as f:   # the play client is gone
                    json.dump(state, f)
            used = state.setdefault(f"used.{a.kind}", {})
            rule = next((r for i, r in enumerate(rules) if r.get("tool", name) == name
                         and re.search(r.get("match", ""), blob, re.S)
                         and ("times" not in r or used.get(str(i), 0) < r["times"])), None)
            if rule is not None and "times" in rule:          # a rule good for N matches only
                i = str(rules.index(rule))
                used[i] = used.get(i, 0) + 1
                if a.state:
                    with open(a.state, "w", encoding="utf-8") as f:
                        json.dump(state, f)
            content, is_err = [], False
            if rule:
                if rule.get("exit"):
                    sys.exit(3)
                time.sleep(rule.get("sleep", 0))
                text = rule.get("text", "").replace("{W}", str(state["w"])).replace("{H}", str(state["h"]))
                if rule.get("mirror"):
                    text = mirror_tally(str(args.get("code") or ""), rule["mirror"])
                content.append({"type": "text", "text": text})
                is_err = bool(rule.get("is_error"))
                if rule.get("png"):
                    w, h = rule["png"] if rule["png"] != "state" else (state["w"], state["h"])
                    content.append({"type": "image", "mimeType": "image/png",
                                    "data": base64.b64encode(png(w, h)).decode()})
            elif name == "set_device_simulator":
                res = args.get("resolution") or {}
                if args.get("target") == "client-1" and res.get("width") and res.get("height"):
                    state["pw"], state["ph"] = int(res["width"]), int(res["height"])
                    if a.state:
                        with open(a.state, "w", encoding="utf-8") as f:
                            json.dump(state, f)
                content.append({"type": "text", "text": json.dumps({"success": True})})
            elif name == "eval_client_runtime" and "ViewportSize" in str(args.get("code") or ""):
                pw = state.get("pw", PLAY_VIEWPORT_DEFAULT[0])
                ph = state.get("ph", PLAY_VIEWPORT_DEFAULT[1])
                content.append({"type": "text", "text": json.dumps(
                    {"ok": True, "bridge": "ok", "result": f"VIEWPORT {pw}x{ph}", "output": []})})
            elif name == "get_connected_instances":
                content.append({"type": "text", "text": json.dumps(
                    {"instances": [{"id": "I1", "placeName": "AnimalUprising", "placeId": 1}],
                     "multiplayerGroups": []})})
            elif name == "list_roblox_studios":
                content.append({"type": "text", "text": json.dumps(
                    {"studios": [{"id": "S1", "name": "AnimalUprising"}]})})
            elif name == "screen_capture":
                content.append({"type": "image", "mimeType": "image/jpeg",
                                "data": base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()})
            elif name == "capture_screenshot":
                w, h = state["w"], state["h"]
                content.append({"type": "text", "text": json.dumps({"width": w, "height": h})})
                content.append({"type": "image", "mimeType": "image/png",
                                "data": base64.b64encode(png(w, h)).decode()})
            else:
                content.append({"type": "text", "text": ""})
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": is_err}})
        elif method is None:
            continue                                           # a reply to our ping
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no method"}})
    if a.ignore_eof:
        time.sleep(120)


if __name__ == "__main__":
    main()
