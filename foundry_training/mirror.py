"""Send a workstation's training records to the router (v0.181.162).

    python -m foundry_training.mirror --url http://192.168.0.112:11435 \
        [--store ~/.foundry-training] [--source kilo --source coordinator --source git] \
        [--key-env FOUNDRY_TRAINING_KEY] [--batch 300] [--max-minutes 0] [--dry-run]

The router holds every model call it routed, but what the coding harness did
(Kilo), what the coordinator decided and what was committed (git) is made here,
and the router cannot read this machine's database or checkout. This walks the
local store's events for those sources, oldest first, and posts them to

    POST /v1/foundry/training/events

The router remembers where each source got to, so this can be stopped and run
again at any time; an event that arrives twice is ignored. Events are sent as
they were written (their text put back together first), and the router stores
each message once again at its end.

`foundry` events are left out by default: the router made them itself from its
own capture. `--source foundry` sends them anyway (it is harmless, just slower).

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SOURCES = ("kilo", "opencode", "coordinator", "git", "gate")
FIELDS = ("event_id", "schema_version", "project_id", "repository_id", "task_id", "session_id",
          "parent_session_id", "request_id", "tool_call_id", "source_adapter", "source_record_id",
          "part", "timestamp", "seq", "event_type", "actor_role", "actor_model",
          "requested_persona", "actual_backend", "actual_model", "source_revision",
          "artifact_references", "capture_status", "redaction_status", "verification_status",
          "facts", "inferred", "raw_ref")
JSON_FIELDS = ("artifact_references", "facts", "inferred")
SKIP = {"event_id", "schema_version"}        # the router works them out itself


def paths(admin: bool) -> tuple:
    """(cursor path, send path). The admin API is the way in for a machine that
    reaches the router's admin panel but holds no client key of its own."""
    base = "/admin/api/training/events" if admin else "/v1/foundry/training/events"
    return base + "/cursor", base


def _send(url: str, path: str, key: str, body=None, method="GET", timeout=120):
    req = urllib.request.Request(url.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body, default=str).encode())
    if body is not None:
        req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def rows_after(store, source: str, after: int, limit: int) -> list:
    """`limit` events of one source after rowid `after`, their text put back."""
    cols = ", ".join(("rowid",) + FIELDS)
    out = []
    for r in store.norm.q(f"SELECT {cols} FROM events WHERE source_adapter=? AND rowid > ? "
                          f"AND deleted=0 ORDER BY rowid LIMIT ?", (source, after, limit)):
        e = {k: r[k] for k in FIELDS if k not in SKIP}
        for k in JSON_FIELDS:
            try:
                e[k] = store.unpack(json.loads(r[k] or ("[]" if k == "artifact_references" else "{}")))
            except ValueError:
                e[k] = [] if k == "artifact_references" else {}
        out.append((r["rowid"], e))
    return out


def run(store, url: str, key: str, sources=DEFAULT_SOURCES, batch: int = 300,
        max_minutes: float = 0, dry_run: bool = False, log=print,
        sender: str = "workstation", admin: bool = False) -> dict:
    cursor_path, send_path = paths(admin)
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else 0
    report = {"sources": {}, "sent": 0, "errors": []}
    for source in sources:
        got = _send(url, f"{cursor_path}?source={source}&sender={sender}", key)
        after = int(got.get("cursor") or 0)
        left = store.norm.q("SELECT COUNT(*) AS n FROM events WHERE source_adapter=? AND rowid > ? "
                            "AND deleted=0", (source, after))[0]["n"]
        s = report["sources"][source] = {"from": after, "to_send": left, "sent": 0, "batches": 0}
        if not left:
            log(f"{source}: nothing new (the router has everything up to {after})")
            continue
        log(f"{source}: {left:,} event(s) to send, starting after {after}")
        while True:
            if deadline and time.monotonic() > deadline:
                s["stopped"] = "time limit"
                log(f"{source}: time limit reached; the next run continues from {after}")
                break
            rows = rows_after(store, source, after, batch)
            if not rows:
                break
            if dry_run:
                mb = len(json.dumps([e for _, e in rows], default=str)) / 1e6
                log(f"{source}: would send {len(rows)} event(s), {mb:.1f} MB (dry run)")
                s["sent"] += len(rows)
                after = rows[-1][0]
                if s["sent"] >= batch * 2:
                    break
                continue
            try:
                _send(url, send_path, key, method="POST",
                      body={"source": source, "sender": sender, "cursor": str(rows[-1][0]),
                            "events": [e for _, e in rows]})
            except urllib.error.HTTPError as e:
                detail = (e.read() or b"")[:300].decode("utf-8", "replace")
                report["errors"].append(f"{source}: HTTP {e.code} {detail}")
                log(f"{source}: HTTP {e.code} {detail}")
                break
            except Exception as e:                                # noqa: BLE001
                report["errors"].append(f"{source}: {type(e).__name__}: {e}")
                log(f"{source}: {type(e).__name__}: {e}")
                break
            after = rows[-1][0]
            s["sent"] += len(rows)
            s["batches"] += 1
            report["sent"] += len(rows)
            if s["batches"] % 20 == 0:
                log(f"{source}: {s['sent']:,} of {left:,} sent")
        log(f"{source}: {s['sent']:,} event(s) sent")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_training.mirror", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the router, e.g. http://192.168.0.112:11435")
    ap.add_argument("--store", default=os.environ.get("FOUNDRY_TRAINING_STORE", "~/.foundry-training"))
    ap.add_argument("--source", action="append", dest="sources",
                    help=f"default: {', '.join(DEFAULT_SOURCES)}")
    ap.add_argument("--key-env", default="FOUNDRY_TRAINING_KEY")
    ap.add_argument("--sender", default="workstation",
                    help="this machine and store, as the router remembers its position "
                         "(a different store on the same machine needs a different name)")
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop after this long; the next run continues (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--admin", action="store_true",
                    help="send through the router's admin API instead of a client key "
                         "(for a machine that already reaches the admin panel)")
    a = ap.parse_args(argv)
    from .store import TrainingStore
    key = os.environ.get(a.key_env, "")
    if not key and not a.dry_run and not a.admin:
        print(f"error: no key in {a.key_env} (the router needs a client key with the "
              f"orchestration scope)", file=sys.stderr)
        return 2
    store = TrainingStore(a.store, allow_in_repo=True)
    out = run(store, a.url, key, a.sources or list(DEFAULT_SOURCES), a.batch, a.max_minutes,
              a.dry_run, log=lambda s: print(time.strftime("%H:%M:%S"), s, flush=True),
              sender=a.sender, admin=a.admin)
    print(json.dumps(out, indent=1))
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
