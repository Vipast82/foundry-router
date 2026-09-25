"""One place that turns a finished model call into performance telemetry.

Every dispatch path — direct (Cline), direct-streaming, raw passthrough, and
the agent's worker/tool-loop calls — used to repeat the same three-to-five
bookkeeping calls by hand, and they drifted: the agent path never wrote a
perf-history sample or counted truncations, the streaming passthrough logged
its backend as the literal "stream", and prefill counts diverged. Now each path
builds (or reconstructs) a ChatResult and calls record_call(); what the Live
view and the Performance tab see is identical no matter who dispatched.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import perf_history

log = logging.getLogger("foundry.telemetry")


def record_call(db: Any, registry: Any, *, model: str, backend: str, result: Any,
                persona: str = "", mode: str = "", ttft_ms: Optional[float] = None,
                wall_ms: Optional[float] = None,
                max_tokens: Optional[int] = None) -> None:
    """Fold one completed call into the registry's rolling stats (Live view),
    the finish-reason / truncation counter, and a perf-history sample
    (Performance tab). A truncated reply (finish_reason=length) also raises a
    warning in the Events log. Best-effort: telemetry never breaks a request."""
    r = result
    try:
        registry.note_inference(
            model, r.completion_tokens, r.eval_duration_ns, r.load_duration_ns,
            prompt_count=r.prompt_tokens,
            prompt_eval_duration_ns=r.prompt_eval_duration_ns,
            draft_n=r.draft_n, draft_n_accepted=r.draft_n_accepted,
            cached_tokens=r.cached_tokens,
            prefill_count=getattr(r, "prefill_tokens", 0) or 0)
        if ttft_ms:
            registry.note_ttft(model, ttft_ms)
        registry.note_finish(model, r.finish_reason)
    except Exception:
        log.exception("registry telemetry failed for %s", model)
    if db is None:
        return
    if r.finish_reason == "length":
        cap = f", max_tokens={max_tokens}" if max_tokens else ""
        try:
            db.log_event(
                "warning", "facade",
                f"{model} reply TRUNCATED at max_tokens ({r.completion_tokens} "
                f"tokens{cap}) — the client will need to continue; raise "
                f"worker_max_tokens or fix looping (sampling)")
        except Exception:
            pass
    perf_history.record_sample(
        db, model=model, backend=backend or "", persona=persona or "",
        mode=mode or "", prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens, cached_tokens=r.cached_tokens,
        draft_n=r.draft_n, draft_n_accepted=r.draft_n_accepted,
        eval_duration_ns=r.eval_duration_ns,
        prompt_eval_duration_ns=r.prompt_eval_duration_ns,
        ttft_ms=ttft_ms, wall_ms=wall_ms, finish_reason=r.finish_reason,
        prefill_tokens=getattr(r, "prefill_tokens", 0) or 0,
        reasoning_tokens=getattr(r, "reasoning_tokens", 0) or 0,
        load_duration_ns=r.load_duration_ns,
        timing_source=getattr(r, "timing_source", "") or "")
