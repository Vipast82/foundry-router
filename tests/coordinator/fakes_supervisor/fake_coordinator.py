"""A tiny fake coordinator for supervisor tests.

Run with cwd = the (fake) router checkout:

    python fake_coordinator.py --state <dir>

It writes <state>/coordinator.pid and, every 0.1 s, the heartbeat
<state>/coordinator.heartbeat ({pid, ts, ok, build}). Its behavior comes from
files in the checkout it runs from, so the build installed decides it:

  BUILD        its build label (reported in the heartbeat)
  FAIL_HEALTH  present: the heartbeat says ok: false
  NO_HEARTBEAT present: never writes a heartbeat
  IGNORE_STOP  present: ignores the stop-request file (must be killed)
  SCHEMA.json  {meta key: value} written into <state>/coordinator.sqlite at start
  LEAD_NOT_READY  present: the heartbeat says lead_ready: false
  LEAD_UNKNOWN    present: the heartbeat says lead_ready: null (a readiness error)

The heartbeat's `version` is the __version__ of foundry_router/__init__.py in
the checkout (empty when absent). --pid-json writes the pid file with
supervisor.write_pid_file ({pid, created, exe}); the default is the legacy
bare pid.

It exits when <state>/coordinator.stop appears (unless IGNORE_STOP), and
appends "start <pid> <build>" / "stop <pid>" lines to <state>/fake_events.log.
"""

import argparse
import json
import os
import sqlite3
import sys
import time


def _read(name, default=""):
    try:
        with open(name, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--max-seconds", type=float, default=120)
    ap.add_argument("--pid-json", action="store_true")
    a = ap.parse_args()
    state = a.state
    pid = os.getpid()
    build = _read("BUILD", "unknown")
    events = os.path.join(state, "fake_events.log")
    with open(events, "a", encoding="utf-8") as fh:
        fh.write(f"start {pid} {build}\n")
    if a.pid_json:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))))
        from foundry_coordinator.supervisor import write_pid_file
        write_pid_file(os.path.join(state, "coordinator.pid"))
    else:
        with open(os.path.join(state, "coordinator.pid"), "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    version = ""
    for line in _read(os.path.join("foundry_router", "__init__.py")).splitlines():
        if line.startswith("__version__"):
            version = line.split("=", 1)[1].strip().strip("\"'")
    lead_ready = None if os.path.exists("LEAD_UNKNOWN") else not os.path.exists("LEAD_NOT_READY")
    schema = _read("SCHEMA.json")
    if schema:
        con = sqlite3.connect(os.path.join(state, "coordinator.sqlite"), timeout=10)
        for k, v in json.loads(schema).items():
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (k, str(v)))
        con.commit()
        con.close()
    fail = os.path.exists("FAIL_HEALTH")
    quiet = os.path.exists("NO_HEARTBEAT")
    ignore_stop = os.path.exists("IGNORE_STOP")
    stop = os.path.join(state, "coordinator.stop")
    hb = os.path.join(state, "coordinator.heartbeat")
    end = time.time() + a.max_seconds
    while time.time() < end:
        if not ignore_stop and os.path.exists(stop):
            with open(events, "a", encoding="utf-8") as fh:
                fh.write(f"stopreq {pid} {_read(stop)}\n")
            break
        if not quiet:
            tmp = hb + f".{pid}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"pid": pid, "ts": time.time(), "ok": not fail, "build": build,
                           "detail": "fake health failure" if fail else "",
                           "version": version, "lead_ready": lead_ready,
                           "lead_detail": "" if lead_ready else "fake lead readiness"}, fh)
            try:
                os.replace(tmp, hb)
            except OSError:
                pass
        time.sleep(0.1)
    with open(events, "a", encoding="utf-8") as fh:
        fh.write(f"stop {pid}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
