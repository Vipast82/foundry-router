"""Preflight, research, evidence reconciliation and handoff lanes (stage 5).

All four are optional advisory lanes on the same journal. Deterministic code
does what code can: plan checklist parsing, evidence file inventory, gate
revision binding, official-source provenance and the research cache. Models
only judge.

  preflight   at a new run of an APPROVED task only. When the project's
              implement prompt already runs the au-planner subagent, that is
              the existing planner artifact: preflight is skipped, not
              duplicated.
  research    one unit per API symbol per task; an answer with an official
              source (create.roblox.com / luau.org / luau-lang.org) is cached
              across tasks for cache_hours and re-served without inference.
  evidence    at a gate verdict: a checklist-to-evidence map computed here
              (ticked/unticked items, evidence files present, gate HEAD vs
              current HEAD) goes into the packet; the helper reconciles and
              names contradictions. It never runs or waives a check.
  handoff     when the primary's context crosses the threshold: a compact
              handoff built from the existing plan NEXT line/checklist, exact
              revision and helper results in flight. It is offered to the next
              session prompt only while HEAD is unchanged.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import lanes as L

OFFICIAL = ("https://create.roblox.com/", "https://luau.org/", "https://luau-lang.org/",
            "https://www.luau.org/", "https://rfcs.luau.org/")


def checklist(spec_text: str) -> list[dict]:
    out = []
    for line in (spec_text or "").splitlines():
        m = re.match(r"^\s*-\s\[([ xX])\]\s*(.+)$", line)
        if m:
            out.append({"done": m.group(1).lower() == "x", "item": m.group(2).strip()[:300]})
    return out


class AdvisoryMixin:
    # ------------------------------------------------------------------ preflight

    def _preflight(self, P, t, spec, ls, ev):
        if P.profile.roles.get("planner") and "au-planner" in (
                P.adapter.implement_prompt(spec, {"run_id": t["run_id"], "attempt": t["attempt"],
                                                  "base": t.get("base_sha")}) or ""):
            self.store.event(P.id, t["task_id"], "lane_reused", detail={
                "lane": "preflight", "reason": "the approved implement prompt already runs the "
                                               "au-planner subagent; no duplicate preflight"})
            return None
        snap = self._snap(P, t, spec)
        uid, how = self.add(P, t, "preflight", f"run:{t['run_id']}", {
            "checklist": json.dumps(checklist(spec.spec), indent=1)[:4000],
            "plan_next": "\n".join(l for l in spec.spec.splitlines() if "NEXT" in l)[:1000],
            "snapshot": snap.get("snapshot_id"), "snapshot_status": snap["status"],
            "files": sorted(snap.get("manifest") or {})}, ls, files=spec.scope,
            snapshot_id=snap.get("snapshot_id") or "", event_key=ev["event_key"])
        return uid

    # ------------------------------------------------------------------ research

    def _plan_api(self, P, t, spec, ls, ev):
        if not L.lane_on(ls, "research"):
            return self._set_event(ev["event_key"], "ignored")
        sym = ev["signature"]
        hours = float(ls["lanes"]["research"].get("cache_hours") or 168)
        hit = self.store.q("SELECT * FROM lane_cache WHERE cache_key=? AND project_id=? AND ts >= ?",
                           (f"research:{sym}", P.id, self.clock() - hours * 3600))
        if hit:
            uid, how = self.add(P, t, "research", sym, {"symbol": sym, "cached_from": hit[0]["unit_id"],
                                                         "question": ev["detail"].get("line")}, ls)
            if how == "created":
                env = json.loads(hit[0]["result"] or "{}")
                self.store.x("UPDATE helper_units SET status='done', persona='cache', backend='cache', "
                             "reason=?, envelope=?, feedback='waiting', started_ts=?, finished_ts=?, "
                             "result=? WHERE unit_id=?",
                             (f"cached answer from {hit[0]['unit_id']} (no inference)",
                              json.dumps({**env, "cached_from": hit[0]["unit_id"]}), self.clock(),
                              self.clock(), env.get("summary") or "", uid))
            return self._done_event(ev, uid, how)
        snap = self._snap(P, t, spec)
        uid, how = self.add(P, t, "research", sym, {
            "symbol": sym, "question": f"Error seen by the primary: {ev['detail'].get('line')}\n"
                                       f"What is the current, official API behavior for {sym}? "
                                       f"Use at most {int(ls['lanes']['research'].get('max_lookups') or 6)}"
                                       f" documentation lookups; cite each source URL.",
            "snapshot": snap.get("snapshot_id"), "files": sorted(snap.get("manifest") or {})},
            ls, files=spec.scope, snapshot_id=snap.get("snapshot_id") or "",
            event_key=ev["event_key"])
        self._done_event(ev, uid, how)

    def _after_research(self, P, u, env):
        srcs = [s for s in env.get("sources") or [] if s.startswith("http")]
        official = [s for s in srcs if s.startswith(OFFICIAL)]
        cur = json.loads(self.store.q("SELECT envelope FROM helper_units WHERE unit_id=?",
                                      (u["unit_id"],))[0]["envelope"] or "{}")
        cur.setdefault("checks", {})["provenance"] = {
            "sources": len(srcs), "official": official[:6],
            "ok": bool(official)}
        if not official:
            cur["limitations"] = ((cur.get("limitations") or "") + " No official Roblox/Luau "
                                  "documentation source was cited: treat as unverified.").strip()
        self.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?",
                     (json.dumps(cur, default=str)[:80000], u["unit_id"]))
        if official:
            sym = json.loads(u.get("packet") or "{}").get("symbol") or ""
            self.store.x("INSERT INTO lane_cache (cache_key, project_id, lane, unit_id, result, ts) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                         "unit_id=excluded.unit_id, result=excluded.result, ts=excluded.ts",
                         (f"research:{sym}", P.id, "research", u["unit_id"],
                          json.dumps({k: cur.get(k) for k in ("summary", "findings", "sources",
                                                              "next_action", "limitations",
                                                              "structured")}), self.clock()))

    # ------------------------------------------------------------------ evidence

    def evidence_map(self, P, t, spec, gate_line: str) -> dict:
        """Deterministic checklist/evidence facts at the current revision."""
        ws = P.profile.workspace
        try:
            head = P.ws.head()
        except Exception:                                         # noqa: BLE001
            head = ""
        items = checklist(spec.spec)
        ev_dir = os.path.join(ws, "docs", "evidence", spec.task_id)
        files = sorted(os.listdir(ev_dir)) if os.path.isdir(ev_dir) else []
        raw = os.path.join(ws, "docs", "progress", f"{spec.task_id}_EVIDENCE_RAW.log")
        gate_head, verdicts = "", []
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            verdicts = re.findall(r"^GATE %s (PASS|FAIL)\b" % re.escape(spec.task_id), txt, re.M)
            heads = re.findall(r"^PASS: GATE HEAD ([0-9a-f]{7,40})", txt, re.M)
            gate_head = heads[-1] if heads else ""
        problems = []
        if gate_head and head and not head.startswith(gate_head):
            problems.append(f"the last gate ran at {gate_head}, current HEAD is {head[:12]} "
                            f"(wrong revision unless only evidence files changed)")
        if verdicts and gate_line and gate_line.split()[2:3] != verdicts[-1:]:
            problems.append(f"the tool output said '{gate_line[:80]}' but the evidence log's last "
                            f"verdict is GATE {spec.task_id} {verdicts[-1]}")
        open_items = [i["item"] for i in items if not i["done"]]
        claimed = (gate_line.split()[2:3] == ["PASS"]) or verdicts[-1:] == ["PASS"]
        if claimed and open_items:
            problems.append(f"gate PASS while {len(open_items)} checklist item(s) are unticked")
        if "visual" in spec.tags and not [f for f in files if f.lower().endswith((".png", ".jpg"))
                                          and "_INVALID" not in f]:
            problems.append("visual task without a valid evidence image")
        return {"head": head, "checklist": items, "evidence_files": files[:60],
                "evidence_log": os.path.isfile(raw), "gate_head": gate_head,
                "verdicts": verdicts[-5:], "problems": problems}

    def _plan_gate(self, P, t, spec, ls, ev):
        emap = self.evidence_map(P, t, spec, ev["detail"].get("line") or "")
        self.store.event(P.id, t["task_id"], "evidence_map", detail={
            "head": emap["head"], "problems": emap["problems"][:10],
            "unticked": len([i for i in emap["checklist"] if not i["done"]])})
        if not L.lane_on(ls, "evidence"):
            return self._set_event(ev["event_key"], "ignored", detail={**ev["detail"],
                                                                       "evidence_map": emap})
        uid, how = self.add(P, t, "evidence", f"gate:{emap['head']}:{ev['signature']}", {
            "gate_line": ev["detail"].get("line"),
            "checklist": json.dumps(emap["checklist"], indent=1)[:5000],
            "evidence_files": "\n".join(emap["evidence_files"]),
            "notes": "Deterministic checks by the coordinator (facts, not opinions):\n- "
                     + ("\n- ".join(emap["problems"]) or "no contradiction found by code"),
            "deterministic": emap}, ls, files=spec.scope, event_key=ev["event_key"])
        self._done_event(ev, uid, how)

    # ------------------------------------------------------------------ handoff

    def _plan_context(self, P, t, spec, ls, ev):
        if not L.lane_on(ls, "handoff"):
            return self._set_event(ev["event_key"], "ignored")
        try:
            dirty = [p for _, p in P.ws.status()][:40]
            head = P.ws.head()
        except Exception:                                         # noqa: BLE001
            dirty, head = [], ""
        units = [u for u in self.mgr.units(P.id, t["task_id"]) if u.get("lane") and
                 u["status"] in ("running", "done", "delivered")]
        uid, how = self.add(P, t, "handoff", ev["session_id"] or t["session_id"], {
            "plan_next": "\n".join(l for l in spec.spec.splitlines() if "NEXT" in l)[:1500],
            "checklist": json.dumps(checklist(spec.spec), indent=1)[:4000],
            "notes": f"Exact revision: {head}. Uncommitted files: {', '.join(dirty) or 'none'}.",
            "helper_results": "\n".join(f"- {u['unit_id']} {u['lane']} {u['status']} "
                                        f"{u.get('feedback') or ''}" for u in units)[:3000],
            "context_tokens": ev["detail"].get("context_tokens"), "head": head},
            ls, files=spec.scope, event_key=ev["event_key"])
        self._done_event(ev, uid, how)

    def handoff_note(self, P, tid: str) -> tuple:
        """(text, unit_id) of the latest structured handoff whose revision is
        still HEAD, for the next session prompt; else ("", "")."""
        try:
            head = P.ws.head()
        except Exception:                                         # noqa: BLE001
            return "", ""
        rows = self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=? AND "
                            "lane='handoff' AND status IN ('done','delivered') ORDER BY "
                            "finished_ts DESC LIMIT 1", (P.id, tid))
        if not rows:
            return "", ""
        u = rows[0]
        pk = json.loads(u.get("packet") or "{}")
        env = json.loads(u.get("envelope") or "{}")
        if not env.get("structured") or pk.get("head") != head:
            return "", ""
        return ("## Handoff prepared by a read-only helper (advisory)\nAt revision "
                f"{head[:12]} ({u['unit_id']}, {u.get('persona')}): {env.get('summary') or ''}\n"
                + (f"Next: {env.get('next_action')}\n" if env.get("next_action") else "")
                + (f"Limitations: {env.get('limitations')}\n" if env.get("limitations") else "")
                + "Verify it against the plan and the code; the plan file stays the memory.\n",
                u["unit_id"])

    def now_ts(self) -> float:
        return time.time()
