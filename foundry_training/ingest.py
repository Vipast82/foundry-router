"""Ingestion runner shared by every adapter: redact, keep the raw record,
normalize, store events, advance the checkpoint, record gaps.

Idempotent: event ids are deterministic and inserts ignore duplicates, so a
re-run or a resumed run never doubles anything. Recoverable: every run is
logged; a run left 'running' (process killed) is reported and simply run
again; records are committed one at a time with the checkpoint, so nothing
processed before the interruption is lost.
"""

from __future__ import annotations

import traceback
from typing import Callable, Iterable

from .redact import Redactor
from .store import TrainingStore


class Ingestor:
    def __init__(self, store: TrainingStore, known_secrets=(), allowed_email_domains=()):
        self.store = store
        self.known = list(known_secrets)
        self.allowed = list(allowed_email_domains)

    def redactor(self, extra_domains=()) -> Redactor:
        return Redactor(self.known, list(self.allowed) + list(extra_domains))

    def run(self, source_key: str, records: Iterable[tuple[str, str, dict, dict]],
            normalize: Callable, cursor_of: Callable = None, domains=(),
            stop_after: int = 0) -> dict:
        """records yields (record_id, project_id, payload, provenance).
        normalize(payload, project_id, raw_ref, redaction) -> list[Event]."""
        st = self.store
        run_id = st.run_start(source_key)
        counts = {"records": 0, "new_events": 0, "existing_events": 0, "redactions": 0,
                  "skipped": 0}
        try:
            for rid, project_id, payload, prov in records:
                if not project_id:
                    counts["skipped"] += 1
                    continue
                red, f = self.redactor(domains).obj(payload)
                redaction = {"status": f.status(), "kinds": f.kinds(),
                             "source_sanitization": prov.get("sanitization", "none")}
                counts["redactions"] += f.count
                raw_ref = st.put_raw(prov.get("adapter", source_key.split(":")[0]), rid,
                                     project_id, red, redaction, prov)
                events = normalize(red, project_id, raw_ref, redaction)
                new, old = st.put_events(events)
                counts["new_events"] += new
                counts["existing_events"] += old
                counts["records"] += 1
                if cursor_of:
                    st.set_checkpoint(source_key, str(cursor_of(rid, payload)))
                if stop_after and counts["records"] >= stop_after:
                    raise KeyboardInterrupt("stop_after (test hook)")
            st.run_finish(run_id, "done", counts)
        except KeyboardInterrupt:
            st.run_finish(run_id, "interrupted", counts, "interrupted")
            raise
        except Exception as e:                                    # noqa: BLE001
            st.run_finish(run_id, "error", counts, f"{e}\n{traceback.format_exc()[-800:]}")
            raise
        return counts
