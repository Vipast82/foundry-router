"""Summaries that invent nothing (v0.181.244).

Video notes and infrastructure pulses are summaries, and nothing measured
whether one says only what its source says. A case here is a source to be
summarised for someone who missed it - a service log with several services
interleaved, or the transcript of a call that went through several items -
and one subject in it: one service, or one item. The others are made like it
on purpose (billing-api beside billing-api-eu, the spring fair beside the
summer fair) and carry the same kinds of facts: an error code, a count, a
time, a name, a version, an amount, a reference.

Scoring needs no judge. Every figure the summary gives - a time, a number of
11 or more, a version, an error code, a name or a place the source uses - is
looked up in the source: on a line about the subject it is fine; found only on
lines about something else it is a fact about something else; found nowhere it
is invented. Either is a 0, as is going over the word limit. With nothing
wrong, 100 when every planted fact about the subject is there and 50 when at
least half are. The dials are the length of the source, the number of facts
about the subject and how many look-alikes stand beside it.
"""
from __future__ import annotations

import random
import re

SUMM_TOP = 8
SUMM_MAX = 8
KINDS = ("log", "talk")
LADDERS = ("length", "facts", "near")
LENGTHS = (24, 48, 96, 192, 384, 640, 960, 1280)   # lines in the whole source, by level
FACT_COUNTS = (2, 3, 4, 6, 8, 11, 14, 18)          # facts about the subject, by level
NEAR_COUNTS = (1, 2, 3, 4, 5, 6, 7, 8)             # look-alikes beside it, by level
BASE_FACTS = 4
BASE_LINES = 160
NEAR_LINES = 200
BASE_DECOYS = 2
DECOY_FACTS = 6
SINGLE_LINES = 120
SINGLES = {"log": ("correction", "quiet", "late"), "talk": ("correction", "quiet", "aside")}
SINGLE_SEEDS = 2
POUND = chr(163)
CHAIR = "Morgan"
DAY = "2026-11-18"
LADDER_NAMES = {"length": "length of the source", "facts": "facts to keep",
                "near": "look-alikes beside it"}
SINGLE_NAMES = {"correction": "a figure corrected later", "quiet": "a quiet subject",
                "late": "the facts at the very end", "aside": "a fact said in another item"}
KIND_NAMES = {"log": "a service log", "talk": "a call transcript"}

SYSTEM = ("You write short summaries for people who missed something. Say only what the "
          "source says.")


def word_cap(facts: int) -> int:
    return 30 + 10 * int(facts)


# --------------------------------------------------------------------------- #
# What a piece of text states: the figures a summary can get wrong            #
# --------------------------------------------------------------------------- #

_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?![\w.]?\d)", re.I)
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CODE = re.compile(r"\bERR_[A-Z]+(?:_[A-Z]+)+\b")
_TIMES = (re.compile(r"\b(\d{1,2})\.(\d{2})\s*([ap])\.?\s?m\b\.?", re.I),
          re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([ap])\.?\s?m\b\.?)?", re.I),
          re.compile(r"\b(\d{1,2})()\s*([ap])\.?\s?m\b\.?", re.I))
_NUM = re.compile(r"(?<![A-Za-z0-9])(\d{1,3}(?:,\d{3})+|\d+)(?!\d)")
_NAME = re.compile(r"\b[A-Z][a-z]+\b")


def norm_code(code: str) -> str:
    """ERR_POOL_EXHAUSTED and "pool exhausted" are the same code."""
    c = " ".join(re.sub(r"[_\-]", " ", str(code).lower()).split())
    return re.sub(r"^err\s+", "", c)


def _clock(h, m, ap) -> str:
    h, m = int(h), int(m or 0)
    if ap:
        if not 1 <= h <= 12:
            return ""
        h = h % 12 + (12 if ap.lower() == "p" else 0)
    return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else ""


def _blank(m) -> str:
    return " " * len(m.group(0))


def tokens(text: str, names=(), places=(), codes=()) -> set:
    """Every figure the text states, typed: t:HH:MM, n:<number of 11 or more>,
    v:<version>, c:<error code>, w:<a name the source uses>, p:<a place the
    source uses>. A time in am/pm form reads as the same 24-hour time; a code
    written out in words ("pool exhausted") as the code."""
    raw = str(text or "")
    s, out = raw, set()
    for m in _VERSION.finditer(s):
        out.add("v:" + m.group(1))
    s = _VERSION.sub(_blank, s)
    s = _ISO.sub(_blank, s)
    for m in _CODE.finditer(s):
        out.add("c:" + norm_code(m.group(0)))
    s = _CODE.sub(_blank, s)
    low = " " + " ".join(re.sub(r"[_\-]", " ", s.lower()).split()) + " "
    for code in codes:
        ph = norm_code(code)
        if ph and re.search(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", low):
            out.add("c:" + ph)
    for rx in _TIMES:
        for m in rx.finditer(s):
            t = _clock(*m.groups())
            if t:
                out.add("t:" + t)
        s = rx.sub(_blank, s)
    for m in _NUM.finditer(s):
        v = int(m.group(1).replace(",", ""))
        if v >= 11:
            out.add("n:%d" % v)
    nameset, placeset = set(names), set(places)
    for w in _NAME.findall(raw):
        if w in nameset:
            out.add("w:" + w.lower())
        if w in placeset:
            out.add("p:" + w.lower())
    return out


def count_words(text: str) -> int:
    return sum(1 for w in str(text or "").split() if re.search(r"\w", w))


WHY_WORDS = {"nothing": "said nothing", "too_long": "over the word limit",
             "theirs": "a fact about something else",
             "invented": "a figure that is not in the source",
             "left_out": "left out most of the facts",
             "some_left_out": "left out some facts"}


def summ_score(text: str, ref: dict) -> dict:
    """{score, present, total, words, theirs, invented, missing, why, detail}.

    0 when nothing is said, the summary runs over its word limit, or it gives
    a figure found only on lines about something else, or found nowhere. With
    none of those, 100 when every fact about the subject is there, 50 when at
    least half are."""
    text = str(text or "").strip()
    facts = ref.get("facts") or []
    cap = int(ref.get("cap") or 0)
    n_words = count_words(text)
    got = tokens(text, ref.get("names") or (), ref.get("places") or (),
                 ref.get("codes") or ())
    mine, theirs = set(ref.get("mine") or ()), set(ref.get("theirs") or ())
    other = sorted(t for t in got if t not in mine and t in theirs)
    invented = sorted(t for t in got if t not in mine and t not in theirs)
    present = [f for f in facts if any(t in got for t in f["any"])]
    missing = [f["label"] for f in facts if not any(t in got for t in f["any"])]
    if not text:
        why = "nothing"
    elif cap and n_words > cap:
        why = "too_long"
    elif other:
        why = "theirs"
    elif invented:
        why = "invented"
    elif not missing:
        why = ""
    elif 2 * len(present) >= len(facts):
        why = "some_left_out"
    else:
        why = "left_out"
    score = 100.0 if not why else 50.0 if why == "some_left_out" else 0.0

    def _j(xs):
        return ",".join(x.replace(" ", "_") for x in xs)
    detail = f"facts={len(present)}/{len(facts)} words={n_words}/{cap}"
    if why:
        detail += f" why={why}"
    if other:
        detail += " theirs=" + _j(other)
    if invented:
        detail += " invented=" + _j(invented)
    if missing:
        detail += " missing=" + _j(missing)
    return {"score": score, "present": len(present), "total": len(facts), "words": n_words,
            "theirs": other, "invented": invented, "missing": missing, "why": why,
            "detail": detail}


# --------------------------------------------------------------------------- #
# Values that occur once in a whole source                                    #
# --------------------------------------------------------------------------- #

_FIRST = ("Dana", "Priya", "Tomasz", "Ines", "Kofi", "Mirela", "Anders", "Yusuf", "Hana",
          "Callum", "Sofia", "Ravi", "Elif", "Bruno", "Aoife", "Kenji", "Leona", "Matteo",
          "Nadia", "Oskar", "Freya", "Idris", "Zainab", "Lukas", "Marisol", "Teodor",
          "Amara", "Henrik", "Saoirse", "Dmitri", "Noor", "Emeka", "Greta", "Rafael",
          "Signe", "Tariq", "Vesna", "Wendell", "Yara", "Xavier")
_LAST = ("Okafor", "Lindqvist", "Novak", "Haddad", "Mensah", "Petrescu", "Brandt", "Kaya",
         "Sato", "Ferreira", "Quigley", "Moreau", "Achterberg", "Banerjee", "Castellano",
         "Delacroix", "Eriksen", "Fitzgerald", "Gallagher", "Hoffmann", "Iwasaki",
         "Jovanovic", "Kowalczyk", "Larsen", "Mahlangu", "Nakamura", "Oyelaran", "Pellegrini",
         "Ramaswamy", "Szabo", "Takahashi", "Underwood", "Valdivia", "Whitcombe", "Yilmaz",
         "Zielinski", "Abernethy", "Brannigan", "Coutinho", "Drummond")
_PLACES = ("Cardross", "Quarrybank", "Elmhurst", "Rowanlea", "Thornfield", "Glenmore",
           "Larchmont", "Fenwick", "Holloway", "Marchbank", "Redbrae", "Stanmore", "Wexcombe",
           "Alderley", "Brackenridge", "Coldwell", "Dunmore", "Ashgrove", "Birkhill",
           "Corsewall", "Ettrick", "Fairlie", "Gartmore", "Hawkhill")
_NOUNS = ("POOL", "DISK", "QUOTA", "TOKEN", "SOCKET", "LOCK", "SCHEMA", "CERT", "DNS",
          "CACHE", "SHARD", "REPLICA")
_STATES = ("EXHAUSTED", "TIMEOUT", "EXPIRED", "REFUSED", "CORRUPT", "FULL", "STALE",
           "MISSING")


class _Pools:
    """Draws values that occur once in a whole source: numbers, times, codes,
    people, versions, places."""

    def __init__(self, rnd: random.Random):
        self.rnd = rnd
        self.numbers: set = {18, 2026}         # the day of the log: never a fact
        self.times: set = set()
        self.versions: set = set()
        firsts, lasts = list(_FIRST), list(_LAST)
        rnd.shuffle(firsts)
        rnd.shuffle(lasts)
        self.people = list(zip(firsts, lasts))
        self.codes = [f"ERR_{n}_{s}" for n in _NOUNS for s in _STATES]
        rnd.shuffle(self.codes)
        self.places = list(_PLACES)
        rnd.shuffle(self.places)
        self.used_people: list = []
        self.used_codes: list = []
        self.used_places: list = []

    def number(self, lo: int, hi: int) -> int:
        for _ in range(500):
            n = self.rnd.randint(lo, hi)
            if n not in self.numbers:
                self.numbers.add(n)
                return n
        raise ValueError("no number left")

    def clock(self) -> str:
        for _ in range(500):
            t = f"{self.rnd.randint(7, 21):02d}:{self.rnd.randrange(0, 60, 5):02d}"
            if t not in self.times:
                self.times.add(t)
                return t
        raise ValueError("no time left")

    def version(self) -> str:
        for _ in range(500):
            v = f"{self.rnd.randint(2, 9)}.{self.rnd.randint(1, 40)}.{self.rnd.randint(0, 20)}"
            if v not in self.versions:
                self.versions.add(v)
                return v
        raise ValueError("no version left")

    def code(self) -> str:
        c = self.codes.pop()
        self.used_codes.append(c)
        return c

    def person(self) -> tuple:
        p = self.people.pop()
        self.used_people.append(p)
        return p

    def place(self) -> str:
        p = self.places.pop()
        self.used_places.append(p)
        return p

    def ref(self) -> str:
        a, b = self.rnd.sample("ABCDEFGHJKLMNPRSTVWXZ", 2)
        return f"{a}{b}-{self.number(1000, 9899)}"


def _fmt(n: int) -> str:
    return f"{n:,}"


def _cycle(kinds: tuple, n: int, start: int = 0) -> list:
    return [kinds[(start + i) % len(kinds)] for i in range(n)]


# --------------------------------------------------------------------------- #
# A service log                                                               #
# --------------------------------------------------------------------------- #

_FAMILIES = ("billing", "search", "media", "orders", "mail", "auth")
_SUFFIXES = ("worker", "cron", "gateway", "export", "sync", "ledger", "canary", "api-eu",
             "api-us", "api-next")
_LOG_KINDS = ("error", "count", "restart", "page", "deploy", "ticket", "latency")
_COUNTS = ("{n} requests failed in the last 5 minutes", "retry queue at {n} jobs",
           "{n} messages moved to the dead-letter queue", "{n} payments stuck in pending",
           "{n} sessions dropped")
_COUNT_SAYS = ("{n} requests failed", "retry queue at {n} jobs",
               "{n} messages dead-lettered", "{n} payments stuck", "{n} sessions dropped")
_ERRDESC = ("no free connections", "request rejected", "write failed",
            "upstream closed the connection", "lookup failed", "could not verify")
_PATHS = ("invoices", "search", "upload", "orders", "users", "status", "items", "send")
_DEPS = ("postgres", "redis", "the queue", "object storage", "the mail relay")
_JOBS = ("cleanup", "reindex", "export", "digest", "rollup")


def _background(rnd: random.Random) -> tuple:
    r = rnd.random()
    if r < 0.35:
        return ("INFO", f"GET /v1/{rnd.choice(_PATHS)} ok in {rnd.randint(2, 9)} ms")
    if r < 0.5:
        return ("INFO", "health check ok")
    if r < 0.6:
        b = rnd.randint(4, 10)
        return ("DEBUG", f"worker pool {rnd.randint(1, b)}/{b} busy")
    if r < 0.7:
        return ("INFO", f"connection to {rnd.choice(_DEPS)} ok")
    if r < 0.8:
        return ("INFO", f"scheduled job {rnd.choice(_JOBS)} finished")
    return ("INFO", rnd.choice(("config reloaded", "cache warmed", "token refreshed",
                                "log file rotated")))


def _log_fact(pools: _Pools, kind: str, seen: dict) -> dict:
    """One line that carries a fact: {lvl, text, facts: [{label, any, say, when}]}."""
    rnd = pools.rnd
    k = seen.get(kind, 0)
    seen[kind] = k + 1
    if kind == "error":
        c = pools.code()
        return {"lvl": "ERROR", "text": f"{c}: {rnd.choice(_ERRDESC)}",
                "facts": [{"label": "error", "any": ["c:" + norm_code(c)], "say": c}]}
    if kind == "count":
        n = pools.number(120, 9800)
        i = k % len(_COUNTS)
        return {"lvl": "WARN", "text": _COUNTS[i].format(n=_fmt(n)), "count": i,
                "facts": [{"label": "count", "any": [f"n:{n}"],
                           "say": _COUNT_SAYS[i].format(n=_fmt(n))}]}
    if kind == "restart":
        return {"lvl": "WARN", "text": "restarted by the watchdog after 3 missed health checks",
                "facts": [{"label": "restart time", "any": [], "say": "restarted at {t}",
                           "when": True}]}
    if kind == "page":
        first, last = pools.person()
        return {"lvl": "INFO", "text": f"paged on-call {first} {last}; acknowledged",
                "facts": [{"label": "name", "any": ["w:" + first.lower(), "w:" + last.lower()],
                           "say": f"paged {first} {last}"}]}
    if kind == "deploy":
        v = pools.version()
        return {"lvl": "INFO", "text": f"deployed version {v}",
                "facts": [{"label": "version", "any": ["v:" + v], "say": f"deployed {v}"}]}
    if kind == "ticket":
        n = pools.number(10000, 98999)
        return {"lvl": "INFO", "text": f"opened incident INC-{n}",
                "facts": [{"label": "incident", "any": [f"n:{n}"], "say": f"incident INC-{n}"}]}
    if kind == "latency":
        n = pools.number(1200, 9800)
        return {"lvl": "WARN", "text": f"peak latency {_fmt(n)} ms over the last minute",
                "facts": [{"label": "latency", "any": [f"n:{n}"],
                           "say": f"peak latency {_fmt(n)} ms"}]}
    raise ValueError(kind)


def _hms(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def _log(rnd: random.Random, n_lines: int, n_facts: int, n_decoys: int, near: bool,
         variant: str) -> dict:
    fam = rnd.choice(_FAMILIES)
    subject = f"{fam}-api"
    near_pool = [f"{fam}-{s}" for s in _SUFFIXES]
    far_pool = [f"{f}-{s}" for f in _FAMILIES if f != fam for s in ("api", "worker", "cron")]
    others = (rnd.sample(near_pool, n_decoys) if near
              else rnd.sample(near_pool, 1) + rnd.sample(far_pool, n_decoys - 1))
    pools = _Pools(rnd)
    seen: dict = {}
    if variant == "quiet":
        v = pools.version()
        subj = [{"lvl": "INFO", "text": f"deployed version {v}",
                 "facts": [{"label": "version", "any": ["v:" + v], "say": f"deployed {v}"},
                           {"label": "time", "any": [], "say": "at {t}", "when": True}]}]
    else:
        kinds = _cycle(_LOG_KINDS, n_facts)
        if variant == "correction" and "count" not in kinds:
            kinds[-1] = "count"
        subj = [_log_fact(pools, k, seen) for k in kinds]
    entries = [dict(e, svc=subject) for e in subj]
    if variant == "correction":
        orig = next(e for e in entries if e.get("count") is not None)
        n_old = int(orig["facts"][0]["any"][0][2:])
        n_new = pools.number(120, 9800)
        what = _COUNTS[orig["count"]].format(n="").strip()
        orig["facts"] = []                     # the first figure is not the one to keep
        fix = {"svc": subject, "lvl": "INFO", "after": True,
               "text": f"correction: the figure above was counted twice; the true figure is "
                       f"{_fmt(n_new)}, not {_fmt(n_old)} ({what})",
               "facts": [{"label": "corrected count", "any": [f"n:{n_new}"],
                          "say": f"corrected to {_fmt(n_new)} ({what})"}]}
        orig["first"] = True
        entries.append(fix)
    dfacts = DECOY_FACTS if variant == "quiet" else max(3, min(n_facts, DECOY_FACTS))
    for d in others:
        dseen: dict = {}
        for k in _cycle(_LOG_KINDS, dfacts, rnd.randrange(len(_LOG_KINDS))):
            entries.append(dict(_log_fact(pools, k, dseen), svc=d, decoy=True))
    services = [subject] + others
    total = max(n_lines, len(entries) + 2 * len(services))
    bg = [subject] + [rnd.choice(services) for _ in range(total - len(entries) - 1)]
    for s in bg:
        lvl, text = _background(rnd)
        entries.append({"svc": s, "lvl": lvl, "text": text, "facts": [], "decoy": s != subject})
    rnd.shuffle(entries)
    if variant == "correction":
        i = next(k for k, e in enumerate(entries) if e.get("first"))
        j = next(k for k, e in enumerate(entries) if e.get("after"))
        if j < i:
            entries[i], entries[j] = entries[j], entries[i]
    if variant == "late":
        mine = [e for e in entries if e["svc"] == subject and e["facts"]]
        rest = [e for e in entries if not (e["svc"] == subject and e["facts"])]
        entries = rest + mine                  # every fact about it in the last lines
    avg = max(30, min(240, 43200 // total))
    sec = 6 * 3600 + rnd.randint(0, 1800)
    stamps = []
    for _ in entries:
        sec += rnd.randint(max(5, avg // 3), avg * 5 // 3)
        stamps.append(sec)
    minute = [_hms(s)[:5] for s in stamps]
    subj_min = {minute[i] for i, e in enumerate(entries) if e["svc"] == subject}
    for i, e in enumerate(entries):        # a look-alike's restart time is never the subject's
        if e.get("decoy") and any(f.get("when") for f in e["facts"]) and minute[i] in subj_min:
            free = sorted((abs(j - i), j) for j, x in enumerate(entries)
                          if x.get("decoy") and not x["facts"] and minute[j] not in subj_min)
            if free:
                j = free[0][1]
                entries[i], entries[j] = entries[j], entries[i]
    lines, owner, facts, theirs_say = [], [], [], []
    for i, e in enumerate(entries):
        for f in e["facts"]:
            if f.get("when"):
                f["any"] = ["t:" + minute[i]]
                f["say"] = f["say"].format(t=minute[i])
            (theirs_say if e.get("decoy") else facts).append(f)
        lines.append(f"{_hms(stamps[i])} {e['svc']} {e['lvl']} {e['text']}")
        owner.append(e["svc"] == subject)
    header = (f"Service log for {DAY}, times in UTC. Each line is the time, the service, "
              f"the level and the message.")
    cap = word_cap(len(facts))
    ask = (f"Summarise what this log shows about the service {subject} for someone who "
           f"missed it, in at most {cap} words. Keep every time, count, name, version, "
           f"error code and incident number that matters to it. Leave out everything about "
           f"the other services, even ones with similar names. Use only what the log says: "
           f"give figures exactly as they appear and do not work out new ones.")
    return {"subject": subject, "others": others, "header": header, "lines": lines,
            "owner": owner, "facts": facts, "decoy_facts": theirs_say, "ask": ask, "cap": cap,
            "names": [w for p in pools.used_people for w in p], "places": [],
            "codes": list(pools.used_codes)}


# --------------------------------------------------------------------------- #
# A call transcript                                                           #
# --------------------------------------------------------------------------- #

_TALK = {
    "fair": {
        "words": ("spring", "summer", "autumn", "winter", "craft", "book", "plant", "food",
                  "charity"),
        "topic": "the {w} fair",
        "time": ("set-up starts at {t}", "doors open at {t}", "the raffle is drawn at {t}",
                 "we pack down at {t}"),
        "count": ("we need {n} trestle tables", "{n} stalls are booked",
                  "we've sold {n} tickets", "{n} volunteers have signed up"),
        "money": ("the hall deposit is {m}", "the float is {m}", "the insurance comes to {m}"),
        "ref": ("the booking reference is {r}", "the insurance policy number is {r}",
                "the invoice number is {r}"),
        "place": ("it's in {p} Hall", "overflow parking is at {p} School",
                  "the tables come from {p} Church"),
        "owner": ("{who} is running the raffle", "{who} is doing the volunteer rota",
                  "{who} is collecting the keys"),
    },
    "move": {
        "words": ("Leith", "Bristol", "Cardiff", "Derby", "Exeter", "Galway", "Hull", "Luton",
                  "Perth"),
        "topic": "the {w} office move",
        "time": ("the van arrives at {t}", "the keys are handed over at {t}",
                 "the network goes down at {t}", "the movers finish at {t}"),
        "count": ("{n} boxes are packed", "we have {n} desks to move",
                  "{n} people are moving across", "{n} chairs are going to storage"),
        "money": ("the removal quote is {m}", "the storage costs {m} a month",
                  "the cleaning comes to {m}"),
        "ref": ("the job number is {r}", "the storage unit number is {r}",
                "the parking permit number is {r}"),
        "place": ("storage is at {p} Park", "the van picks up from {p} Yard",
                  "the old furniture goes to {p} Depot"),
        "owner": ("{who} is booking the van", "{who} is labelling the crates",
                  "{who} is handing back the keys"),
    },
    "issue": {
        "words": ("March", "April", "June", "July", "August", "October", "November",
                  "December", "January"),
        "topic": "the {w} newsletter",
        "time": ("it goes out at {t}", "the proof is due at {t}",
                 "the printer collects it at {t}", "the editorial call is at {t}"),
        "count": ("we have {n} subscribers", "{n} copies are being printed",
                  "{n} people opened the last one", "the main piece is {n} words"),
        "money": ("printing is {m}", "the photographer charges {m}",
                  "postage comes to {m}"),
        "ref": ("the print order is {r}", "the issue reference is {r}",
                "the advert booking is {r}"),
        "place": ("it's printed at {p} Press", "the launch is at {p} Library",
                  "the photos were taken at {p} Gardens"),
        "owner": ("{who} is editing it", "{who} is writing the lead piece",
                  "{who} is sorting the mailing list"),
    },
}
_TALK_KINDS = ("time", "count", "owner", "money", "ref", "place")
_LABELS = {"time": "time", "count": "number", "owner": "name", "money": "amount",
           "ref": "reference", "place": "place"}
_CHAT = ("Sounds good to me.", "I'll send round the details after this.",
         "Can we come back to that next week?", "I think that works.", "Yes, agreed.",
         "Let me check and get back to you.", "That's the main thing from me.", "Fine by me.",
         "I'll chase them again tomorrow.", "I've put it all in the shared folder.",
         "We talked about this last time as well.", "Does anyone have a problem with that?",
         "No, that's fine.", "Good, that's settled then.", "I'll write it up.",
         "Sorry, I was on mute.", "Can you say that again?", "I'll double-check with them.",
         "Same as last time, really.", "Nothing else from me on that.")


def _cap1(s: str) -> str:
    return s[:1].upper() + s[1:]


def _talk_fact(pools: _Pools, fam: dict, kind: str, seen: dict, people: list) -> dict:
    """One sentence that carries a fact: {text, facts: [...]}, `count` for a count."""
    k = seen.get(kind, 0)
    seen[kind] = k + 1
    tpl = fam[kind][k % len(fam[kind])]
    if kind == "time":
        t = pools.clock()
        return {"text": tpl.format(t=t),
                "facts": [{"label": "time", "any": ["t:" + t], "say": tpl.format(t=t)}]}
    if kind == "count":
        n = pools.number(13, 990)
        return {"text": tpl.format(n=_fmt(n)), "tpl": tpl, "n": n,
                "facts": [{"label": "number", "any": [f"n:{n}"], "say": tpl.format(n=_fmt(n))}]}
    if kind == "money":
        n = pools.number(25, 2400)
        m = POUND + _fmt(n)
        return {"text": tpl.format(m=m),
                "facts": [{"label": "amount", "any": [f"n:{n}"], "say": tpl.format(m=m)}]}
    if kind == "ref":
        r = pools.ref()
        return {"text": tpl.format(r=r),
                "facts": [{"label": "reference", "any": ["n:" + r.split("-")[1]],
                           "say": tpl.format(r=r)}]}
    if kind == "place":
        p = pools.place()
        return {"text": tpl.format(p=p),
                "facts": [{"label": "place", "any": ["p:" + p.lower()], "say": tpl.format(p=p)}]}
    if kind == "owner":
        first, last = people[k % len(people)]
        who = f"{first} {last}"
        return {"text": tpl.format(who=who),
                "facts": [{"label": "name", "any": ["w:" + first.lower(), "w:" + last.lower()],
                           "say": tpl.format(who=who)}]}
    raise ValueError(kind)


def _talk(rnd: random.Random, n_lines: int, n_facts: int, n_decoys: int, near: bool,
          variant: str) -> dict:
    key = rnd.choice(sorted(_TALK))
    fam = _TALK[key]
    words = rnd.sample(fam["words"], n_decoys + 1 if near else 2)
    topics = [(fam, w) for w in words]
    while len(topics) < n_decoys + 1:
        okey = rnd.choice([k for k in sorted(_TALK) if k != key])
        w = rnd.choice(_TALK[okey]["words"])
        if all(t[1] != w for t in topics):
            topics.append((_TALK[okey], w))
    pools = _Pools(rnd)
    blocks = []
    for ti, (tf, w) in enumerate(topics):
        people = [pools.person() for _ in range(3)]
        mine = ti == 0
        if mine and variant == "quiet":
            t = pools.clock()
            first, last = people[0]
            ttext = tf["time"][0].format(t=t)
            sents = [{"text": f"nothing new on that: {ttext} as planned, and {first} {last} "
                              f"still has it",
                      "facts": [{"label": "time", "any": ["t:" + t], "say": ttext},
                                {"label": "name", "any": ["w:" + first.lower(),
                                                          "w:" + last.lower()],
                                 "say": f"{first} {last} has it"}]}]
        else:
            nf = n_facts if mine else (DECOY_FACTS if variant == "quiet"
                                       else max(3, min(n_facts, DECOY_FACTS)))
            if mine and variant == "aside":
                kinds = ["time", "count", "owner", "ref"]
            else:
                kinds = _cycle(_TALK_KINDS, nf, 0 if mine else rnd.randrange(len(_TALK_KINDS)))
            seen: dict = {}
            sents = [_talk_fact(pools, tf, k, seen, people) for k in kinds]
        blocks.append({"topic": tf["topic"].format(w=w), "people": people, "mine": mine,
                       "sents": sents})
    aside = None
    if variant == "aside":
        aside = next(s for s in blocks[0]["sents"] if s["facts"][0]["label"] == "reference")
        blocks[0]["sents"].remove(aside)
    fix = None
    if variant == "correction":
        orig = next(s for s in blocks[0]["sents"] if s.get("tpl"))
        n_new = pools.number(13, 990)
        orig["facts"] = []
        fix = {"text": "sorry, I got that wrong earlier: " + orig["tpl"].format(n=_fmt(n_new))
                       + f", not {_fmt(orig['n'])}",
               "facts": [{"label": "corrected number", "any": [f"n:{n_new}"],
                          "say": orig["tpl"].format(n=_fmt(n_new))}], "orig": orig}
    order = list(range(len(blocks)))
    rnd.shuffle(order)
    if variant == "aside" and order[-1] == 0:
        order = [order[-1]] + order[:-1]        # the aside comes in a later item
    per = max(4, (n_lines - 2) // len(blocks))
    lines, owner, facts, theirs_say = [], [], [], []

    def say(speaker, text, is_mine, carried):
        end = "" if text.endswith(("?", "!")) else "."
        lines.append(f"{speaker}: {_cap1(text)}{end}")
        owner.append(is_mine)
        for f in carried:
            (facts if is_mine else theirs_say).append(f)

    say(CHAIR, "morning, everyone. Let's go through the list", True, [])
    for pos, bi in enumerate(order):
        b = blocks[bi]
        say(CHAIR, ("first" if pos == 0 else "next") + ", " + b["topic"], b["mine"], [])
        body = list(b["sents"])
        n_body = max(per - 1, len(body) + (1 if fix and b["mine"] else 0) + 1)
        slots = sorted(rnd.sample(range(n_body), len(body)))
        fix_at = None
        if fix and b["mine"]:
            at = slots[body.index(fix["orig"])]
            after = [i for i in range(n_body) if i > at and i not in slots]
            fix_at = after[len(after) // 2] if after else None
        aside_at = None
        if aside is not None and pos == len(order) - 1 and not b["mine"]:
            free = [i for i in range(n_body) if i not in slots]
            aside_at = free[len(free) // 2] if free else None
        it = iter(body)
        for i in range(n_body):
            spk = b["people"][i % len(b["people"])][0]
            if i in slots:
                s = next(it)
                say(spk, s["text"], b["mine"], s["facts"])
            elif i == fix_at:
                say(spk, fix["text"], True, fix["facts"])
                fix = None
            elif i == aside_at:
                say(CHAIR, f"oh, before I forget, on {blocks[0]['topic']}: " + aside["text"],
                    True, aside["facts"])
                aside = None
            else:
                chat = rnd.choice([c for c in _CHAT if not lines[-1].endswith(" " + c)])
                say(spk, chat.rstrip("."), b["mine"], [])
        if fix and b["mine"]:
            say(b["people"][0][0], fix["text"], True, fix["facts"])
            fix = None
    if aside is not None:
        say(CHAIR, f"oh, before I forget, on {blocks[0]['topic']}: " + aside["text"], True,
            aside["facts"])
    say(CHAIR, "that's everything. Thanks, all", True, [])
    subject = blocks[0]["topic"]
    header = ("Transcript of the Thursday planning call. Each line is the speaker's first "
              "name and what they said.")
    cap = word_cap(len(facts))
    ask = (f"Summarise what was said about {subject} on this call for someone who missed it, "
           f"in at most {cap} words. Keep every time, number, amount, name, place and "
           f"reference that matters to it. Leave out everything about the other items, even "
           f"ones that sound alike. Use only what was said: give figures exactly as they were "
           f"said and do not work out new ones.")
    names = [CHAIR] + [w for p in pools.used_people for w in p]
    return {"subject": subject, "others": [b["topic"] for b in blocks[1:]], "header": header,
            "lines": lines, "owner": owner, "facts": facts, "decoy_facts": theirs_say,
            "ask": ask, "cap": cap, "names": names, "places": list(pools.used_places),
            "codes": []}


# --------------------------------------------------------------------------- #
# Cases                                                                        #
# --------------------------------------------------------------------------- #

def _finish(sp: dict, kind: str, ladder: str, level: int, variant: str, seed: int) -> dict:
    names, places, codes = sp["names"], sp["places"], sp["codes"]
    mine = set(tokens(sp["header"] + " 18 November 2026", names, places, codes))
    theirs: set = set()
    for line, is_mine in zip(sp["lines"], sp["owner"]):
        (mine if is_mine else theirs).update(tokens(line, names, places, codes))
    theirs -= mine
    source = sp["header"] + "\n\n" + "\n".join(sp["lines"])
    ideal = sp["subject"] + ": " + "; ".join(f["say"] for f in sp["facts"]) + "."
    slip_from = next((f for f in sp["decoy_facts"]
                      if any(t in theirs for t in f["any"])), None)
    slip = ideal + (" Also " + slip_from["say"] + "." if slip_from else "")
    name = variant or f"{ladder}-{level}"
    return {"task": f"{kind}-{name}" + (f"-{seed}" if variant else ""), "kind": kind,
            "ladder": ladder, "level": level, "variant": variant, "seed": seed,
            "subject": sp["subject"], "others": sp["others"],
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": source + "\n\n" + sp["ask"]}],
            "facts": [{"label": f["label"], "any": list(f["any"])} for f in sp["facts"]],
            "mine": sorted(mine), "theirs": sorted(theirs), "names": names,
            "places": places, "codes": codes, "cap": sp["cap"], "lines": len(sp["lines"]),
            "decoys": len(sp["others"]), "ideal": ideal, "slip": slip}


def summ_spec(kind: str, ladder: str, level: int, seed: int = 0) -> dict:
    """One rung of a ladder: `length` raises the lines in the source, `facts`
    the facts about the subject, `near` the look-alikes beside it."""
    lv = max(1, min(SUMM_MAX, int(level)))
    lines = (LENGTHS[lv - 1] if ladder == "length"
             else NEAR_LINES if ladder == "near" else BASE_LINES)
    facts = FACT_COUNTS[lv - 1] if ladder == "facts" else BASE_FACTS
    decoys = NEAR_COUNTS[lv - 1] if ladder == "near" else BASE_DECOYS
    rnd = random.Random(f"summ|{kind}|{ladder}|{lv}|{seed}")
    make = _log if kind == "log" else _talk
    return _finish(make(rnd, lines, facts, decoys, ladder == "near", ""), kind, ladder, lv,
                   "", seed)


def single_spec(kind: str, variant: str, seed: int = 0) -> dict:
    """One case of a kind that is not a size: a figure corrected later, a
    subject with almost nothing to say, every fact at the very end, a fact
    said in another item."""
    rnd = random.Random(f"summ|{kind}|{variant}|{seed}")
    make = _log if kind == "log" else _talk
    return _finish(make(rnd, SINGLE_LINES, BASE_FACTS, BASE_DECOYS, False, variant), kind,
                   "", 0, variant, seed)


def summ_specs(top_level: int = 0) -> list:
    """Every case of the suite: each ladder up to `top_level` for both kinds of
    source, then the single cases."""
    top = max(2, min(SUMM_MAX, int(top_level or SUMM_TOP)))
    out = []
    for kind in KINDS:
        for ladder in LADDERS:
            for level in range(1, top + 1):
                sp = summ_spec(kind, ladder, level)
                sp["ladder"] = f"summary:{kind} {ladder}"
                out.append(sp)
    for kind in KINDS:
        for variant in SINGLES[kind]:
            for seed in range(SINGLE_SEEDS):
                sp = single_spec(kind, variant, seed)
                sp["ladder"] = ""
                out.append(sp)
    return out
