"""Asset judge (v0.167.0): an independent, blind vision check of a 3D model sprint.

Plan: reports/ASSET_PIPELINE_REVIEW_2026-11.md gap 4 (approved 2026-11-01). It follows what
the four-ways test found works: a fresh judge that writes its OWN checklist from the design
before it sees the model, then scores the model's images shown under shuffled letters.

When it runs
  [approval] asset_judge = true, and an approval rule with rubric = "asset" whose `match` tag
  fits the task, whose `scope_any` globs name a file of the plan's scope (a model sprint lists
  its .blend / .fbx), and for which at least one reference image is found. A rule that finds
  no reference does not apply (the task's other rules decide, as before). An asset rule that
  applies replaces the plain screenshot ("visual") rules for that task.

Inputs (all data in the rule)
  references  globs with {asset} (e.g. assets/source/*/{asset}/ref/*_views_v*.png, then the
              concept sheet); per asset the FIRST glob with a hit wins; at most MAX_REFS images
  notes       text globs with {asset} (e.g. the asset's checklist.md); capped
  asset_from  regexes with one group, run over the plan scope, giving the asset ids
  artifacts   the model's images (renders, Studio captures); at most max_frames are shown

Two model calls, each a fresh session on the visual_reviewer persona (vision route checked)
  1. checklist: the reference images + notes + sprint criteria -> the judge's own checklist.
     Keyed by the references' bytes, so a rework pass reuses it (paid once).
  2. score: the checklist + the references + the model images under shuffled letters (no file
     names; the order and letters are a hash of the revision, so a retry sees the same
     mapping) -> per item pass / fail / unclear and per image a verdict. The coordinator maps
     the letters back to files and computes the verdict itself.

Fail-closed and paid once like visual_approval.py: bound to the revision and the image bytes,
an in-flight call is never repeated, an inconclusive answer gets one structured retry and is
then accepted with notes (W1.3), CHANGES go back for at most [fast_lane] max_vision_rounds
fix rounds. Victor's batch review is unchanged.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re

from .adapters.foundry import FoundryDenied, FoundryError
from .profile import matches_any

DEFAULT_MAX_FRAMES = 12
MAX_REFS = 4
MAX_NOTE_CHARS = 4000
MAX_NOTES_TOTAL = 12000
MAX_IMAGE_BYTES = 8_000_000
VIEW_ORDER = ("front", "back", "left", "right", "side", "q34", "three", "top", "bottom", "close")
POLICY = "asset_judge v1"
_SYN = {"approved": "approve", "approval": "approve", "changes": "changes_required",
        "change_required": "changes_required", "request_changes": "changes_required",
        "changes_requested": "changes_required", "unsure": "uncertain"}
_OK_VALIDITY = {"ok", "valid", "good", "pass", "passed", "yes", "true"}
HOUSE_AXES = ("The house convention: the asset faces +Y toward the viewer, Z is up, so in a "
              "FRONT view the model's +X side is on screen-left. Name sides as +X / -X, never "
              "'his left' or 'my left'.")


def enabled(P) -> bool:
    return (P.profile.raw.get("approval") or {}).get("asset_judge") is True


def is_asset(rule) -> bool:
    return str(getattr(rule, "rubric", "") or "").lower() == "asset"


# ------------------------------------------------------------------------- which rule applies

def assets_of(rule, spec) -> list[str]:
    out = []
    for rx in getattr(rule, "asset_from", None) or []:
        try:
            cre = re.compile(rx)
        except re.error:
            continue
        for p in getattr(spec, "scope", None) or []:
            m = cre.search(str(p).replace("\\", "/"))
            if m and m.group(1) and "*" not in m.group(1) and m.group(1) not in out:
                out.append(m.group(1))
    return out


def _glob(P, pat: str) -> list[str]:
    ws = P.profile.workspace
    out = []
    for path in sorted(glob.glob(os.path.join(ws, pat), recursive=True)):
        rel = os.path.relpath(path, ws).replace("\\", "/")
        if os.path.isfile(path) and P.profile.in_workspace(rel):
            out.append(rel)
    return out


def references(P, rule, assets: list[str]) -> list[str]:
    """Per asset, the files of the first `references` glob that finds any (newest version
    last in name order, so the last one is kept when a glob finds several versions)."""
    out = []
    for a in assets:
        for pat in getattr(rule, "references", None) or []:
            hits = _glob(P, pat.replace("{asset}", a))
            if hits:
                out.append(hits[-1])
                break
    return out[:MAX_REFS]


def applies(P, spec, rule) -> tuple[bool, list[str], list[str], str]:
    """(applies, assets, reference paths, why not)."""
    if not is_asset(rule):
        return False, [], [], "not an asset rule"
    if not enabled(P):
        return False, [], [], "[approval] asset_judge is off"
    scope = [str(s).replace("\\", "/") for s in getattr(spec, "scope", None) or []]
    want = getattr(rule, "scope_any", None) or []
    if want and not any(matches_any(s, want) for s in scope):
        return False, [], [], "the plan lists no model file (scope_any)"
    assets = assets_of(rule, spec)
    if not assets:
        return False, [], [], "no asset id found in the plan scope (asset_from)"
    refs = references(P, rule, assets)
    if not refs:
        return False, assets, [], f"no reference image for {', '.join(assets)}"
    return True, assets, refs, ""


def _tag_match(rule, spec) -> bool:
    return any(matches_any(tag, [rule.match]) for tag in getattr(spec, "tags", None) or [])


def select_rules(P, spec, rules: list) -> list:
    """The rules that apply to a task, from those whose tag already matches: asset rules
    only when the judge applies (then they replace the plain visual rules), else the rest."""
    asset = [r for r in rules if is_asset(r) and applies(P, spec, r)[0]]
    if asset:
        return asset + [r for r in rules if not is_asset(r) and r.kind != "visual"]
    return [r for r in rules if not is_asset(r)]


def rule_for(P, spec, kind: str, stage: str = ""):
    """The one rule of `kind` that binds this task's approval (what the engine, the control
    center and the obligations hash): the applicable asset rule, else the first other rule
    of that kind (the pre-v0.167 behaviour)."""
    rules = [r for r in P.profile.approvals if r.kind == kind and (not stage or r.stage == stage)]
    if spec is not None:
        chosen = select_rules(P, spec, [r for r in rules if _tag_match(r, spec)])
        if chosen:
            return chosen[0]
    plain = [r for r in rules if not is_asset(r)]
    return plain[0] if plain else None


# ------------------------------------------------------------------------- inputs

def _image(P, rel: str):
    path = os.path.join(P.profile.workspace, rel)
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        return None
    mime = "image/png" if blob.startswith(b"\x89PNG\r\n\x1a\n") else \
        "image/jpeg" if blob.startswith(b"\xff\xd8\xff") else ""
    return (mime, blob) if mime else None


def _view_rank(rel: str) -> tuple:
    name = os.path.basename(rel).lower()
    for i, v in enumerate(VIEW_ORDER):
        if v in name:
            return (i, rel)
    return (len(VIEW_ORDER), rel)


def pick_frames(P, arts: dict, max_frames: int) -> tuple[list, list]:
    """(frames [(rel, mime, blob)], left out [rel]): readable images in view order."""
    frames, left = [], []
    for rel in sorted(arts, key=_view_rank):
        img = _image(P, rel)
        if img is None:
            continue
        if len(frames) >= max_frames:
            left.append(rel)
            continue
        frames.append((rel, img[0], img[1]))
    return frames, left


def notes_text(P, rule, assets: list[str]) -> str:
    out, total = [], 0
    for a in assets:
        for pat in getattr(rule, "notes", None) or []:
            for rel in _glob(P, pat.replace("{asset}", a)):
                try:
                    with open(os.path.join(P.profile.workspace, rel), encoding="utf-8",
                              errors="replace") as fh:
                        txt = fh.read(MAX_NOTE_CHARS)
                except OSError:
                    continue
                if total + len(txt) > MAX_NOTES_TOTAL:
                    return "\n\n".join(out)
                out.append(f"--- {rel} ---\n{txt}")
                total += len(txt)
    return "\n\n".join(out)


def letters(fingerprint: str, rels: list[str]) -> dict:
    """letter -> rel, shuffled by a hash of the review fingerprint (stable for a retry)."""
    order = sorted(rels, key=lambda r: hashlib.sha256((fingerprint + r).encode()).hexdigest())
    out = {}
    for i, rel in enumerate(order):
        n, s = i, ""
        while True:
            s = chr(ord("A") + n % 26) + s
            n = n // 26 - 1
            if n < 0:
                break
        out[s] = rel
    return out


# ------------------------------------------------------------------------- answers

def _answer(text: str) -> dict:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.I)
    if fenced:
        raw = fenced.group(1)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        m = re.search(r"\{[\s\S]*\}", raw)
        try:
            result = json.loads(m.group(0)) if m else {}
        except (TypeError, ValueError):
            return {}
    return result if isinstance(result, dict) else {}


def valid_checklist(answer: dict) -> tuple[list, str]:
    items = answer.get("checklist")
    if not isinstance(items, list) or not 3 <= len(items) <= 40:
        return [], "the checklist must have 3 to 40 items"
    out, seen = [], set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return [], f"checklist item {i + 1} is not an object"
        iid = str(it.get("id") or f"C{i + 1}").strip()[:12]
        check = str(it.get("check") or "").strip()
        if not check or iid in seen:
            return [], f"checklist item {iid} has no check or a duplicate id"
        seen.add(iid)
        out.append({"id": iid, "part": str(it.get("part") or "")[:80], "check": check[:300],
                    "side": str(it.get("side") or "")[:12], "kind": str(it.get("kind") or "")[:20]})
    return out, ""


def valid_score(answer: dict, lettered: dict, arts: dict, checklist: list) -> dict:
    """The coordinator's own verdict from a score answer: verdict, findings (with file
    paths), score (passed/total), unclear count, clean frames."""
    bad = lambda why: {"verdict": "uncertain", "findings": [why], "score": None,  # noqa: E731
                       "unclear": 0, "clean": []}
    said = str(answer.get("verdict") or "").strip().lower().replace("-", "_").replace(" ", "_")
    said = _SYN.get(said, said)
    if said not in ("approve", "changes_required", "uncertain"):
        return bad("the judge returned no valid verdict")
    rows = answer.get("frames")
    if not isinstance(rows, list) or len(rows) != len(lettered):
        return bad("the judge did not account for every model image")
    by = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("letter"), str):
            return bad("an image verdict has no letter")
        L = row["letter"].strip().upper()
        if L in by:
            return bad("duplicate image verdict")
        by[L] = row
    if set(by) != set(lettered):
        return bad("the image verdicts name the wrong letters")
    findings, clean = [], []
    for L, rel in lettered.items():
        row = by[L]
        if row.get("image_seen") is not True:
            return bad(f"image {L} was not verified as seen")
        if not str(row.get("observation") or "").strip():
            return bad(f"image {L} has no observation")
        fl = row.get("findings")
        if not isinstance(fl, list) or not all(isinstance(f, str) and f.strip() for f in fl):
            return bad(f"image {L} has no structured findings list")
        v = row.get("validity")
        ok = v is True or (isinstance(v, str) and v.strip().lower() in _OK_VALIDITY)
        if row.get("subject_present") is not True or not ok:
            findings.append(f"{rel}: the model is not clearly shown (invalid, occluded or "
                            f"missing): {str(row.get('observation'))[:200]}")
        for f in fl:
            findings.append(f"{rel}: {f[:300]}")
        if not fl and row.get("subject_present") is True and ok:
            clean.append(rel)
    items = answer.get("items")
    if not isinstance(items, list):
        return bad("the judge returned no checklist scores")
    want = {c["id"]: c for c in checklist}
    got = {}
    for it in items:
        if not isinstance(it, dict):
            return bad("a checklist score is not an object")
        iid = str(it.get("id") or "").strip()
        st = str(it.get("status") or "").strip().lower()
        if iid not in want or iid in got or st not in ("pass", "fail", "unclear"):
            return bad(f"checklist score {iid or '?'} is unknown, duplicate or has no status")
        got[iid] = it
    if set(got) != set(want):
        return bad("the judge did not score every checklist item")
    passed = sum(1 for it in got.values() if it["status"].lower() == "pass")
    unclear = sum(1 for it in got.values() if it["status"].lower() == "unclear")
    for iid, it in got.items():
        if it["status"].lower() != "fail":
            continue
        shown = [lettered[L.strip().upper()] for L in (it.get("letters") or [])
                 if isinstance(L, str) and L.strip().upper() in lettered]
        c = want[iid]
        where = f" (see {', '.join(shown)})" if shown else ""
        findings.append(f"checklist {iid} {c['part'] or ''}: {c['check'][:160]} -> "
                        f"{str(it.get('note') or 'fails')[:240]}{where}")
    score = f"{passed}/{len(want)}"
    if findings:
        verdict = "changes_required"
    elif unclear > max(2, len(want) // 2):
        verdict = "uncertain"
        findings = [f"{unclear} of {len(want)} checklist items could not be judged from the images"]
    else:
        verdict = "approve"
    if said == "changes_required" and verdict == "approve" and not findings:
        verdict, findings = "uncertain", ["changes required without a visible finding"]
    return {"verdict": verdict, "findings": findings, "score": score, "unclear": unclear,
            "clean": clean}


# ------------------------------------------------------------------------- the step

def _intro_refs(refs) -> list:
    parts = []
    for i, (rel, mime, blob) in enumerate(refs):
        parts.append({"type": "text", "text": f"Reference R{i + 1} (approved design): {rel}"})
        parts.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"}})
    return parts


def _route_ok(P, persona: str):
    try:
        info = P.foundry.persona_info(persona)
    except FoundryError as e:
        return None, f"asset judge route unavailable: {e}"
    routes = (info or {}).get("routes") or []
    if not (info and info.get("exists", True) and info.get("enabled", True)
            and info.get("strict") is True and info.get("client_allowed") is not False
            and routes and all(r.get("vision") is True and r.get("model") for r in routes)):
        return None, "asset judge route is not verified vision-capable for every possible model"
    return routes, ""


def _call(c, P, t, key: str, sid: str, persona: str, routes, parts: list, revision: str,
          purpose: str):
    """One paid call under op `key`: (raw, served) or (None, None) when deferred/failed."""
    pid, tid = P.id, t["task_id"]
    if not c.store.session(sid):
        c.store.add_session(sid, pid, tid, t["run_id"], "visual_reviewer", purpose,
                            t["attempt"], "foundry-direct", persona,
                            {"parent": t.get("session_id"), "revision": revision})
    try:
        P.foundry.bind(sid, pid, tid, t["run_id"], "visual_reviewer", persona,
                       parent=t.get("session_id") or "", purpose="review")
        c.store.op_update(key, "calling", {"session_id": sid})
        raw, meta = P.foundry.chat(persona, [
            {"role": "system", "content": "You are a read-only, independent 3D asset judge. "
             "Judge only the attached images. Never waive gates or invent evidence. Answer "
             "with one JSON object only."},
            {"role": "user", "content": parts}], session_id=sid, max_tokens=6000)
    except FoundryDenied as e:
        c.store.op_update(key, "deferred", {"retry_at": c.clock() + max(60, int(e.retry_after or 300)),
                                            "reason": str(e)[:300]})
        c.store.update_task(pid, tid, next_action=f"asset judge deferred: {e}"[:500])
        return None, None
    except FoundryError as e:
        c.store.op_update(key, "deferred", {"retry_at": c.clock() + 300, "reason": str(e)[:300]})
        c.store.update_task(pid, tid, next_action=f"asset judge unavailable: {e}"[:500])
        return None, None
    served = str((meta or {}).get("served_model") or "")
    c.store.update_session(sid, status="ended", outcome="answered", ended_ts=c.clock())
    if served not in {r["model"] for r in routes}:
        return raw, ""
    return raw, served


def _op(c, key: str, pid: str, tid: str, kind: str):
    """(prior record or None, proceed): proceed False = wait (in flight / deferred)."""
    prior = c.store.op_begin(key, pid, tid, kind)
    if not prior:
        return None, True
    if prior["status"] == "done":
        return prior, False
    if prior["status"] == "deferred" and c.clock() >= float(
            (prior.get("result") or {}).get("retry_at") or 0):
        return None, True
    return prior, False


def step(c, P, t: dict, spec, rule, revision: str) -> None:
    """One fail-closed asset-judge attempt (called from visual_approval.step)."""
    from . import visual_approval as VA
    pid, tid = P.id, t["task_id"]
    ok, assets, ref_rels, why = applies(P, spec, rule)
    if not ok:
        c.store.update_task(pid, tid, next_action=f"asset judge: {why}"[:500])
        return
    dirty = {path for _, path in P.ws.status()}
    if P.ws.head() != revision and not VA._frames_unchanged_since(P, spec, rule, revision):
        c.store.update_task(pid, tid, next_action="asset judge waits for the exact clean "
                            "closeout revision")
        return
    arts = P.adapter.approval_artifacts(spec, rule.artifacts)
    if any(path in dirty for path in arts):
        c.store.update_task(pid, tid, next_action="asset judge waits for committed images")
        return
    max_frames = int(getattr(rule, "max_frames", 0) or DEFAULT_MAX_FRAMES)
    frames, left = pick_frames(P, arts, max_frames)
    if not frames:
        return VA._missing_frames(c, P, t, spec, rule, revision,
                                  "no readable model image (render or capture) matches "
                                  f"{list(rule.artifacts)}")
    mem = VA.accepted_hashes(c.store, pid, tid, t)
    if all(arts[rel] in mem for rel in arts):
        c.store.add_approval(pid, tid, "visual", revision, arts, "coordinator:vision-memory",
                             f"every model image was judged or accepted at these bytes earlier "
                             f"({len(arts)} image(s)); no model call; {POLICY}")
        c.store.event(pid, tid, "visual_approval_from_memory", detail={
            "revision": revision, "frames": len(arts), "rubric": "asset"})
        return
    refs = [(r, *img) for r in ref_rels for img in [_image(P, r)] if img]
    if not refs:
        c.store.update_task(pid, tid, next_action="asset judge: the reference images are not "
                            "readable PNG/JPEG files")
        return
    persona = P.profile.persona("visual_reviewer", tid)
    routes, why = _route_ok(P, persona)
    if routes is None:
        c.store.update_task(pid, tid, next_action=why[:500])
        return
    notes = notes_text(P, rule, assets)
    criteria = str(getattr(spec, "spec", "") or "")[:6000]
    ref_fp = hashlib.sha256(json.dumps(
        [[r, hashlib.sha256(b).hexdigest()] for r, _, b in refs] +
        [hashlib.sha256(notes.encode()).hexdigest()], sort_keys=True).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([revision, arts, ref_fp], sort_keys=True)
                                 .encode()).hexdigest()
    data0 = dict((VA._task(c.store, pid, tid, t) or {}).get("data") or {})
    tries = int((data0.get("visual_uncertain") or {}).get(fingerprint[:16]) or 0)
    sfx = f":retry{tries}" if tries else ""
    retry_note = ("This is the one retry of an answer that could not be verified: answer with "
                  "exactly the JSON shape asked for. " if tries else "")

    # -- 1. the judge's own checklist, from the design only -----------------------------
    base_ckey = f"asset-checklist:{pid}:{tid}:{ref_fp}"
    getter = getattr(c.store, "op", None)
    base = getter(base_ckey) if callable(getter) else None
    checklist = None
    if base and base.get("status") == "done" and (base.get("result") or {}).get("checklist"):
        # a valid checklist for these references is reused by every later pass and retry
        prior, go, checklist = base, False, (base.get("result") or {})["checklist"]
        ckey = base_ckey
    else:
        ckey = base_ckey + sfx
        prior, go = _op(c, ckey, pid, tid, "asset_checklist")
    if checklist is not None:
        pass
    elif prior and prior["status"] == "done":
        checklist = (prior.get("result") or {}).get("checklist") or None
        if not checklist:
            return VA._uncertain(c, P, t, revision, arts, f"model:{persona}", fingerprint,
                                 tries, [f"no valid checklist: "
                                         f"{(prior.get('result') or {}).get('error', '')}"])
    elif not go:
        if prior and prior["status"] == "calling":
            c.store.update_task(pid, tid, next_action="asset judge checklist call outcome "
                                "uncertain after restart; paid call not repeated")
        return
    if checklist is None:
        parts = [{"type": "text", "text": retry_note + (
            f"Project {pid}, task {tid}: a 3D model of {', '.join(assets)} is being built. You "
            "have NOT seen the model. From the approved reference images below (and the notes), "
            "write YOUR OWN checklist of what the finished model must show to match this "
            "design: every visible part, its position, its side, colours, proportions and "
            "small details that are easy to get wrong. " + HOUSE_AXES + " Use 6 to 30 items. "
            'Return JSON {"checklist": [{"id": "C1", "part": "...", "check": "...", '
            '"side": "+X|-X|center|n/a", "kind": "presence|position|side|colour|proportion|'
            'detail"}], "limitations": ["..."]}.' +
            (f"\n\nNotes for this asset:\n{notes}" if notes else "") +
            (f"\n\nSprint criteria:\n{criteria}" if criteria else ""))}]
        parts += _intro_refs(refs)
        raw, served = _call(c, P, t, ckey, f"asset-checklist-{ref_fp[:20]}{sfx.replace(':', '-')}",
                            persona, routes, parts, revision, "asset_checklist")
        if raw is None:
            return
        items, err = valid_checklist(_answer(raw))
        if not served:
            items, err = [], "the serving model was not verified as vision-capable"
        c.store.op_update(ckey, "done", {"checklist": items, "error": err,
                                         "served_model": served,
                                         "response_sha256": hashlib.sha256(
                                             str(raw).encode()).hexdigest()})
        c.store.event(pid, tid, "asset_judge_checklist", detail={
            "assets": assets, "references": [r for r, _, _ in refs], "items": len(items),
            "error": err, "served_model": served})
        if not items:
            return VA._uncertain(c, P, t, revision, arts, f"model:{persona}/{served}",
                                 fingerprint, tries, [f"no valid checklist: {err}"])
        checklist = items

    # -- 2. score the model images, blind -----------------------------------------------
    skey = f"asset-judge:{pid}:{tid}:{fingerprint}{sfx}"
    prior, go = _op(c, skey, pid, tid, "asset_judge")
    if prior and prior["status"] == "done":
        res = prior.get("result") or {}
        actor = f"model:{persona}/{res.get('served_model') or 'unknown'}"
        if res.get("verdict") == "approve" and not c._valid_approval(P, t, spec, rule, revision):
            c.store.add_approval(pid, tid, "visual", revision, arts, actor,
                                 f"recovered asset judge {res.get('session_id')}; score "
                                 f"{res.get('score')}; {POLICY}")
        elif res.get("verdict") == "changes_required":
            VA._changes(c, P, t, revision, arts, actor, list(res.get("findings") or []))
        else:
            VA._uncertain(c, P, t, revision, arts, actor, fingerprint, tries,
                          list(res.get("findings") or []))
        return
    if not go:
        if prior and prior["status"] == "calling":
            c.store.update_task(pid, tid, next_action="asset judge call outcome uncertain "
                                "after restart; paid call not repeated")
        return
    lettered = letters(fingerprint, [rel for rel, _, _ in frames])
    blob_of = {rel: (mime, blob) for rel, mime, blob in frames}
    parts = [{"type": "text", "text": retry_note + (
        f"Project {pid}, task {tid}. Below: YOUR checklist for {', '.join(assets)} (you wrote "
        "it from the design before seeing the model), the approved reference images R1.., "
        f"then {len(lettered)} images of the finished model labelled with shuffled letters. "
        "The letters and their order say nothing about the view or which version is newest; "
        "file names are hidden on purpose. " + HOUSE_AXES + " Score EVERY checklist item: "
        "pass, fail or unclear, naming the letters that show it. For every image give: letter, "
        "image_seen (true only if you see it), subject_present (the model is clearly shown), "
        'validity (exactly "ok", "invalid", "occluded" or "missing_subject"), observation (one '
        "line), findings (defects that break the design; empty when none). Approve only if no "
        'item fails and every image is valid. Return JSON {"verdict": "approve|changes_required'
        '|uncertain", "frames": [{"letter", "image_seen", "subject_present", "validity", '
        '"observation", "findings"}], "items": [{"id", "status", "letters", "note"}], '
        '"limitations": []}.\n\nChecklist:\n' +
        "\n".join(f"{x['id']} [{x['kind']}] {x['part']} {('(' + x['side'] + ')') if x['side'] else ''}"
                  f": {x['check']}" for x in checklist))}]
    parts += _intro_refs(refs)
    for L, rel in lettered.items():
        mime, blob = blob_of[rel]
        parts.append({"type": "text", "text": f"Model image {L}"})
        parts.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"}})
    sid = f"asset-judge-{fingerprint[:20]}{sfx.replace(':', '-')}"
    raw, served = _call(c, P, t, skey, sid, persona, routes, parts, revision, "asset_judge")
    if raw is None:
        return
    res = valid_score(_answer(raw), lettered, arts, checklist)
    if not served:
        res = {"verdict": "uncertain", "score": None, "unclear": 0, "clean": [],
               "findings": ["actual serving model was not verified as vision-capable"]}
    c.store.op_update(skey, "done", {"session_id": sid, "verdict": res["verdict"],
                                     "score": res["score"], "served_model": served,
                                     "findings": res["findings"][:12],
                                     "response_sha256": hashlib.sha256(
                                         str(raw).encode()).hexdigest()})
    c.store.event(pid, tid, "asset_judge_review", session_id=sid, detail={
        "verdict": res["verdict"], "score": res["score"], "unclear": res["unclear"],
        "persona": persona, "served_model": served, "revision": revision, "assets": assets,
        "references": [r for r, _, _ in refs], "letters": lettered, "left_out": left[:20],
        "artifacts": arts, "findings": res["findings"][:12]})
    VA.remember_clean(c.store, pid, tid, [arts[r] for r in res["clean"]]
                      if res["verdict"] == "approve" else [])
    if ((P.ws.head() != revision and not VA._frames_unchanged_since(P, spec, rule, revision)) or
            P.adapter.approval_artifacts(spec, rule.artifacts) != arts or
            any(path in {p for _, p in P.ws.status()} for path in arts)):
        c.store.update_task(pid, tid, next_action="asset judge review became stale; the "
                            "current revision is judged next")
        return
    actor = f"model:{persona}/{served}"
    extra = f"; {len(left)} image(s) over max_frames not shown" if left else ""
    if res["verdict"] == "approve":
        c.store.add_approval(pid, tid, "visual", revision, arts, actor,
                             f"asset judge {sid}; score {res['score']}{extra}; {POLICY}")
    elif res["verdict"] == "changes_required":
        VA._changes(c, P, t, revision, arts, actor, res["findings"])
    else:
        VA._uncertain(c, P, t, revision, arts, actor, fingerprint, tries, res["findings"])
