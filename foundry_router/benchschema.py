"""Output a program will read (v0.181.242).

Scheduled jobs, the Document MCP and the jobs list all take structured input,
and what fills it in is a model reading somebody's notes. A case here is a
JSON Schema and notes in plain prose; the reply is the JSON object, handed
over either as the whole reply or as the arguments of one tool call.

Every case is generated with its key, so it is scored exactly: the object must
be valid against the schema and every value must be the one the notes give.
What the notes make hard is what makes it hard in use - a time written in
words, a length of time to turn into minutes, a coded value, days to put in
week order, a default stated once, a correction, an instruction at the end
that applies to every record, and optional fields the notes never mention.
The dial is the width and depth of the schema: more fields, more records,
records inside records.
"""
from __future__ import annotations

import json
import random
import re

TOOL_NAME = "file_record"
TEXT_SYSTEM = ("You turn written notes into the JSON a program reads. The program does no "
               "guessing: it takes exactly what you give it.")
TEXT_RULE = ("Reply with the JSON object and nothing else - no explanation and no code fence. "
             "It must be valid against the schema. Leave out anything optional that the notes "
             "do not state.")
TOOL_RULE = (f"Call `{TOOL_NAME}` once, with everything the notes state. Leave out anything "
             "optional that the notes do not state.")

# --------------------------------------------------------------------------- #
# A small JSON Schema validator: the keywords these cases use                 #
# --------------------------------------------------------------------------- #


def _is(v, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "null":
        return v is None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return t == "number" or float(v).is_integer()        # "integer"


def same(a, b) -> bool:
    """Two JSON values a program would read as the same: 90 and 90.0 are, true
    and 1 are not, "P1" and "p1" are not."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def schema_errors(v, s: dict, path: str = "", out=None, limit: int = 25) -> list:
    """[(path, keyword)] for every way `v` breaks schema `s`, first found
    first. Keywords: type, enum, pattern, range, size, unique, required,
    additional."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    t = s.get("type")
    if t and not _is(v, t):
        out.append((path or "$", "type"))
        return out
    if "enum" in s and not any(same(v, e) for e in s["enum"]):
        out.append((path or "$", "enum"))
    if isinstance(v, str) and s.get("pattern") and not re.search(s["pattern"], v):
        out.append((path or "$", "pattern"))
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if ("minimum" in s and v < s["minimum"]) or ("maximum" in s and v > s["maximum"]):
            out.append((path or "$", "range"))
    if isinstance(v, list):
        if len(v) < s.get("minItems", 0) or len(v) > s.get("maxItems", len(v)):
            out.append((path or "$", "size"))
        if s.get("uniqueItems") and any(same(x, y) for i, x in enumerate(v) for y in v[:i]):
            out.append((path or "$", "unique"))
        if isinstance(s.get("items"), dict):
            for i, x in enumerate(v):
                schema_errors(x, s["items"], f"{path}[{i}]", out, limit)
    if isinstance(v, dict):
        props = s.get("properties") or {}
        for k in s.get("required") or []:
            if k not in v:
                out.append((f"{path}.{k}" if path else k, "required"))
        for k, x in v.items():
            where = f"{path}.{k}" if path else k
            if k in props:
                schema_errors(x, props[k], where, out, limit)
            elif s.get("additionalProperties") is False:
                out.append((where, "additional"))
    return out


def _at(v, path: str):
    """The value at a path like jobs[3].time, or None when it is not there."""
    for part in re.findall(r"\[\d+\]|[^.\[\]]+", path):
        try:
            v = v[int(part[1:-1])] if part.startswith("[") else v[part]
        except (KeyError, IndexError, TypeError):
            return None
    return v


def flatten(v, path: str = "", out=None) -> dict:
    """{path: value} for every leaf; an empty list or object is a leaf too."""
    out = {} if out is None else out
    if isinstance(v, dict) and v:
        for k, x in v.items():
            flatten(x, f"{path}.{k}" if path else str(k), out)
    elif isinstance(v, list) and v:
        for i, x in enumerate(v):
            flatten(x, f"{path}[{i}]", out)
    else:
        out[path or "$"] = v
    return out


# --------------------------------------------------------------------------- #
# Reading the reply                                                           #
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```$", re.S)


def read_json(text: str) -> tuple:
    """(object, how) from a reply: how is "bare" when the whole reply is the
    JSON, "fenced" when a code fence is all there is around it, "prose" when
    it had to be dug out of other text, "" when there is none."""
    text = str(text or "").strip()
    if not text:
        return None, ""
    try:
        v = json.loads(text)
        return (v, "bare") if isinstance(v, dict) else (None, "")
    except ValueError:
        pass
    m = _FENCE.match(text)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict):
                return v, "fenced"
        except ValueError:
            pass
    # the last whole object in the reply: a model that writes one, has second
    # thoughts in prose and writes another meant the second
    # ... unless the braces do not close: that is an object cut off, and the
    # whole records inside it are not what was asked for
    if text.count("{") != text.count("}"):
        return None, ""
    dec, found, at = json.JSONDecoder(), None, text.find("{")
    while at >= 0:
        try:
            v, end = dec.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        if isinstance(v, dict):
            found = v
        at = text.find("{", end)
    return (found, "prose") if found is not None else (None, "")


WHY_WORDS = {
    "not_json": "no JSON object in the reply",
    "cut_off": "the JSON was cut off before it ended",
    "fenced": "right, but inside a code fence a program would trip on",
    "prose": "right, but with prose around the JSON",
    "not_called": "right, but written out instead of calling the tool",
    "called_more_than_once": "called the tool more than once",
    "schema:type": "a value of the wrong type",
    "schema:enum": "a value outside the allowed list",
    "schema:pattern": "a value in the wrong format",
    "schema:range": "a number outside its range",
    "schema:size": "a list of the wrong size",
    "schema:unique": "the same value twice in a list",
    "schema:required": "left out a required field",
    "schema:additional": "added a field the schema does not have",
    "items": "the wrong number of records",
    "invented": "filled in an optional field the notes never mention",
    "wrong:time": "wrong value: a time written in words",
    "wrong:duration": "wrong value: a length of time to turn into minutes",
    "wrong:code": "wrong value: a coded value",
    "wrong:days": "wrong value: days of the week, in week order",
    "wrong:money": "wrong value: an amount of money",
    "wrong:date": "wrong value: a date written out",
    "wrong:switch": "wrong value: on or off",
    "wrong:count": "wrong value: a count written in words",
    "wrong:default": "wrong value: one that came from the stated default",
    "wrong:correction": "wrong value: one the notes corrected",
    "wrong:late": "wrong value: an instruction at the end for every record",
    "wrong:copy": "wrong value: a name or text to copy exactly",
}


def schema_score(got: dict, ref: dict) -> dict:
    """{score, accepted, right, total, extra, why, detail} for one reply.

    100: handed over the way it was asked for (the whole reply, or one tool
    call), valid against the schema, every value right and nothing added.
    50: every value right and valid, but a program reading it would trip - a
    code fence, prose around it, written out when a call was asked for.
    0: anything else. The detail keeps how many values were right and the
    first thing that went wrong."""
    mode, schema, key = ref.get("mode") or "text", ref.get("schema") or {}, ref.get("key")
    kinds = ref.get("kinds") or {}
    text = str((got or {}).get("content") or "")
    calls = [c for c in (got or {}).get("tool_calls") or []
             if isinstance(c, dict) and c.get("name") == TOOL_NAME]
    how = ""
    if mode == "tool" and calls:
        obj = calls[-1].get("arguments")
        obj = obj if isinstance(obj, dict) else None
        how = "bare" if len(calls) == 1 else "called_more_than_once"
    else:
        obj, how = read_json(text)
        if mode == "tool" and how:
            how = "not_called"
            # a call written out as text, name and all: what it meant to send
            if obj.get("name") == TOOL_NAME and isinstance(obj.get("arguments"), dict):
                obj = obj["arguments"]
    want = flatten(key)
    total = len(want)
    if obj is None:
        cut = text.count("{") > text.count("}") and "{" in text
        why = ["cut_off" if cut else "not_json"]
        return {"score": 0.0, "accepted": 0.0, "right": 0, "total": total, "extra": 0,
                "why": why, "detail": f"accepted=0 values=0/{total} extra=0 why={why[0]}"}
    errors = schema_errors(obj, schema)
    have = flatten(obj)
    right = sum(1 for p, v in want.items() if p in have and same(have[p], v))
    extra = [p for p in have if p not in want]
    why: list = []
    for _path, word in errors[:3]:
        if f"schema:{word}" not in why:
            why.append(f"schema:{word}")
    if right < total or extra:
        # a list of records of the wrong length, else the first value that is
        # not the notes', named for what made it hard
        lists = [a for a in kinds.get("__lists__") or [] if isinstance(_at(key, a), list)]
        if any(not isinstance(_at(obj, a), list) or len(_at(obj, a)) != len(_at(key, a))
               for a in lists):
            why.append("items")
        else:
            bad = next((p for p, v in want.items()
                        if p not in have or not same(have[p], v)), None)
            if bad is not None:
                why.append("wrong:" + str(kinds.get(bad) or "copy"))
            elif extra:
                why.append("invented")
    exact = right == total and not extra and not errors
    if how == "called_more_than_once":
        why.insert(0, how)
    elif exact and how != "bare":
        why.append(how)
    score = 100.0 if exact and how == "bare" else 50.0 if exact else 0.0
    return {"score": score, "accepted": 1.0 if (how == "bare" and not errors) else 0.0,
            "right": right, "total": total, "extra": len(extra), "why": why,
            "detail": (f"accepted={int(how == 'bare' and not errors)} values={right}/{total} "
                       f"extra={len(extra)}" + (" why=" + ";".join(why) if why else ""))}


# --------------------------------------------------------------------------- #
# The notes: how each value is written                                        #
# --------------------------------------------------------------------------- #

PEOPLE = ("Dana", "Ravi", "Mina", "Tobias", "Ines", "Karol", "Lena", "Omar")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_WORDS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_NUM = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_PRIORITY = {"P1": ("everything else stops for it", "nothing else gets done until it has run"),
             "P2": ("it must be done the same day", "it cannot be left until tomorrow"),
             "P3": ("any day this week will do", "it has to happen within the week"),
             "P4": ("it has no deadline", "there is no date it has to be done by")}
_DURATIONS = {20: ("20 minutes",), 30: ("half an hour", "30 minutes"),
              45: ("45 minutes", "three quarters of an hour"), 60: ("an hour", "60 minutes"),
              90: ("an hour and a half", "90 minutes"), 120: ("two hours",),
              150: ("two and a half hours",), 180: ("three hours",), 240: ("four hours",)}
_RETRIES = ("it is not tried again", "it is tried once more", "it is tried again twice",
            "it is tried again up to three times", "it is tried again up to four times")
_CHANNEL = {"email": "An email goes out", "chat": "A chat message goes out",
            "sms": "A text message goes out"}
_MONEY = ((5, "5"), (12.5, "12.50"), (40, "40"), (7.25, "7.25"), (120, "120"), (0.8, "0.80"),
          (1250.5, "1,250.50"), (64, "64"))
_SIGN = {"EUR": "€", "USD": "$", "GBP": "£"}
_TAGS = ("finance", "nightly", "photos", "backups", "household", "reports", "inbox", "media",
         "ledger", "archive")
_NOTES = ("check the VPN first", "ask before changing the schedule", "skips public holidays",
          "needs the blue drive plugged in", "slow on the first run of the month",
          "safe to stop at any time")
_HOSTS = ("ledger", "store", "media", "vault", "relay", "index", "paper", "atlas")


def time_words(h: int, m: int, plain: bool) -> str:
    """A start time as people write it; `plain` gives it in digits."""
    if plain:
        return f"at {h:02d}:{m:02d}"
    part = "in the morning" if h < 12 else "in the afternoon" if h < 18 else "in the evening"
    h12 = h if h <= 12 else h - 12
    nxt = h12 + 1 if h12 < 12 else 1
    if m == 0:
        return f"at {_NUM[h12]} o'clock {part}"
    if m == 15:
        return f"at a quarter past {_NUM[h12]} {part}"
    if m == 30:
        return f"at half past {_NUM[h12]} {part}"
    if m == 45:
        return f"at a quarter to {_NUM[nxt]} {part}"
    return f"at {h12}:{m:02d} {part}"


def _ordinal(d: int) -> str:
    return f"{d}{'th' if 10 <= d % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')}"


def _join(words: list) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": list(props if required is None else required)}


FIELD_SCHEMAS = {
    "name": {"type": "string", "description": "The job's name, exactly as written."},
    "owner": {"type": "string", "description": "First name of the person the job belongs to."},
    "days": {"type": "array", "minItems": 1, "uniqueItems": True,
             "items": {"type": "string", "enum": list(DAYS)},
             "description": "The days it runs, in week order with Monday first."},
    "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$",
             "description": "When it starts, 24-hour HH:MM."},
    "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 720,
                        "description": "How long it may run before it is stopped, in minutes."},
    "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"],
                 "description": "P1 = everything else stops for it, P2 = must be done the "
                                "same day, P3 = within the week, P4 = no deadline."},
    "retries": {"type": "integer", "minimum": 0, "maximum": 5,
                "description": "How many times it is tried again after a failure."},
    "enabled": {"type": "boolean", "description": "Whether the job is switched on."},
    "notify": _obj({"on_failure": {"type": "boolean",
                                   "description": "Whether anyone is told when it fails."},
                    "channel": {"type": "string", "enum": ["email", "chat", "sms"],
                                "description": "How they are told (sms = a text message). "
                                               "Leave out when on_failure is false."}},
                   ["on_failure"]),
    "target": _obj({"host": {"type": "string", "description": "The machine it talks to."},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535}}),
    "budget": _obj({"amount": {"type": "number", "minimum": 0,
                               "description": "The most one run may cost."},
                    "currency": {"type": "string", "enum": ["EUR", "USD", "GBP"]}}),
    "starts_on": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                  "description": "The day of its first run, YYYY-MM-DD."},
    "tags": {"type": "array", "items": {"type": "string"},
             "description": "Its tags, in the order written."},
    "note": {"type": "string",
             "description": "A note shown beside the job, exactly as written. Optional."},
    "depends_on": {"type": "string",
                   "description": "Name of the job that must finish first. Optional."},
}
OPTIONAL = ("note", "depends_on")


def _name(rnd: random.Random, taken: set) -> str:
    while True:
        n = (f"{rnd.choice(('nightly', 'weekly', 'hourly', 'morning', 'evening', 'monthly'))}-"
             f"{rnd.choice(('ledger', 'invoice', 'backup', 'photo', 'media', 'inbox', 'report', 'index', 'paper'))}-"
             f"{rnd.choice(('sync', 'export', 'sweep', 'check', 'rebuild', 'digest'))}")
        if n not in taken:
            taken.add(n)
            return n


def _clock(rnd: random.Random) -> tuple:
    h = rnd.choice([x for x in range(1, 24) if x != 12])
    return h, rnd.choice((0, 15, 30, 45, 0, 15, 30, 45, 5, 10, 20, 40, 50))


def field(rnd: random.Random, f: str, plain: bool = False, other: str = "",
          it: str = "It") -> tuple:
    """(value, the sentence that states it, {relative path: kind}) for one
    field of a job. `plain` writes times and lengths in digits."""
    low = it[0].lower() + it[1:]
    if f == "owner":
        v = rnd.choice(PEOPLE)
        return v, rnd.choice((f"{it} belongs to {v}.", f"{v} owns {low}.")), {"": "copy"}
    if f == "days":
        r = rnd.random()
        v = (list(DAYS[:5]) if r < 0.15 else ["sat", "sun"] if r < 0.25 else list(DAYS)
             if r < 0.3 else sorted(rnd.sample(DAYS, rnd.randint(1, 3)), key=DAYS.index))
        said = list(v)
        if not plain:
            rnd.shuffle(said)
        # "only": Sonnet 5.5 twice read a bare "It runs at weekends" as every
        # day, weekends included
        words = ("only on weekdays" if v == list(DAYS[:5])
                 else "only at weekends" if v == ["sat", "sun"]
                 else "every day of the week" if len(v) == 7
                 else f"every {_DAY_WORDS[v[0]]}" if len(v) == 1
                 else "on " + _join([_DAY_WORDS[d] + "s" for d in said]))
        return v, f"{it} runs {words}.", {f"[{i}]": "days" for i in range(len(v))}
    if f == "time":
        h, m = _clock(rnd)
        return f"{h:02d}:{m:02d}", f"{it} starts {time_words(h, m, plain)}.", {"": "time"}
    if f == "timeout_minutes":
        v = rnd.choice(list(_DURATIONS))
        words = f"{v} minutes" if plain else rnd.choice(_DURATIONS[v])
        return v, f"{it} may run for {words} before it is stopped.", {"": "duration"}
    if f == "priority":
        v = rnd.choice(list(_PRIORITY))
        return v, f"On priority: {rnd.choice(_PRIORITY[v])}.", {"": "code"}
    if f == "retries":
        v = rnd.randint(0, 4)
        return v, f"If {low} fails {_RETRIES[v]}.", {"": "count"}
    if f == "enabled":
        v = rnd.random() < 0.5
        return v, (f"{it} is switched on from the start." if v
                   else f"{it} stays switched off for now."), {"": "switch"}
    if f == "notify":
        if rnd.random() < 0.3:
            return ({"on_failure": False}, f"Nobody is told when {low} fails.",
                    {".on_failure": "switch"})
        ch = rnd.choice(list(_CHANNEL))
        return ({"on_failure": True, "channel": ch}, f"{_CHANNEL[ch]} when {low} fails.",
                {".on_failure": "switch", ".channel": "code"})
    if f == "target":
        host = f"{rnd.choice(_HOSTS)}-{rnd.randint(1, 9):02d}.lan"
        port = rnd.choice((22, 443, 5432, 8443, 9000, 11435, 3306))
        return ({"host": host, "port": port}, f"{it} talks to `{host}` on port {port}.",
                {".host": "copy", ".port": "copy"})
    if f == "budget":
        amount, shown = rnd.choice(_MONEY)
        cur = rnd.choice(list(_SIGN))
        return ({"amount": amount, "currency": cur},
                f"A run may cost at most {_SIGN[cur]}{shown}.",
                {".amount": "money", ".currency": "money"})
    if f == "starts_on":
        mo, d = rnd.randint(1, 12), rnd.randint(1, 28)
        words = rnd.choice((f"{d} {_MONTHS[mo - 1]} 2027", f"{_MONTHS[mo - 1]} {_ordinal(d)}, 2027",
                            f"the {_ordinal(d)} of {_MONTHS[mo - 1]} 2027"))
        return f"2027-{mo:02d}-{d:02d}", f"Its first run is on {words}.", {"": "date"}
    if f == "tags":
        v = rnd.sample(_TAGS, rnd.randint(1, 3))
        return (v, f"{it} is tagged " + _join([f'"{t}"' for t in v]) + ".",
                {f"[{i}]": "copy" for i in range(len(v))})
    if f == "note":
        v = rnd.choice(_NOTES)
        return v, f'The note beside it reads "{v}".', {"": "copy"}
    if f == "depends_on":
        return other, f"{it} may not start until `{other}` has finished.", {"": "copy"}
    raise ValueError(f"unknown field: {f!r}")


def _put(kinds: dict, base: str, f: str, rel: dict) -> None:
    for sub, kind in rel.items():
        kinds[f"{base}{'.' if base else ''}{f}{sub}"] = kind


# --------------------------------------------------------------------------- #
# The four shapes                                                             #
# --------------------------------------------------------------------------- #

FLAT_ORDER = ("owner", "time", "priority", "days", "timeout_minutes", "enabled", "retries",
              "notify", "target", "budget", "starts_on", "tags", "note", "depends_on")
_FLAT = (3, 5, 7, 9, 12, 14)
# 64 records is about 6,800 tokens of JSON, the most that fits the 8,192 a case
# gets: at 128 Sonnet 5.5 and Opus 5.5 both ran to 13,000
_LIST = (1, 2, 4, 8, 16, 32, 48, 64)
_LIST_FIELDS = ("owner", "days", "time", "timeout_minutes", "priority", "enabled")
_SPARSE = (2, 3, 5, 8, 12, 18, 27, 40)
_NESTED = ((1, 2, 1), (2, 2, 1), (2, 3, 2), (3, 3, 2), (3, 4, 3), (4, 5, 3), (5, 6, 3), (6, 8, 3))
SHAPES = ("flat", "list", "nested", "sparse")
MODES = ("text", "tool")
SCHEMA_TOP = 8             # one call a case: the top rungs cost little and are where it tells
SCHEMA_MAX = 8
LADDER_NAMES = {"flat": "wide", "list": "many", "nested": "deep", "sparse": "defaults"}


def _flat(rnd: random.Random, level: int) -> tuple:
    fields = FLAT_ORDER[:_FLAT[min(level, len(_FLAT)) - 1]]
    taken: set = set()
    name = _name(rnd, taken)
    key, kinds, said = {"name": name}, {"name": "copy"}, []
    for f in fields:
        v, s, rel = field(rnd, f, plain=level <= 1, other=_name(rnd, taken))
        key[f] = v
        said.append(s)
        _put(kinds, "", f, rel)
    if level >= 3:
        rnd.shuffle(said)
    schema = _obj({f: FIELD_SCHEMAS[f] for f in ("name",) + fields},
                  [f for f in ("name",) + fields if f not in OPTIONAL])
    return schema, key, kinds, f"The job is called `{name}`. " + " ".join(said)


def _list(rnd: random.Random, level: int, n: int = 0) -> tuple:
    n = n or _LIST[level - 1]
    taken: set = set()
    jobs, kinds, paras = [], {"__lists__": ["jobs"]}, []
    for i in range(n):
        name = _name(rnd, taken)
        job, said = {"name": name}, []
        kinds[f"jobs[{i}].name"] = "copy"
        for f in _LIST_FIELDS:
            v, s, rel = field(rnd, f, plain=level <= 1)
            job[f] = v
            said.append(s)
            _put(kinds, f"jobs[{i}]", f, rel)
        if level >= 3:
            rnd.shuffle(said)
        jobs.append(job)
        paras.append(f"The job `{name}`: " + " ".join(said))
    schema = _obj({"jobs": {"type": "array", "description": "One entry per job, in the order "
                                                             "the notes describe them.",
                            "items": _obj({f: FIELD_SCHEMAS[f]
                                           for f in ("name",) + _LIST_FIELDS})}})
    return schema, {"jobs": jobs}, kinds, "\n\n".join(paras)


def _sparse(rnd: random.Random, n: int, defaults: bool = True, late: bool = True,
            p_own: float = 0.35, p_fix: float = 0.3, p_opt: float = 0.3) -> tuple:
    """Jobs whose notes lean on what was said elsewhere: defaults stated once
    at the top, a time corrected mid-sentence, and one instruction at the end
    that sets `enabled` for every job."""
    taken: set = set()
    names = [_name(rnd, taken) for _ in range(n)]
    base = {f: field(rnd, f) for f in ("owner", "priority", "retries")}
    stated = {"owner": f"Each one belongs to {base['owner'][0]}.",
              "priority": base["priority"][1],
              "retries": f"If one fails {_RETRIES[base['retries'][0]]}."}
    it = "This one" if defaults else "It"
    off = set(rnd.sample(names, max(1, n // 4))) if late else set()
    jobs, kinds, paras = [], {"__lists__": ["jobs"]}, []
    if defaults:
        paras.append("These hold for every job below unless its own paragraph says otherwise. "
                     + " ".join(stated[f] for f in ("owner", "priority", "retries")))
    for i, name in enumerate(names):
        job, said = {"name": name}, []
        kinds[f"jobs[{i}].name"] = "copy"
        for f in ("owner", "priority", "retries"):
            if defaults and rnd.random() >= p_own:
                job[f] = base[f][0]
                kinds[f"jobs[{i}].{f}"] = "default"
                continue
            v, s, rel = field(rnd, f, it=it)
            while defaults and same(v, base[f][0]):
                v, s, rel = field(rnd, f, it=it)
            job[f] = v
            said.append(s.replace("On priority:", "On priority, for this one:") if defaults else s)
            _put(kinds, f"jobs[{i}]", f, rel)
        h, m = _clock(rnd)
        job["time"] = f"{h:02d}:{m:02d}"
        if rnd.random() < p_fix:
            h0 = rnd.choice([x for x in range(1, 24) if x not in (12, h)])
            lead = (f"It starts {time_words(h0, m, False)} - correction, "
                    f"{time_words(h, m, False)}.")
            kinds[f"jobs[{i}].time"] = "correction"
        else:
            lead = f"It starts {time_words(h, m, False)}."
            kinds[f"jobs[{i}].time"] = "time"
        if late:
            job["enabled"] = name not in off
            kinds[f"jobs[{i}].enabled"] = "late"
        else:
            v, s, rel = field(rnd, "enabled")
            job["enabled"] = v
            said.append(s)
            _put(kinds, f"jobs[{i}]", "enabled", rel)
        if rnd.random() < p_opt:
            v, s, rel = field(rnd, "note")
            job["note"] = v
            said.append(s)
            _put(kinds, f"jobs[{i}]", "note", rel)
        if i and rnd.random() < p_opt:
            v, s, rel = field(rnd, "depends_on", other=rnd.choice(names[:i]))
            job["depends_on"] = v
            said.append(s)
            _put(kinds, f"jobs[{i}]", "depends_on", rel)
        rnd.shuffle(said)
        jobs.append(job)
        paras.append(f"The job `{name}`: {lead} " + " ".join(said))
    if late:
        gone = [n_ for n_ in names if n_ in off]
        paras.append("Last thing: every job above is switched on, except "
                     + _join([f"`{x}`" for x in gone])
                     + (", which stays" if len(gone) == 1 else ", which stay")
                     + " switched off for now.")
    fields = ("name", "owner", "priority", "retries", "time", "enabled", "note", "depends_on")
    schema = _obj({"jobs": {"type": "array", "description": "One entry per job, in the order "
                                                             "the notes describe them.",
                            "items": _obj({f: FIELD_SCHEMAS[f] for f in fields},
                                          [f for f in fields if f not in OPTIONAL])}})
    return schema, {"jobs": jobs}, kinds, "\n\n".join(paras)


_ACTIONS = {"restart": "restart", "snapshot": "take a snapshot of", "drain": "drain",
            "verify": "verify", "patch": "patch"}
_METRICS = {"cpu": ("CPU load", "percent"), "memory": ("memory use", "percent"),
            "disk": ("disk use", "percent"), "latency": ("latency", "ms")}
_SECTIONS = ("Prepare", "Fail over", "Verify", "Clean up", "Hand back", "Warm the cache",
             "Freeze writes", "Report")
NESTED_SCHEMA = _obj({
    "title": {"type": "string", "description": "The runbook's title, exactly as written."},
    "sections": {"type": "array", "description": "In the order written.", "items": _obj({
        "name": {"type": "string"},
        "steps": {"type": "array", "description": "In the order written.", "items": _obj({
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "target": {"type": "string", "description": "The machine the step acts on."},
            "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 720,
                                "description": "How long the step may take, in minutes. "
                                               "Optional."},
            "checks": {"type": "array", "description": "What must hold after the step.",
                       "items": _obj({
                           "metric": {"type": "string", "enum": list(_METRICS)},
                           "comparison": {"type": "string", "enum": ["below", "above"]},
                           "threshold": {"type": "number"},
                           "unit": {"type": "string", "enum": ["percent", "ms"]}})},
        }, ["action", "target", "checks"])},
    })},
})


def _nested(rnd: random.Random, level: int) -> tuple:
    n_sec, n_step, n_chk = _NESTED[level - 1]
    title = rnd.choice(("Storage failover", "Monthly patch round", "Media node swap",
                        "Ledger restore drill"))
    key = {"title": title, "sections": []}
    kinds = {"title": "copy", "__lists__": ["sections"]}
    lines = [f'The runbook is called "{title}". It has {_NUM[n_sec]} '
             f'section{"" if n_sec == 1 else "s"}.']
    for i, sname in enumerate(rnd.sample(_SECTIONS, n_sec)):
        sec = {"name": sname, "steps": []}
        kinds[f"sections[{i}].name"] = "copy"
        kinds["__lists__"].append(f"sections[{i}].steps")
        lines.append(f'\nSection {i + 1}, "{sname}", has {_NUM[n_step]} steps.')
        for j in range(n_step):
            base = f"sections[{i}].steps[{j}]"
            action = rnd.choice(list(_ACTIONS))
            host = f"{rnd.choice(_HOSTS)}-{rnd.randint(1, 9):02d}.lan"
            step = {"action": action, "target": host}
            kinds[f"{base}.action"], kinds[f"{base}.target"] = "code", "copy"
            said = f"Step {j + 1}: {_ACTIONS[action]} `{host}`"
            if rnd.random() < 0.5:
                v = rnd.choice(list(_DURATIONS))
                step["timeout_minutes"] = v
                kinds[f"{base}.timeout_minutes"] = "duration"
                said += f", allowing {rnd.choice(_DURATIONS[v])}"
            checks, parts = [], []
            for k, metric in enumerate(rnd.sample(list(_METRICS), rnd.randint(1, n_chk))):
                words, unit = _METRICS[metric]
                cmp_ = rnd.choice(("below", "above"))
                thr = rnd.randrange(50, 100, 5) if unit == "percent" else rnd.randrange(50, 550, 50)
                checks.append({"metric": metric, "comparison": cmp_, "threshold": thr,
                               "unit": unit})
                for leaf in ("metric", "comparison", "unit"):
                    kinds[f"{base}.checks[{k}].{leaf}"] = "code"
                kinds[f"{base}.checks[{k}].threshold"] = "copy"
                parts.append(f"{words} is {cmp_} {thr} {unit}")
            kinds["__lists__"].append(f"{base}.checks")
            step["checks"] = checks
            sec["steps"].append(step)
            lines.append(said + ". Afterwards check that " + _join(parts) + ".")
        key["sections"].append(sec)
    return NESTED_SCHEMA, key, kinds, "\n".join(lines)


def _case(shape: str, mode: str, level: int, seed: int, built: tuple, value: str = "") -> dict:
    schema, key, kinds, notes = built
    errors = schema_errors(key, schema)
    if errors:                                               # pragma: no cover
        raise ValueError(f"the key of {shape} L{level} breaks its own schema: {errors[:3]}")
    if mode == "tool":
        messages = [{"role": "system", "content": TEXT_SYSTEM},
                    {"role": "user", "content": notes + "\n\n" + TOOL_RULE}]
        tools = [{"name": TOOL_NAME,
                  "description": "File what the notes describe, for the program that reads it.",
                  "parameters": schema}]
    else:
        messages = [{"role": "system", "content": TEXT_SYSTEM},
                    {"role": "user", "content": notes + "\n\nThe schema:\n```json\n"
                     + json.dumps(schema, indent=1) + "\n```\n\n" + TEXT_RULE}]
        tools = []
    return {"shape": shape, "mode": mode, "level": level, "seed": seed, "value": value,
            "task": f"{shape}-{mode}-L{level}-{seed}" + (f"-{value}" if value else ""),
            "messages": messages, "tools": tools, "schema": schema, "key": key, "kinds": kinds,
            "values": len(flatten(key))}


def schema_spec(shape: str, mode: str, level: int, seed: int = 0) -> dict:
    """One case on a ladder: a shape (wide, many, deep, defaults), the way the
    object is handed over (text, tool), and a level."""
    level = max(1, min(SCHEMA_MAX, int(level)))
    if mode not in MODES:
        raise ValueError(f"unknown way to hand it over: {mode!r}")
    rnd = random.Random(f"schema|{shape}|{mode}|{level}|{seed}")
    if shape == "flat":
        built = _flat(rnd, level)
    elif shape == "list":
        built = _list(rnd, level)
    elif shape == "nested":
        built = _nested(rnd, level)
    elif shape == "sparse":
        built = _sparse(rnd, _SPARSE[level - 1])
    else:
        raise ValueError(f"unknown shape: {shape!r}")
    return _case(shape, mode, level, seed, built)


VALUES = ("time", "duration", "code", "days", "money", "date", "optional", "default",
          "correction", "late")
VALUE_SEEDS = (0, 1)
_VALUE_FIELD = {"time": "time", "duration": "timeout_minutes", "code": "priority",
                "days": "days", "money": "budget", "date": "starts_on"}


def value_spec(value: str, seed: int = 0) -> dict:
    """One small case for one thing that makes a value hard, so a miss on it
    is that and nothing else."""
    if value not in VALUES:
        raise ValueError(f"unknown kind of value: {value!r}")
    rnd = random.Random(f"schema|value|{value}|{seed}")
    if value == "default":
        built = _sparse(rnd, 2, late=False, p_own=0.0, p_fix=0.0, p_opt=0.0)
    elif value == "correction":
        built = _sparse(rnd, 1, defaults=False, late=False, p_fix=1.0, p_opt=0.0)
    elif value == "late":
        built = _sparse(rnd, 3, defaults=False, p_fix=0.0, p_opt=0.0)
    elif value == "optional":
        # the schema offers a note and a job to wait for; the notes give neither
        built = _sparse(rnd, 2, defaults=False, late=False, p_fix=0.0, p_opt=0.0)
    else:
        f = _VALUE_FIELD[value]
        name = _name(rnd, set())
        key, kinds, said = {"name": name}, {"name": "copy"}, []
        for g in ("owner", f):
            v, s, rel = field(rnd, g)
            key[g] = v
            said.append(s)
            _put(kinds, "", g, rel)
        built = (_obj({g: FIELD_SCHEMAS[g] for g in ("name", "owner", f)}), key, kinds,
                 f"The job is called `{name}`. " + " ".join(said))
    return _case("value", "text", 1, seed, built, value)


def schema_specs(top_level: int = 0) -> list:
    """Every case of the suite: each shape handed over both ways up to
    `top_level`, then the single values."""
    top = max(2, min(SCHEMA_MAX, int(top_level or SCHEMA_TOP)))
    out = []
    for shape in SHAPES:
        for mode in MODES:
            for level in range(1, min(top, len(_FLAT)) + 1 if shape == "flat" else top + 1):
                sp = schema_spec(shape, mode, level, 0)
                sp["ladder"] = f"schema:{LADDER_NAMES[shape]} as {'a call' if mode == 'tool' else 'text'}"
                out.append(sp)
    for value in VALUES:
        for seed in VALUE_SEEDS:
            sp = value_spec(value, seed)
            sp["ladder"] = ""
            out.append(sp)
    return out
