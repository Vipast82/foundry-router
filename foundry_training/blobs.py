"""Store each message once (v0.181.145).

A captured model call carries the whole conversation up to that point, so turn
50 of a session repeats turns 1-49. Measured on a live store (2026-11-15): 99.6 %
of a 103 GB normalized database was `model_request` / `model_call` events of
about 340 KB each, and the raw database held the same text again.

`BlobStore` keeps every message, tool list and large string ONCE, keyed by the
SHA-256 of its JSON, zlib-compressed, in `<root>/blobs.sqlite`. A record that
used to hold the text holds `{"$blob": "<sha>"}` in its place:

    pack(obj)    every item of a "messages" list, a whole "tools" list, and any
                 string of BIG_STRING characters or more -> a reference
    unpack(obj)  the references -> the original values

A long conversation's list of references repeats too (turn N+1 is turn N plus
one), so every full run of SPAN consecutive items of a "messages" list is itself
stored once and replaced by `{"$span": "<sha>"}`: a 300-message call costs about
ten references, not three hundred.

`unpack(pack(x)) == x` for anything JSON can hold, and `unpack` leaves a record
that was never packed as it is, so old and new rows live side by side. The raw
store and the normalized store share one blob table: the text they both hold is
kept once.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from collections import OrderedDict
from typing import Any

KEY = "$blob"
SPAN_KEY = "$span"
SPAN = 32                # consecutive "messages" items stored as one run
MIN_BYTES = 200          # smaller messages stay in place (a reference is 80 bytes)
BIG_STRING = 4096        # any string this long is stored once, wherever it sits
SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
  sha TEXT PRIMARY KEY, z BLOB NOT NULL, n INTEGER NOT NULL) WITHOUT ROWID;
"""


class MissingBlob(RuntimeError):
    pass


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def is_ref(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and isinstance(v.get(KEY), str)


def is_span(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and isinstance(v.get(SPAN_KEY), str)


def refs(obj: Any, out: set | None = None) -> set:
    """Every blob a (packed) record points at directly (spans not opened)."""
    out = set() if out is None else out
    if is_ref(obj):
        out.add(obj[KEY])
    elif is_span(obj):
        out.add(obj[SPAN_KEY])
    elif isinstance(obj, dict):
        for v in obj.values():
            refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            refs(v, out)
    return out


class BlobStore:
    def __init__(self, db, cache_bytes: int = 256 * 1024 * 1024):
        self.db = db                         # store._DB (q / x / many)
        self._cache: "OrderedDict[str, Any]" = OrderedDict()
        self._cache_n = 0
        self._cache_max = cache_bytes
        self._known: set = set()             # shas already in the table (this process)
        self.stats = {"packed": 0, "new": 0, "bytes_in": 0, "bytes_new": 0}

    # -- writing ------------------------------------------------------------------

    def _ref(self, value: Any, pending: dict, text: str | None = None) -> dict:
        data = (_dumps(value) if text is None else text).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        self.stats["packed"] += 1
        self.stats["bytes_in"] += len(data)
        if sha not in self._known and sha not in pending:
            pending[sha] = data
        return {KEY: sha}

    def _walk(self, obj: Any, pending: dict, key: str = "") -> Any:
        if isinstance(obj, dict):
            if is_ref(obj):
                return obj                                   # already packed
            return {k: self._walk(v, pending, k) for k, v in obj.items()}
        if isinstance(obj, list):
            if key == "tools" and obj:
                text = _dumps(obj)
                if len(text) >= MIN_BYTES:
                    return self._ref(obj, pending, text)     # the tool list, whole
            if key == "messages":
                out = []
                for m in obj:
                    if isinstance(m, dict) and not is_ref(m):
                        text = _dumps(m)
                        if len(text) >= MIN_BYTES:
                            out.append(self._ref(m, pending, text))
                            continue
                    out.append(self._walk(m, pending))
                if len(out) >= 2 * SPAN:
                    full = len(out) - len(out) % SPAN
                    runs = []
                    for i in range(0, full, SPAN):
                        text = _dumps(out[i:i + SPAN])
                        runs.append({SPAN_KEY: self._ref(None, pending, text)[KEY]})
                    out = runs + out[full:]
                return out
            return [self._walk(v, pending) for v in obj]
        if isinstance(obj, str) and len(obj) >= BIG_STRING:
            return self._ref(obj, pending)
        return obj

    def pack(self, obj: Any) -> Any:
        """The record with its repeated parts replaced by references; the parts
        are written to the blob table (insert-or-ignore) before this returns."""
        pending: dict = {}
        out = self._walk(obj, pending)
        self.flush(pending)
        return out

    def flush(self, pending: dict) -> None:
        if not pending:
            return
        rows = [(sha, zlib.compress(data, 6), len(data)) for sha, data in pending.items()]
        new = self.db.many("INSERT OR IGNORE INTO blobs (sha, z, n) VALUES (?,?,?)", rows)
        self.stats["new"] += new
        self.stats["bytes_new"] += sum(len(d) for d in pending.values()) if new else 0
        self._known.update(pending)
        if len(self._known) > 2_000_000:
            self._known.clear()

    # -- reading ------------------------------------------------------------------

    def _load(self, shas: set) -> None:
        need = [s for s in shas if s not in self._cache]
        for i in range(0, len(need), 400):
            chunk = need[i:i + 400]
            rows = self.db.q(f"SELECT sha, z, n FROM blobs WHERE sha IN ({','.join('?' * len(chunk))})",
                             chunk)
            for r in rows:
                self._cache[r["sha"]] = json.loads(zlib.decompress(r["z"]).decode("utf-8"))
                self._cache_n += int(r["n"])
            got = {r["sha"] for r in rows}
            lost = [s for s in chunk if s not in got]
            if lost:
                raise MissingBlob(f"{len(lost)} blob(s) referenced but not stored, e.g. {lost[0]}")
        if self._cache_n > self._cache_max:
            # oldest first, but never something this call was asked for (found live:
            # a large record's own messages were evicted while it was being read)
            for sha in [k for k in self._cache if k not in shas]:
                if self._cache_n <= self._cache_max:
                    break
                self._cache_n -= len(_dumps(self._cache.pop(sha)))

    def _fill(self, obj: Any) -> Any:
        if is_ref(obj):
            v = self._cache[obj[KEY]]
            self._cache.move_to_end(obj[KEY])
            # a stored value never contains a reference (pack does not nest), but a
            # copy keeps the cache safe from a caller that edits what it was given
            return json.loads(_dumps(v)) if isinstance(v, (dict, list)) else v
        if isinstance(obj, dict):
            return {k: self._fill(v) for k, v in obj.items()}
        if isinstance(obj, list):
            out = []
            for v in obj:
                if is_span(v):
                    out.extend(self._fill(x) for x in self._cache[v[SPAN_KEY]])
                else:
                    out.append(self._fill(v))
            return out
        return obj

    def unpack(self, obj: Any) -> Any:
        need = refs(obj)
        if not need:
            return obj
        self._load(need)
        inner: set = set()                   # what the runs point at
        for sha in need:
            v = self._cache.get(sha)
            if isinstance(v, list):
                refs(v, inner)
        if inner:
            self._load(inner | need)
        return self._fill(obj)

    def keys(self, items: Any) -> list:
        """One identity per item of a (packed or whole) "messages" list, without
        reading the messages themselves: a reference is its own identity, a run is
        opened into its items, anything else is hashed. Two calls of one
        conversation share a prefix of these (sessions.py)."""
        if not isinstance(items, list):
            return []
        spans = {v[SPAN_KEY] for v in items if is_span(v)}
        if spans:
            self._load(spans)
        out: list = []

        def one(v):
            if is_ref(v):
                out.append(v[KEY])
            else:
                out.append(hashlib.sha256(_dumps(v).encode("utf-8")).hexdigest())

        for v in items:
            if is_span(v):
                for x in self._cache[v[SPAN_KEY]]:
                    one(x)
            else:
                one(v)
        return out

    def count(self) -> dict:
        r = self.db.q("SELECT COUNT(*) AS n, COALESCE(SUM(n), 0) AS raw, "
                      "COALESCE(SUM(LENGTH(z)), 0) AS stored FROM blobs")[0]
        return {"blobs": r["n"], "text_bytes": r["raw"], "stored_bytes": r["stored"]}
