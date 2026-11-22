"""Two required findings that cannot both be met are asked about once
(COORDINATOR_AUTOMATION_NEEDS item 25).

POL-7: the visual review asked for fixes that needed files of completed sprints; the code
review then said the plan forbids editing completed sprints' files. The implementer put
the files back, the visual review failed again, and the sprint went round until its budget
was gone. Neither reviewer could see the other's finding.

  record   when the visual review sends a sprint back, its revision and findings are kept
           on the task (`visual_fix`).
  notice   when a code review then asks for changes, each of its findings that FORBIDS
           touching a file ("must not edit", "out of scope", "belongs to a completed
           sprint", ...) and names a file that was changed since that visual round is a
           clash: the visual fix needed the file, the review forbids it.
  ask      the sprint stops once with both findings side by side (blocker class
           `findings_clash`), instead of passing the review's findings to the implementer.
           With the Lead on that is a Lead case; otherwise it waits for the owner. The
           answer is the unblock's resolution, which rides into the next session.
  once     the same files clashing again are not asked about again: the review's findings
           go to the implementer as before, with the earlier answer still in the trail.

It only ever stops a sprint that was about to be sent back anyway, and only on a finding
that names the file; a review that merely mentions a file changes nothing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

FORBIDS = re.compile(
    r"must not (?:be )?(?:edit|modif|touch|chang)|may not (?:be )?(?:edit|modif|touch|chang)|"
    r"not allowed to (?:edit|modify|touch|change)|should not (?:be )?(?:edit|modif|touch|chang)|"
    r"forbid|prohibit|out[- ]of[- ]scope|outside (?:the |its |this sprint's )?scope|"
    r"not in (?:the |its )?(?:scope|file list)|(?:completed|finished|closed|another|other) "
    r"sprint'?s?'? (?:file|work|code)|belongs? to (?:a |an )?(?:completed|finished|other|another)|"
    r"revert (?:the |these |this )?(?:change|edit)s? to", re.I)
MAX_FILES = 8


def record_visual(c, P, t: dict, revision: str, findings: list, round_no: int) -> None:
    """The visual review sent the sprint back at `revision`."""
    try:
        c.store.merge_data(P.id, t["task_id"], visual_fix={
            "revision": revision, "round": int(round_no), "ts": c.clock(),
            "findings": [str(f)[:300] for f in (findings or [])][:12]})
    except Exception:                                            # noqa: BLE001
        pass


def _names(text: str, path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return p in text.replace("\\", "/") or (len(base) > 5 and base in text)


def find(review: dict, changed: list) -> list:
    """[(file, finding text)]: review findings that forbid a file in `changed`."""
    res = review.get("result") or {}
    items = []
    for f in res.get("findings") or []:
        if isinstance(f, dict):
            items.append((str(f.get("file") or ""), str(f.get("message") or "")))
        else:
            items.append(("", str(f)))
    items += [("", str(x)) for x in res.get("required_repairs") or []]
    out, seen = [], set()
    for ffile, msg in items:
        if not FORBIDS.search(msg):
            continue
        for path in changed:
            p = str(path).replace("\\", "/")
            if p in seen:
                continue
            if ffile.replace("\\", "/") == p or _names(msg, p):
                seen.add(p)
                out.append((p, msg[:400]))
    return out


def check(c, P, t: dict, review: dict) -> Optional[str]:
    """The blocker text when this CHANGES_REQUIRED review clashes with the last visual round
    and that has not been asked yet; else None. Never raises."""
    try:
        data = (c.store.task(P.id, t["task_id"]) or t).get("data") or {}
        vf = data.get("visual_fix") or {}
        base, head = str(vf.get("revision") or ""), str(review.get("reviewed_sha") or "")
        if not base or not head or base == head:
            return None
        try:
            changed = list(P.ws.changed_files(base, head))
        except Exception:                                        # noqa: BLE001
            return None
        hits = find(review, changed)
        if not hits:
            return None
        key = hashlib.sha256("\n".join(sorted(p for p, _ in hits)).encode()).hexdigest()[:16]
        asked = list(data.get("clash_asked") or [])
        if key in asked:
            return None
        c.store.merge_data(P.id, t["task_id"], clash_asked=(asked + [key])[-20:])
        files = [p for p, _ in hits][:MAX_FILES]
        vis = "; ".join(str(x) for x in (vf.get("findings") or [])[:3]) or "(not recorded)"
        text = (f"two required findings clash. Visual review round {vf.get('round') or '?'} "
                f"asked for: {vis[:500]}. The fix changed {', '.join(files)}. The code review "
                f"now says: \"{hits[0][1][:400]}\""
                + (f" (and {len(hits) - 1} more of the same kind)" if len(hits) > 1 else "")
                + ". Both cannot be met. Unblock with which one stands: the visual fix keeps "
                  "those files changed (the reviewer is told why), or the files go back and "
                  "the visual finding is accepted as it is.")
        c.store.event(P.id, t["task_id"], "findings_clash", detail={
            "files": files, "visual_round": vf.get("round"), "visual_revision": base[:12],
            "reviewed": head[:12], "review_finding": hits[0][1][:300], "digest": True,
            "text": f"{t['task_id']}: the code review forbids {', '.join(files[:3])}, which "
                    f"the visual fix needed; asked once"})
        return text
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"{P.id}/{t['task_id']}: clash check failed: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
        return None
