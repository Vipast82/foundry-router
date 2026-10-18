"""Visual observer -> code proposer -> primary (proactive lanes, stage 3).

The observer is ONE direct Foundry chat call (like the coordinator's reviews),
bound first with the optional purpose visual_observation, carrying the actual
snapshotted image bytes as an OpenAI `image_url` data URI (Foundry's facade
turns those into native image input). It never runs in Kilo: no tools, no
files, no shell, no Studio, no delegation are reachable from it.

"Can the model see it?" is checked, not assumed:
  before   every route Foundry reports for the visual persona must say
           vision=true (admission answer, Foundry >= 0.118.0). Missing
           modality information is "unverifiable" and the unit waits; a
           text-only route is refused (it would silently drop the image).
  after    the served model must be one of those vision routes, and the model
           must confirm image_seen. Otherwise the result is discarded as "no
           image inspection" — never a visual PASS.

A finding is an observation; its code cause is a hypothesis. Only
high-confidence, in-scope findings at or above the severity floor become a
code-proposal unit (depth 2, which never spawns anything); uncertain ones go to
the primary as observations. A live visual suggestion is never Victor's visual
approval, a gate verdict or an independent review.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import lanes as L
from .adapters.foundry import FoundryDenied, FoundryError, admitted_routes, pinned_meta

OBSERVER_SYSTEM = """You are a read-only visual observer for a Roblox UI sprint. You receive one captured frame (and, when available, the approved design reference) as images, plus the acceptance criteria and capture metadata.
Report what is VISIBLE: defects against the criteria/reference (clipping, overlap, alignment, safe area, scale, missing or wrong elements, text). For each finding give severity, confidence (0-1), the image region, the observation, and separately any inferred code cause as a hypothesis with the likely file paths. Suggest how the primary should verify.
If the expected subject is missing, the frame is invalid or occluded, or you cannot actually see an image, say so (image_seen false / validity) and report no defects. Never declare the UI approved: a person approves visual work.
Copy artifact_id and source_snapshot_id exactly as the request gives them: the first names the image you judged, the second the source code it was bound to.
Answer with ONE fenced block:
```helper-result
{"assignment": "<id>", "artifact_id": "<artifact id from the request>", "source_snapshot_id": "<source snapshot id from the request>", "image_seen": true, "subject_present": true, "validity": "ok | invalid | occluded | missing_subject", "summary": "<one line>", "findings": [{"id": "V1", "severity": "high|medium|low|info", "confidence": 0.0, "region": "<where>", "observed": "<visible fact>", "hypothesis": "<inferred cause or empty>", "paths": ["<source path>"], "in_scope": true, "verify": "<how to check>"}], "limitations": "", "next_action": ""}
```"""


STALE_IMAGE_NOTE = ("code changed since this image was captured; recapture or explicitly "
                    "reassess before treating this as current")
STALE_IMAGE_UNVERIFIED = ("the code may have changed since this image was captured (it could not "
                          "be verified); recapture or explicitly reassess before treating this "
                          "as current")


def visual_identity_mismatch(u: dict, env: dict, sha256: str = "") -> str:
    """'' when a visual observer's answer names the image and source it was
    given (v0.121.7: distinct artifact_id / source_snapshot_id). A value that
    is present and wrong is a mismatch; an absent one is not checked.

    Legacy answers (and stored rows) carry only `base`: base == the artifact id
    (or the snapshot id the old general check used) is a match."""
    aid, sid = u.get("artifact_id") or "", u.get("snapshot_id") or ""
    got_a, got_s = env.get("artifact_id") or "", env.get("source_snapshot_id") or ""
    out = []
    if got_a and aid:
        sha = (sha256 or "").lower()
        if got_a != aid and not (sha and len(got_a) >= 12 and sha.startswith(got_a.lower())):
            out.append(f"result names artifact {got_a}, assignment was {aid}")
    if got_s and sid and got_s != sid:
        out.append(f"result names source snapshot {got_s}, assignment was {sid}")
    if not (got_a or got_s) and env.get("base") and (aid or sid) and env["base"] not in (aid, sid):
        out.append(f"result names base {env['base']}, assignment was artifact {aid or 'n/a'} "
                   f"/ snapshot {sid or 'n/a'}")
    return "; ".join(out)


class VisualMixin:
    """Mixed into proactive.Proactive."""

    # ------------------------------------------------------------------ identity

    def stale_image(self, P, u: dict, current: Optional[dict] = None) -> Optional[dict]:
        """None when the code still matches the source snapshot the image was
        bound to at plan time; otherwise why it doesn't. Unverifiable counts
        as stale: an older frame is never presented as matching newer code."""
        try:
            pk = json.loads(u.get("packet") or "{}")
        except (TypeError, ValueError):
            pk = {}
        sid = u.get("snapshot_id") or ""
        snap = self.artifacts.snapshot(sid, P.id, u["task_id"]) if sid else None
        try:
            if current is None:
                t = self.store.task(P.id, u["task_id"])
                current = self._snap(P, t, P.adapter.describe(u["task_id"])) if t else {}
        except Exception as e:                                    # noqa: BLE001
            current = {"status": f"error: {type(e).__name__}"}
        out = {"captured_at_snapshot": sid or None,
               "captured_fp": (snap or {}).get("content_fp"),
               "current_fp": (current or {}).get("content_fp"),
               "note": STALE_IMAGE_NOTE}
        unverified = {**out, "note": STALE_IMAGE_UNVERIFIED}
        if pk.get("capture_predates_snapshot"):
            return {**out, "reason": pk["capture_predates_snapshot"]}
        if not snap:
            return {**unverified, "reason": "no source snapshot was bound to this image; "
                                            "whether the code changed can't be verified"}
        if (current or {}).get("status") != "consistent":
            return {**unverified, "reason": "the current code could not be fingerprinted ("
                                            f"{(current or {}).get('status') or 'unknown'})"}
        same = (snap["content_fp"] == current.get("content_fp")) if snap.get("content_fp") \
            else snap["snapshot_id"] == current.get("snapshot_id")       # legacy row
        return None if same else {**out, "reason": "the source changed after the image's snapshot"}

    _pool: Optional[ThreadPoolExecutor] = None

    def _direct_init(self):
        if getattr(self, "_futures", None) is None:
            self._futures = {}
            self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="lane-direct")

    # ------------------------------------------------------------------ planning

    def _plan_artifacts(self, P, t, spec, ls):
        if not L.lane_on(ls, "visual"):
            return
        v = ls["lanes"]["visual"]
        now = self.clock()
        evs = [e for e in self.events(P.id, t["task_id"])
               if (e["kind"] == "capture" and e["status"] == "artifact")
               or (e["kind"] == "artifact" and e["status"] in ("new", "artifact"))]
        for ev in evs:
            aid = ev["detail"].get("artifact_id")
            a = self.artifacts.get(aid) if aid else None
            if not a:
                self._set_event(ev["event_key"], "ignored")
                continue
            if a["valid"] == "invalid":
                self._set_event(ev["event_key"], "invalid_capture")
                self.store.event(P.id, t["task_id"], "visual_not_reviewed", detail={
                    "artifact": aid, "reason": "the capture procedure marked the frame invalid "
                                               f"({(a.get('validity') or {}).get('reason') or ''})"
                                               "; not evidence, no model call"})
                continue
            if a["status"] != "current":
                self._set_event(ev["event_key"], "superseded")
                continue
            if now - (a["created_ts"] or now) < float(v["debounce_seconds"]):
                continue                                  # coalesce a burst of recaptures
            refs, ref_notes = self._references(P, t, v)
            # v0.121.7: the image is bound to THIS source snapshot; every later
            # staleness/drift check (observer and proposer) compares against it
            snap = self._snap(P, t, spec)
            predates = ""
            if a.get("revision") and snap.get("head") and a["revision"] != snap["head"]:
                predates = (f"the image was captured at revision {a['revision'][:12]}, before "
                            f"the source snapshot at {snap['head'][:12]}")
            uid, how = self.add(P, t, "visual", aid, {
                "artifact": aid, "sha256": a["sha256"], "path": a["rel_path"],
                "generation": a["generation"], "width": a["width"], "height": a["height"],
                "revision": a["revision"], "subject": a["subject"], "viewport": v.get("viewport")
                or f"{a['width']}x{a['height']} capture", "validity": a.get("validity") or {},
                "capture_source": a["source"], "references": refs, "reference_notes": ref_notes,
                "snapshot": snap.get("snapshot_id"), "snapshot_fp": snap.get("content_fp"),
                "snapshot_status": snap["status"], "capture_predates_snapshot": predates,
                "previous": a.get("prev_artifact")},
                ls, files=[a["rel_path"]], depth=1, artifact_id=aid,
                snapshot_id=snap.get("snapshot_id") or "", event_key=ev["event_key"])
            self._done_event(ev, uid, how)

    def _references(self, P, t, v) -> tuple:
        out, notes = [], []
        import glob as _g
        for pat in list(v.get("reference_globs") or []):
            pat = pat.replace("{task}", t["task_id"])
            hits = sorted(_g.glob(os.path.join(P.profile.workspace, pat))) if not os.path.isabs(pat) \
                else [pat]
            for absp in hits[:3]:
                r = self.artifacts.record(project_id=P.id, task_id=t["task_id"], run_id=t["run_id"],
                                          session_id="", workspace=P.profile.workspace, path=absp,
                                          allowed=list(v.get("reference_globs") or []),
                                          kind="reference", source="reference",
                                          record={"valid": "ok"},
                                          max_bytes=int(v["max_image_bytes"]))
                if r["status"] in ("recorded", "duplicate"):
                    out.append(r["artifact"]["artifact_id"])
                else:
                    notes.append(f"reference {r.get('path') or absp}: {r['status']} — {r['reason']}")
        return out[:2], notes

    # ------------------------------------------------------------------ admission

    def admission_problem(self, P, u: dict, adm: dict) -> str:
        if u.get("mode") != "direct":
            return ""
        # a pinned fallback (first route not admitted) is the only route that can
        # serve this session, so only it must see images; otherwise every route
        routes = admitted_routes(adm)
        if not routes:
            return "Foundry reported no routes for the visual persona: image modality unverifiable"
        allowed = set((L.settings(P)["lanes"]["visual"].get("approved_models") or []))
        for r in routes:
            name = f"{r.get('backend') or '?'}/{r.get('model') or '?'}"
            if r.get("vision") is None:
                return (f"route {name}: Foundry did not report image modality (needs Foundry "
                        f">= 0.118.0); a text-only fallback can't be ruled out, so no image is sent")
            if r.get("vision") is False:
                return (f"route {name} has no image input; the visual persona must contain only "
                        f"vision-capable routes")
            if allowed and r.get("model") not in allowed:
                return f"route {name} is not in [lanes.visual] approved_models"
        return ""

    # ------------------------------------------------------------------ the call

    def _skill_texts(self, P, ls, names) -> tuple:
        loaded, missing, texts = [], [], []
        for n in names:
            hit = None
            for d in ls["skill_dirs"]:
                p = os.path.join(P.profile.workspace, d, n, "SKILL.md")
                if os.path.isfile(p) and P.profile.in_workspace(os.path.relpath(p, P.profile.workspace)):
                    hit = p
                    break
            if not hit:
                missing.append(n)
                continue
            with open(hit, "rb") as f:
                b = f.read(40000)
            loaded.append({"name": n, "path": os.path.relpath(hit, P.profile.workspace)
                           .replace("\\", "/"), "sha256": hashlib.sha256(b).hexdigest()[:16]})
            texts.append(f"### Skill {n}\n" + b.decode("utf-8", "replace"))
        return loaded, missing, texts

    def build_visual_messages(self, P, spec, u: dict, pk: dict, ls: dict) -> tuple:
        a = self.artifacts.get(pk["artifact"])
        img = self.artifacts.read_bytes(a)
        parts = []
        sent = [{"artifact": a["artifact_id"], "sha256": a["sha256"], "bytes": len(img),
                 "role": "capture"}]
        loaded, missing, texts = self._skill_texts(P, ls, ls["lanes"]["visual"].get("skills") or [])
        text = (f"Assignment {u['unit_id']} — task {spec.task_id} ({spec.title}).\n"
                f"Identity (copy exactly into your answer): artifact_id {a['artifact_id']}, "
                f"source_snapshot_id {u.get('snapshot_id') or pk.get('snapshot') or 'none'}.\n"
                f"Capture:{a['rel_path']} generation {a['generation']}, sha256 {a['sha256']}, "
                f"{a['width']}x{a['height']}, captured at source revision {a['revision'][:12]} "
                f"(capture record: {a['source']}).\nSubject: {pk.get('subject')}. Viewport/device: "
                f"{pk.get('viewport')}.\nCapture validity lines:\n"
                + "\n".join((pk.get("validity") or {}).get("lines") or ["(none reported)"])
                + "\n\n## Acceptance / design criteria\n" + spec.spec[:5000]
                + ("\n\n## Reference notes\n" + "\n".join(pk.get("reference_notes"))
                   if pk.get("reference_notes") else "")
                + (f"\n\nSkills NOT available to you: {', '.join(missing)}" if missing else ""))
        parts.append({"type": "text", "text": text})
        parts.append({"type": "text", "text": "Image 1: the capture under review."})
        parts.append({"type": "image_url", "image_url": {
            "url": f"data:{a['mime']};base64," + base64.b64encode(img).decode()}})
        for rid in pk.get("references") or []:
            r = self.artifacts.get(rid)
            if not r:
                continue
            rb = self.artifacts.read_bytes(r)
            parts.append({"type": "text", "text": f"Approved design reference: {r['rel_path']} "
                                                  f"(sha256 {r['sha256'][:16]})."})
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{r['mime']};base64," + base64.b64encode(rb).decode()}})
            sent.append({"artifact": rid, "sha256": r["sha256"], "bytes": len(rb),
                         "role": "reference"})
        system = OBSERVER_SYSTEM + ("\n\n## Project skills (loaded by the coordinator)\n"
                                    + "\n\n".join(texts) if texts else "")
        return ([{"role": "system", "content": system}, {"role": "user", "content": parts}],
                sent, loaded, missing)

    def start_direct(self, P, t, spec, u, h, backend, persona, adm) -> bool:
        self._direct_init()
        pid, tid, uid = P.id, t["task_id"], u["unit_id"]
        key = f"helper:{pid}:{tid}:{uid}:{int(u.get('requests') or 1)}"
        prior = self.store.op_begin(key, pid, tid, "helper")
        res = dict((prior or {}).get("result") or {})
        if res.get("calling"):
            # restarted while the call was in flight: the call may have used
            # quota and its answer is lost; never repeated automatically
            self.mgr._finish(P, {**u, "session_id": res.get("session_id") or ""}, "failed",
                             "the coordinator restarted during the vision call; result unknown, "
                             "not repeated", None)
            return False
        ls = L.settings(P)
        sid = res.get("session_id") or f"lane-{uid}-{int(u.get('requests') or 1)}"
        if not res.get("session_id"):
            res = {"session_id": sid}
            self.store.op_update(key, "started", res)
            self.store.add_session(sid, pid, tid, t["run_id"], "helper", "helper", t["attempt"],
                                   "foundry-direct", persona, {"parent": t["session_id"],
                                                               "unit": uid, "purpose": u["kind"],
                                                               "lane": u["lane"]})
        if not res.get("bound"):
            try:
                P.foundry.bind(sid, pid, tid, t["run_id"], "helper", persona,
                               parent=t["session_id"] or "", purpose=u["kind"],
                               meta=pinned_meta(adm) or None, worker_client="", assignment={
                                   "unit": uid, "lane": u["lane"], "kind": u["kind"],
                                   "artifact": u.get("artifact_id"), "depth": u.get("depth") or 0})
            except FoundryError as e:
                self.store.update_session(sid, status="ended", outcome="unbound",
                                          ended_ts=self.clock())
                self.store.x("UPDATE helper_units SET status='deferred', reason=?, next_check=?, "
                             "updated_ts=? WHERE unit_id=?",
                             (f"binding failed: {e}"[:500], self.clock() + float(h["poll_seconds"]),
                              self.clock(), uid))
                self.store.x("DELETE FROM ops WHERE key=?", (key,))
                return False
            res["bound"] = True
            self.store.op_update(key, "bound", res)
        pk = json.loads(u.get("packet") or "{}")
        try:
            messages, sent, loaded, missing = self.build_visual_messages(P, spec, u, pk, ls)
        except (OSError, ValueError) as e:
            self.mgr._finish(P, {**u, "session_id": sid}, "failed",
                             f"snapshot unavailable: {e}"[:300], None)
            return False
        served_by = admitted_routes(adm)          # only the pinned route when pinned
        vision_routes = [f"{r.get('backend')}/{r.get('model')}" for r in served_by
                         if r.get("vision")]
        pk.update(sent=sent, skills_loaded=loaded, skills_missing=missing,
                  vision_routes=vision_routes, vision_models=[r.get("model") for r in
                                                              served_by if r.get("vision")],
                  **({"pinned_route": pinned_meta(adm)["pinned_route"]}
                     if pinned_meta(adm) else {}))
        res["calling"] = True
        self.store.op_update(key, "calling", res)
        now = self.clock()
        self.store.x("UPDATE helper_units SET status='running', persona=?, backend=?, session_id=?, "
                     "parent_session=?, attempts=attempts+1, reason=?, next_eligible=NULL, "
                     "updated_ts=?, started_ts=?, packet=? WHERE unit_id=?",
                     (persona, backend, sid, t["session_id"], f"vision call admitted ("
                      f"{', '.join(vision_routes)})", now, now, json.dumps(pk, default=str)[:60000],
                      uid))
        max_tokens = int(ls["lanes"]["visual"].get("max_tokens") or 3000)
        self._futures[uid] = (self._pool.submit(P.foundry.chat, persona, messages,
                                                session_id=sid, max_tokens=max_tokens), key)
        self.store.event(pid, tid, "helper_started", session_id=sid, detail={
            "unit": uid, "kind": u["kind"], "lane": u["lane"], "persona": persona,
            "backend": backend, "parent": t["session_id"], "images": sent})
        return True

    def wait_direct(self, timeout: float = 10.0) -> None:
        """Tests: let in-flight direct calls finish."""
        for fut, _ in list((getattr(self, "_futures", None) or {}).values()):
            try:
                fut.result(timeout=timeout)
            except Exception:                                    # noqa: BLE001
                pass

    def forget_direct(self, unit_id: str) -> None:
        f = (getattr(self, "_futures", None) or {}).pop(unit_id, None)
        if f:
            f[0].cancel()                          # only works before it started
            self.store.op_update(f[1], "done", {"cancelled": True})

    def poll_direct(self, P, u: dict, h: dict) -> None:
        self._direct_init()
        uid = u["unit_id"]
        entry = self._futures.get(uid)
        if entry is None:
            return self.mgr._finish(P, u, "failed", "the coordinator restarted while the vision "
                                                    "call was in flight; result unknown, not "
                                                    "repeated", None)
        fut, key = entry
        if not fut.done():
            if self.clock() - (u.get("started_ts") or self.clock()) > \
                    float(h["timeout_minutes"]) * 60:
                self.forget_direct(uid)
                self.mgr._finish(P, u, "failed", f"vision call timed out after "
                                                 f"{h['timeout_minutes']} min (a late reply is "
                                                 f"discarded)", None)
            return
        self._futures.pop(uid, None)
        self.store.op_update(key, "done", {"session_id": u["session_id"]})
        try:
            text, meta = fut.result()
        except FoundryDenied as e:
            # the call-time gate refused (e.g. a fallback route over the cutoff):
            # nothing was generated; the unit waits like any deferral
            self.store.x("UPDATE helper_units SET status='deferred', reason=?, next_eligible=?, "
                         "next_check=?, updated_ts=?, requests=COALESCE(requests,1)+1 WHERE "
                         "unit_id=?",
                         (f"Foundry refused at call time ({e.category}): {e.message}"[:900],
                          self.clock() + e.retry_after if e.retry_after else None,
                          self.clock() + max(float(h["poll_seconds"]), float(e.retry_after or 0)),
                          self.clock(), uid))
            self.store.update_session(u["session_id"], status="ended", outcome="denied",
                                      ended_ts=self.clock())
            self.store.event(P.id, u["task_id"], "helper_deferred", detail={
                "unit": uid, "reason": f"call-time refusal: {e.category}"})
            return
        except Exception as e:                                   # noqa: BLE001
            return self.mgr._finish(P, u, "failed", f"vision call failed: {type(e).__name__}: "
                                                    f"{e}"[:300], None)
        pk = json.loads(u.get("packet") or "{}")
        env = L.parse_envelope(text or "")
        served = (meta or {}).get("served_model") or ""
        reject = ""
        if not served:
            reject = "Foundry did not report the served model: image inspection unconfirmed"
        elif served not in (pk.get("vision_models") or []):
            reject = (f"served by {served}, which is not one of the vision-validated routes "
                      f"({', '.join(pk.get('vision_routes') or [])}): no image inspection")
        elif env.get("image_seen") is not True:
            reject = "the model did not confirm it saw the image: no image inspection"
        if reject:
            env = {**env, "findings": [], "not_inspected": True}
        elif (env.get("validity") and env["validity"] != "ok") or env.get("subject_present") is False:
            # the model says the frame is invalid/occluded or the subject is missing:
            # that is the result; defects "seen" in such a frame are not delivered
            why = env.get("validity") if env.get("validity") not in (None, "", "ok") \
                else "subject missing"
            env = {**env, "findings": [], "frame_problem": why,
                   "limitations": ((env.get("limitations") or "") + f" Frame not usable: {why}; "
                                   "recapture before judging the UI.").strip()}
        usage = (meta or {}).get("usage") or {}
        self.mgr._finish(P, u, "done", reject or "observation returned", text,
                         tokens={"input": usage.get("prompt_tokens"),
                                 "output": usage.get("completion_tokens")},
                         meta={"envelope": env, "served_model": served,
                               "skills_observed": [s["name"] for s in pk.get("skills_loaded") or []],
                               "reject": reject})

    # ------------------------------------------------------------------ cascade

    def _after_visual(self, P, u: dict, env: dict) -> None:
        """observer -> proposer for confident, in-scope findings only."""
        ls = L.settings(P)
        v = ls["lanes"]["visual"]
        if int(u.get("depth") or 0) >= 2 or not v.get("propose") or not L.lane_on(ls, "proposer"):
            return
        if env.get("validity") and env["validity"] != "ok" or env.get("subject_present") is False:
            return
        floor = L.SEVERITY.get(v["min_severity"], 2)
        good = [f for f in env.get("findings") or []
                if L.SEVERITY.get(f["severity"], 0) >= floor and f["confidence"] >= float(
                    v["min_confidence"]) and f["in_scope"] and (f.get("hypothesis") or f.get("paths"))]
        if not good:
            return
        t = self.store.task(P.id, u["task_id"])
        if not t or t["state"] != "IMPLEMENTING" or t["run_id"] != u["run_id"]:
            return
        spec = P.adapter.describe(u["task_id"])
        from .profile import matches_any
        paths = sorted({p.split(":")[0] for f in good for p in f.get("paths") or []
                        if matches_any(p.split(":")[0], spec.scope)})
        # v0.121.7: the proposal targets the source the IMAGE was bound to (the
        # observer's plan-time snapshot), never a fresh one taken now: drift
        # and staleness are then measured from what the image actually shows
        snap = self.artifacts.snapshot(u.get("snapshot_id") or "", P.id, u["task_id"]) or {}
        a = self.artifacts.get(u["artifact_id"]) or {}
        pk = json.loads(u.get("packet") or "{}")
        stale = env.get("stale_image")
        evidence = (f"Capture {a.get('rel_path')} gen {a.get('generation')} sha256 {a.get('sha256')} "
                    f"({a.get('width')}x{a.get('height')}) at revision {str(a.get('revision'))[:12]}"
                    f", bound to source snapshot {snap.get('snapshot_id') or 'none'}; "
                    f"observer {u['unit_id']} ({u.get('persona')}, served {env.get('served_model') or ''})"
                    f": {env.get('summary') or ''}. References: {pk.get('references') or 'none'}."
                    + (f" STALE IMAGE: {STALE_IMAGE_NOTE} ({stale.get('reason')})." if stale else ""))
        manifest = sorted((snap.get("manifest") or {}))
        uid, how = self.add(P, t, "proposer", f"{u['unit_id']}", {
            "findings": good, "evidence": evidence, "ui": True, "snapshot": snap.get("snapshot_id"),
            "snapshot_status": "consistent" if snap else "missing", "files": manifest,
            "excerpt_files": [p for p in paths if p in manifest][:4] or manifest[:2],
            "capture_predates_snapshot": pk.get("capture_predates_snapshot") or "",
            "stale_image": stale,
            "notes": (f"The image predates the current code ({stale.get('reason')}): the findings "
                      f"describe the snapshot above, not necessarily the live workspace. Say in "
                      f"limitations whether each finding still applies to the current source."
                      if stale else "")},
            ls, files=paths or spec.scope, parent_unit=u["unit_id"], depth=2,
            artifact_id=u.get("artifact_id") or "", snapshot_id=snap.get("snapshot_id") or "")
        if uid and how == "created":
            floor_d = L.SEVERITY.get(ls["min_delivery_severity"], 2)
            rest = [f for f in env.get("findings") or [] if f not in good
                    and L.SEVERITY.get(f["severity"], 0) >= floor_d]
            if not rest:
                cur = json.loads(self.store.q("SELECT envelope FROM helper_units WHERE unit_id=?",
                                              (u["unit_id"],))[0]["envelope"] or "{}")
                cur["routed_to"] = uid
                self.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?",
                             (json.dumps(cur, default=str)[:80000], u["unit_id"]))
