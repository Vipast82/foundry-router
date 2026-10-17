"""Proactive lanes on the helper journal (lanes.py has the policy and parsers).

Proactive is owned by HelperManager and only does three things:
  ingest()  read the busy primary's transcript (rate limited, GET only),
            classify completed tool results into durable lane_events, snapshot
            ready artifacts and coherent source checkpoints
  plan()    turn events into lane units (one per coalesce key) through the
            same helper_units journal, admission and binding as other helpers
  the rest  packets, results, supersession and delivery cards used by
            helpers.py / engine.py at the existing safe point
Nothing here prompts the primary or runs a tool in the workspace.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from . import lanes as L
from .artifacts import ArtifactStore


class Proactive:
    def __init__(self, mgr):
        self.mgr = mgr
        self.store = mgr.store
        self.clock = mgr.clock
        self.store.conn.executescript(L.SCHEMA)
        self.artifacts = ArtifactStore(self.store, mgr.c.state_dir, clock=mgr.clock)
        self._last_poll: dict[str, float] = {}

    # ------------------------------------------------------------------ ingest

    def _event(self, P, t: dict, ev: dict, sid: str) -> bool:
        now = self.clock()
        n = self.store.x("INSERT OR IGNORE INTO lane_events (event_key, project_id, task_id, run_id, "
                         "session_id, kind, signature, detail, part_id, status, ts, updated_ts) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"{P.id}:{t['task_id']}:{ev['key']}", P.id, t["task_id"], t["run_id"],
                          sid, ev["kind"], ev.get("signature") or "",
                          json.dumps(ev.get("detail") or {}, default=str)[:20000],
                          ev.get("part_id") or "", "new", now, now))
        return bool(n)

    def events(self, pid: str, tid: str, kind: str = "", status: str = "") -> list[dict]:
        sql, args = "SELECT * FROM lane_events WHERE project_id=? AND task_id=?", [pid, tid]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self.store.q(sql + " ORDER BY ts, rowid", args)
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    def _set_event(self, key: str, status: str, unit_id: str = "", detail: Optional[dict] = None):
        self.store.x("UPDATE lane_events SET status=?, unit_id=COALESCE(NULLIF(?,''), unit_id), "
                     "detail=COALESCE(?, detail), tries=tries+1, updated_ts=? WHERE event_key=?",
                     (status, unit_id, json.dumps(detail, default=str)[:20000] if detail is not None
                      else None, self.clock(), key))

    def ingest(self, P, t: dict, spec, ls: dict, force: bool = False) -> int:
        """New durable events from the primary session of this run."""
        sid = t.get("session_id") or ""
        now = self.clock()
        if not sid or (not force and now - self._last_poll.get(sid, -1e18)
                       < ls["transcript_poll_seconds"]):
            return 0
        self._last_poll[sid] = now
        new = 0
        if self._event(P, t, {"kind": "run_start", "key": f"run:{t['run_id']}",
                              "signature": t["run_id"], "detail": {"base": t.get("base_sha")}},
                       sid):
            new += 1
        try:
            msgs = P.client.transcript(sid, directory=P.profile.workspace) or []
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, t["task_id"], "lane_ingest_unavailable", session_id=sid,
                             detail={"error": f"{type(e).__name__}: {e}"[:300]})
            msgs = []
        last_info = None
        for m in msgs:
            if not isinstance(m, dict):
                continue
            info = m.get("info") or {}
            if info.get("role") == "assistant":
                last_info = info
            for p in m.get("parts") or []:
                tv = L.tool_view(p)
                if not tv:
                    continue
                for ev in L.classify(tv, t["task_id"], ls):
                    ev["part_id"] = tv["part_id"]
                    new += self._event(P, t, ev, sid)
        thr = int(ls["lanes"]["handoff"].get("context_threshold") or 0) \
            if L.lane_on(ls, "handoff") else 0
        ctx = L.context_tokens(last_info) if last_info else None
        if thr and ctx is not None and ctx >= thr:
            new += self._event(P, t, {"kind": "context", "key": f"context:{sid}:{thr}",
                                      "signature": str(thr), "detail": {"context_tokens": ctx,
                                                                        "threshold": thr}}, sid)
        try:
            head = P.ws.head()
        except Exception:                                         # noqa: BLE001
            head = ""
        last = (t.get("data") or {}).get("lanes_head")
        if head and last and head != last:
            new += self._event(P, t, {"kind": "commit", "key": f"head:{head}", "signature": head,
                                      "detail": {"from": last, "to": head}}, sid)
        if head and head != last:
            self.store.merge_data(P.id, t["task_id"], lanes_head=head)
        if L.lane_on(ls, "visual") and ls["lanes"]["visual"].get("fs_fallback"):
            v = ls["lanes"]["visual"]
            for r in self.artifacts.scan(project_id=P.id, task_id=t["task_id"], run_id=t["run_id"],
                                         session_id=sid, workspace=P.profile.workspace,
                                         allowed=v["capture_globs"],
                                         debounce_s=float(v["debounce_seconds"]), revision=head,
                                         max_bytes=int(v["max_image_bytes"])):
                if r["status"] == "recorded":
                    a = r["artifact"]
                    new += self._event(P, t, {"kind": "artifact", "key": f"fs:{a['artifact_id']}",
                                              "signature": a["rel_path"],
                                              "detail": {"artifact_id": a["artifact_id"],
                                                         "source": "filesystem"}}, sid)
        self._process_captures(P, t, ls, sid, head)
        return new

    def _process_captures(self, P, t: dict, ls: dict, sid: str, head: str) -> None:
        """Capture events -> immutable artifacts (only while the visual lane is on)."""
        if not L.lane_on(ls, "visual"):
            return
        v = ls["lanes"]["visual"]
        for ev in self.events(P.id, t["task_id"], "capture"):
            if ev["status"] not in ("new", "pending"):
                continue
            rec = ev["detail"]
            r = self.artifacts.record(project_id=P.id, task_id=t["task_id"], run_id=t["run_id"],
                                      session_id=sid, workspace=P.profile.workspace,
                                      path=rec.get("path") or "", allowed=v["capture_globs"],
                                      source="tool_result", revision=head, record=rec,
                                      max_bytes=int(v["max_image_bytes"]))
            if r["status"] == "not_ready":
                st = "pending" if ev["tries"] < 5 else "rejected"
                self._set_event(ev["event_key"], st, detail={**rec, "not_ready": r["reason"]})
                continue
            a = r.get("artifact") or {}
            self._set_event(ev["event_key"], "artifact" if r["status"] == "recorded" else
                            r["status"], detail={**rec, "artifact_id": a.get("artifact_id"),
                                                 "result": r["status"], "reason": r["reason"]})
            self.store.event(P.id, t["task_id"], f"artifact_{r['status']}", session_id=sid,
                             detail={"path": r.get("path"), "artifact": a.get("artifact_id"),
                                     "sha256": a.get("sha256"), "generation": a.get("generation"),
                                     "valid": a.get("valid"), "reason": r["reason"][:300],
                                     "superseded": r.get("superseded")})
            for old in r.get("superseded") or []:
                self.mgr.supersede_artifact(P, t["task_id"], old, a.get("artifact_id"))

    def now(self) -> float:
        return time.time()
