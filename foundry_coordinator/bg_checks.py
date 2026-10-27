"""Background check runs (0.147.0).

A sprint's verify and closeout checks (done_check, the Lune suite, project test
suites) can take many minutes. They used to run inside the tick, so one sprint's
checks froze the whole coordinator: no session started, no decision applied, no
Lead case moved (GF-NAME1 sat READY while GF-GATE1 verified, 2026-10-27).

Each run is a job keyed (project, task, kind, head) on a worker thread. A tick
asks for its result: a job finished within `wait_s` returns at once (quick checks
and tests behave as before); otherwise the tick moves on and asks again next
time. At most `cap` jobs run at once (Lune is memory-heavy); a job for the same
task and kind at another head is dropped (its thread finishes, the result is
ignored). Results are applied by the caller on the tick thread only.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, Optional

POOL_MAX = 8


class BackgroundChecks:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._pool: Optional[ThreadPoolExecutor] = None
        self._jobs: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=POOL_MAX,
                                            thread_name_prefix="coordinator-checks")
        return self._pool

    def running(self) -> list[dict]:
        """[{key, label, seconds}] of the jobs not finished yet."""
        now = time.time()
        with self._lock:
            return [{"key": k, "label": f"{k[1]} {k[2]}", "seconds": int(now - j["wall"])}
                    for k, j in self._jobs.items() if not j["future"].done()]

    def run(self, key: tuple, fn: Callable[[], list], wait_s: float = 2.0,
            cap: int = 2) -> tuple[str, object]:
        """("done", results | exception) once the job for `key` finished (the job is
        then forgotten), ("running", seconds) while it runs, ("queued", n_running)
        when `cap` jobs already run."""
        with self._lock:
            for k in [k for k in self._jobs if k[:3] == key[:3] and k != key]:
                self._jobs.pop(k)                   # the same task's run at an older head
            job = self._jobs.get(key)
            if job is None:
                busy = sum(1 for j in self._jobs.values() if not j["future"].done())
                if busy >= max(1, int(cap)):
                    return "queued", busy
                from . import check_progress
                check_progress.reset(key[0], key[1], kind=key[2] if len(key) > 2 else "",
                                     head=key[3] if len(key) > 3 else "")
                job = {"future": self._executor().submit(fn), "wall": time.time()}
                self._jobs[key] = job
        try:
            res = job["future"].result(timeout=max(0.0, float(wait_s)))
        except FutureTimeout:
            return "running", int(time.time() - job["wall"])
        except Exception as e:                                    # noqa: BLE001
            res = e
        with self._lock:
            if self._jobs.get(key) is job:
                self._jobs.pop(key)
        return "done", res

    def drop(self, keep: Callable[[tuple], bool]) -> list[tuple]:
        """Forget every job whose key `keep` rejects (its task left the state)."""
        with self._lock:
            gone = [k for k in self._jobs if not keep(k)]
            for k in gone:
                self._jobs.pop(k)
        return gone

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
