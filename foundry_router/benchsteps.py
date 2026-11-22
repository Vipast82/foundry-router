"""Tool work that takes more than one call (v0.181.241).

Every media, video and document tool on this router queues a job: the first
call hands back a job id, and the link exists only after the job has been
waited for, checked and collected. The finder suite stops at the first right
call, so nothing measured the rest.

A case here is a task and a scripted tool environment (StepEnv): a small state
machine with a clock. Nothing real is ever run. Each tool answers from the
state the calls before it left - a job that is still running, a queue that
refuses once, a file that moved, a job that failed and can be queued again,
one that cannot succeed. Every output id and link is derived from the exact
tool, input and setting that produced it, so the link at the end is right only
when every step before it was the right call with the right arguments.

Scored on that link (or FAILED, where the work could not be done), and on
doing it cleanly: no job queued twice, none the task did not ask for, inside a
cap on calls. The dial is the number of steps.
"""
from __future__ import annotations

import hashlib
import json
import random
import re

TICK = 1                 # seconds of the environment's clock one call takes
MAX_WAIT = 3600
BASE = "https://media.local/out/"
FAILED = "FAILED"
CAP_SLACK = 6            # cap on calls = 2 x the reference run + this

STEPS_SYSTEM = ("You get media and document work done with tools. Nothing is done until a "
                "tool has done it: ids and links come only from tool results.")
STEPS_RULE = ("When all of it is finished, end your reply with one line of the form "
              "`ANSWER: <link>, <link>, ...` - one entry per file, in the order the files "
              "are listed, with FAILED in place of the link for a file that could not be "
              "finished. Only that line is read.")

_LANGS = ("de", "fr", "es", "it", "nl", "pl", "sv", "pt")
# name: (what it does, setting, what the setting is, values, extension)
STAGE_TOOLS = {
    "transcribe_audio": ("Write out the speech in an audio or video file as text.",
                         "language", "Two-letter code of the language spoken.", _LANGS, "txt"),
    "translate_text": ("Translate a text.", "target_language",
                       "Two-letter code of the language to translate into.", _LANGS, "txt"),
    "make_captions": ("Turn a transcript into a caption file.", "format",
                      "Caption format: srt, vtt or ass.", ("srt", "vtt", "ass"), "cap"),
    "denoise_audio": ("Remove background noise from a recording.", "strength",
                      "From 1 (light) to 9 (heavy).", (2, 3, 4, 5, 6, 7, 8), "wav"),
    "normalize_audio": ("Bring a recording to a target loudness.", "target_lufs",
                        "Target loudness in LUFS, for example -16.", (-14, -16, -18, -23), "wav"),
    "trim_media": ("Cut a file down to its first seconds.", "seconds",
                   "How many seconds to keep.", (15, 20, 30, 45, 60, 90), "mp4"),
    "upscale_video": ("Upscale a video.", "height", "Output height in pixels.",
                      (720, 1080, 1440, 2160), "mp4"),
    "stabilize_video": ("Steady a shaky video.", "mode", "light, standard or strong.",
                        ("light", "standard", "strong"), "mp4"),
    "encode_video": ("Re-encode a video.", "codec", "h264, h265, av1 or vp9.",
                     ("h264", "h265", "av1", "vp9"), "mp4"),
    "change_speed": ("Speed a video up or slow it down.", "percent",
                     "Playback speed in percent; 100 leaves it unchanged.",
                     (80, 90, 110, 125, 150), "mp4"),
    "add_watermark": ("Stamp the house watermark on a video.", "position",
                      "top_left, top_right, bottom_left, bottom_right or center.",
                      ("top_left", "top_right", "bottom_left", "bottom_right", "center"), "mp4"),
    "crop_video": ("Crop a video to an aspect ratio.", "aspect", "For example 16:9.",
                   ("16:9", "9:16", "4:3", "1:1"), "mp4"),
    "adjust_color": ("Apply a colour preset to a video.", "preset",
                     "neutral, warm, cool, vivid or mono.",
                     ("neutral", "warm", "cool", "vivid", "mono"), "mp4"),
}
# the order steps make sense in, and which ones a short chain keeps
_CHAIN_ORDER = ("stabilize_video", "crop_video", "adjust_color", "upscale_video", "encode_video",
                "trim_media", "denoise_audio", "normalize_audio", "transcribe_audio",
                "translate_text", "make_captions")
_CHAIN_KEEP = ("transcribe_audio", "translate_text", "make_captions", "denoise_audio",
               "normalize_audio", "trim_media", "stabilize_video", "crop_video",
               "adjust_color", "upscale_video", "encode_video")
_VIDEO_OPS = ("trim_media", "upscale_video", "stabilize_video", "encode_video", "change_speed",
              "add_watermark", "crop_video", "adjust_color", "denoise_audio", "normalize_audio")
_JOB_TOOLS = {
    "job_status": ("The state of a queued job: running (with eta_seconds, an estimate that "
                   "can change), done, or failed (with the reason).",
                   {"job_id": {"type": "string", "description": "The job_id a tool returned."}}),
    "job_result": ("The output of a finished job: its output_id, to pass to a next step, and "
                   "its url. An error while the job is still running.",
                   {"job_id": {"type": "string", "description": "The job_id a tool returned."}}),
    "wait": ("Pause for a number of seconds (at most 3600) while queued jobs run. Asking for "
             "a job's status again does not make it finish sooner.",
             {"seconds": {"type": "integer", "description": "How long to pause."}}),
}
FAULTS = ("moved", "gone", "busy", "lost", "broken")
_LOST = ("the worker was lost while the job ran and nothing was produced. "
         "It is safe to queue the job again.")
# worded so that redoing the step before it is plainly no way out: Sonnet 5.5 ran a
# finished transcription a second time when this said only that the input could
# not be decoded
_BROKEN = ("this tool cannot process this content, however it was produced. Queueing the "
           "job again, or redoing earlier steps, will fail the same way.")
_BUSY = "the job queue is restarting and nothing was queued. Call this again."

WHY_WORDS = {
    "ran_past_the_call_cap": "used up the cap on calls",
    "ran_out_of_time": "ran out of time before it finished",
    "ended_a_turn_with_nothing": "ended its turn with nothing: no call and no answer",
    "stopped_with_a_job_unfinished": "stopped while a job was still unfinished",
    "no_links_handed_over": "handed over no link",
    "wrong_number_of_entries": "did not give one entry per file",
    "said_failed_for_work_that_could_be_finished": "said FAILED for work it could have finished",
    "handed_over_a_link_no_tool_returned": "handed over a link no tool returned",
    "handed_over_a_link_for_a_file_that_failed": "handed over a link for a file that failed",
    "handed_over_the_wrong_output": "handed over the wrong output (an earlier step, or wrong "
                                    "settings)",
    "queued_twice": "queued the same job twice",
    "requeued_a_job_that_cannot_succeed": "queued a job again that could not succeed",
    "unasked_job": "ran a job the task did not ask for",
}


def _h(*parts, n: int = 6) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


def norm_value(v) -> str:
    """A setting as the tools compare it: 7, 7.0 and " 7 " are one value."""
    if isinstance(v, bool):
        return str(v).lower()
    s = str(v).strip().lower()
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


class StepEnv:
    """The scripted tools of one case. Deterministic: the same calls give the
    same answers, and no call does anything outside this object."""

    def __init__(self, spec: dict):
        self.seed = spec.get("seed", 0)
        self.tools = dict(spec.get("tools") or {})     # name -> {param, ext, queued}
        self.files = set(spec.get("files") or [])
        self.moved = dict(spec.get("moved") or {})
        self.gone = set(spec.get("gone") or [])
        self.faults = dict(spec.get("faults") or {})   # job key -> {kind, phases?}
        self.wanted = set(spec.get("wanted") or [])
        self.cap = int(spec.get("cap") or 0)
        self.clock = 0
        self.n = 0
        self.jobs: dict = {}
        self.by_key: dict = {}
        self.outputs: dict = {}
        self.refused: set = set()
        self.flags: list = []
        self.shown: list = []
        self.got: set = set()
        self.log: list = []

    # -- the tools ---------------------------------------------------------
    def call(self, name, args) -> str:
        """One tool call; the JSON text the model is shown."""
        self.n += 1
        self.clock += TICK
        args = args if isinstance(args, dict) else {}
        name = str(name or "")
        if name == "wait":
            out = self._wait(args)
        elif name == "job_status":
            out = self._status(args)
        elif name == "job_result":
            out = self._result(args)
        elif name in self.tools:
            out = self._stage(name, args)
        else:
            out = {"error": f"there is no tool called '{name}'."}
        self.log.append([name, ", ".join(str(v) for v in args.values())[:60],
                         "error" if "error" in out else str(out.get("status") or "ok")])
        return json.dumps(out)

    def _stage(self, name: str, args: dict) -> dict:
        t = self.tools[name]
        src = str(args.get("input_id") if args.get("input_id") is not None else "").strip()
        if not src:
            return {"error": "missing required argument 'input_id'."}
        if args.get(t["param"]) is None:
            return {"error": f"missing required argument '{t['param']}'."}
        if src in self.moved:
            return {"error": f"{src} was moved to the archive. "
                             f"Use input_id '{self.moved[src]}'."}
        if src in self.gone:
            return {"error": f"no file called '{src}' exists in this workspace or its archive."}
        if src in self.files:
            inkey = src
        elif src in self.outputs:
            inkey = self.outputs[src]["key"]
        elif src in self.jobs:
            return {"error": f"'{src}' is a job id, not an output. Use the output_id that "
                             f"job_result returns for it."}
        else:
            return {"error": f"no file or output called '{src}'."}
        key = f"{name}({inkey};{t['param']}={norm_value(args[t['param']])})"
        if not t.get("queued"):
            return self._output(key, t["ext"])
        fault = self.faults.get(key) or {}
        if fault.get("kind") == "busy" and key not in self.refused:
            self.refused.add(key)
            return {"error": _BUSY}
        prior = self.by_key.setdefault(key, [])
        if any(self.jobs[j]["seen"] == "broken" for j in prior):
            self.flags.append("requeued_a_job_that_cannot_succeed")
        elif not all(self.jobs[j]["seen"] == "lost" for j in prior):
            self.flags.append("queued_twice")
        if key not in self.wanted:
            self.flags.append("unasked_job")
        run = len(prior)
        jid = "job-" + _h(self.seed, key, run, n=5)
        while jid in self.jobs:
            jid = "job-" + _h(jid, n=5)
        phases = list(fault.get("phases") or []) if fault.get("kind") == "slow" else []
        self.jobs[jid] = {
            "key": key, "t0": self.clock, "ext": t["ext"], "seen": "",
            "phases": phases or [20 + int(_h(self.seed, key, "d", n=4), 16) % 5 * 10],
            "fails": ("lost" if fault.get("kind") == "lost" and run == 0
                      else "broken" if fault.get("kind") == "broken" else "")}
        prior.append(jid)
        return {"job_id": jid, "status": "queued"}

    def _output(self, key: str, ext: str) -> dict:
        oid = "out-" + _h(self.seed, key, n=6)
        url = BASE + _h(self.seed, key, "url", n=8) + "." + ext
        self.outputs[oid] = {"key": key, "url": url}
        if url not in self.shown:
            self.shown.append(url)
        if key in self.wanted:
            self.got.add(key)
        return {"output_id": oid, "url": url}

    def _state(self, j: dict) -> tuple:
        elapsed, end = self.clock - j["t0"], 0
        for p in j["phases"]:
            end += int(p)
            if elapsed < end:
                return "running", end - elapsed
        return ("failed", 0) if j["fails"] else ("done", 0)

    def _job(self, args: dict):
        jid = str(args.get("job_id") if args.get("job_id") is not None else "").strip()
        if not jid:
            return None, {"error": "missing required argument 'job_id'."}
        if jid not in self.jobs:
            return None, {"error": f"there is no job called '{jid}'."}
        return jid, None

    def _failed(self, jid: str) -> str:
        j = self.jobs[jid]
        j["seen"] = j["fails"]
        if j["fails"] == "broken" and j["key"] in self.wanted:
            self.got.add(j["key"])           # the step was taken as far as it goes
        return _LOST if j["fails"] == "lost" else _BROKEN

    def _status(self, args: dict) -> dict:
        jid, err = self._job(args)
        if err:
            return err
        state, eta = self._state(self.jobs[jid])
        if state == "running":
            return {"job_id": jid, "status": "running", "eta_seconds": eta}
        if state == "failed":
            return {"job_id": jid, "status": "failed", "error": self._failed(jid)}
        return {"job_id": jid, "status": "done"}

    def _result(self, args: dict) -> dict:
        jid, err = self._job(args)
        if err:
            return err
        j = self.jobs[jid]
        state, eta = self._state(j)
        if state == "running":
            return {"error": f"job {jid} is still running (eta_seconds {eta})."}
        if state == "failed":
            return {"error": f"job {jid} failed: {self._failed(jid)}"}
        return {"job_id": jid, **self._output(j["key"], j["ext"])}

    def _wait(self, args: dict) -> dict:
        try:
            s = int(float(str(args.get("seconds"))))
        except (TypeError, ValueError, OverflowError):
            s = 0
        if s < 1 or s > MAX_WAIT:
            return {"error": f"seconds must be a whole number from 1 to {MAX_WAIT}."}
        self.clock += s
        return {"waited_seconds": s}

    # -- what happened -----------------------------------------------------
    def report(self) -> dict:
        unfinished = [jid for jid, j in self.jobs.items()
                      if j["key"] in self.wanted and j["key"] not in self.got and not j["seen"]]
        return {"calls": self.n, "cap": self.cap, "flags": sorted(set(self.flags)),
                "shown": list(self.shown), "reached": len(self.got),
                "steps": len(self.wanted), "open": len(unfinished), "log": list(self.log)}


def reference_calls(plans: list):
    """The plans done the plain way, as a generator: it yields one call
    (name, arguments), is sent that call's result, and returns the entry for
    each file. Queue what is ready, ask after each job once, wait out the
    longest, collect; call again where a tool says to, stop where it says
    there is no point."""
    st = [{"src": p["ask"], "i": 0, "job": None} for p in plans]
    answers: list = [None] * len(plans)
    for _guard in range(100000):
        if all(a is not None for a in answers):
            break
        for k, p in enumerate(plans):
            s = st[k]
            while answers[k] is None and s["job"] is None:
                tool, param, value = p["steps"][s["i"]]
                r = yield tool, {"input_id": s["src"], param: value}
                if "error" in r:
                    m = re.search(r"Use input_id '([^']+)'", r["error"])
                    if m:
                        s["src"] = m.group(1)
                    elif "Call this again" not in r["error"]:
                        answers[k] = FAILED
                    continue
                if "job_id" in r:
                    s["job"] = r["job_id"]
                    break
                s["src"], s["i"] = r["output_id"], s["i"] + 1
                if s["i"] == len(p["steps"]):
                    answers[k] = r["url"]
        etas = []
        for k, s in enumerate(st):
            if s["job"] is None or answers[k] is not None:
                continue
            r = yield "job_status", {"job_id": s["job"]}
            if r["status"] == "running":
                etas.append(r["eta_seconds"])
                continue
            if r["status"] == "failed":
                if "safe to queue" not in r["error"]:
                    answers[k] = FAILED
                s["job"] = None
                continue
            r = yield "job_result", {"job_id": s["job"]}
            s["job"], s["src"], s["i"] = None, r["output_id"], s["i"] + 1
            if s["i"] == len(p["steps"]):
                answers[k] = r["url"]
        if etas:
            yield "wait", {"seconds": max(etas)}
    else:                                                   # pragma: no cover
        raise ValueError("the reference run did not finish")
    return answers


def solve(spec: dict, plans: list) -> tuple:
    """The reference run of a case, through the tools themselves. Returns (the
    entry for each file, the environment it ran in)."""
    env = StepEnv(spec)
    gen = reference_calls(plans)
    try:
        call = next(gen)
        while True:
            call = gen.send(json.loads(env.call(*call)))
    except StopIteration as done:
        return done.value, env


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #

_ENTRY = re.compile(r"https?://[^\s<>()\[\]\"'`,]+|\bFAILED\b")
_ANSWER = re.compile(r"(?i)answer\s*:")


def answer_entries(text: str):
    """The entries after the last `ANSWER:` - links and FAILED, in order - or
    None when the reply has no such line."""
    marks = list(_ANSWER.finditer(text or ""))
    if not marks:
        return None
    return [e.rstrip(".;:*_") for e in _ENTRY.findall(text[marks[-1].end():])]


def steps_score(text: str, rep: dict, want: list) -> dict:
    """{score, result, clean, why, detail} for one run of a case.

    result: the reply hands over exactly the entry for each file, in order.
    clean: no job queued twice, none queued again that could not succeed, none
    the task did not ask for. 100 for both, 50 for the right result reached
    untidily - the second video was still rendered - and 0 without the result."""
    rep = rep or {}
    want = list(want or [])
    cut = rep.get("capped") or rep.get("timed_out")
    got = None if cut else answer_entries(text)
    result = got is not None and got == want
    why: list = []
    if not result:
        if rep.get("capped"):
            why.append("ran_past_the_call_cap")
        elif rep.get("timed_out"):
            why.append("ran_out_of_time")
        elif not str(text or "").strip():
            # thought about the next step and then said and did nothing:
            # qwen3.8-flash on 6 of 21 runs with three files or more
            why.append("ended_a_turn_with_nothing")
        elif not got:
            why.append("stopped_with_a_job_unfinished" if rep.get("open")
                       else "no_links_handed_over")
        elif len(got) != len(want):
            why.append("wrong_number_of_entries")
        else:
            shown = set(rep.get("shown") or [])
            for g, w in zip(got, want):
                if g == w:
                    continue
                k = ("said_failed_for_work_that_could_be_finished" if g == FAILED
                     else "handed_over_a_link_no_tool_returned" if g not in shown
                     else "handed_over_a_link_for_a_file_that_failed" if w == FAILED
                     else "handed_over_the_wrong_output")
                if k not in why:
                    why.append(k)
    flags = list(rep.get("flags") or [])
    clean = not flags
    why += flags
    score = 100.0 if result and clean else 50.0 if result else 0.0
    detail = (f"steps={rep.get('reached', 0)}/{rep.get('steps', 0)} "
              f"calls={rep.get('calls', 0)}/{rep.get('cap', 0)} "
              f"result={int(result)} clean={int(clean)}"
              + (" why=" + ";".join(why) if why else ""))
    return {"score": score, "result": 1.0 if result else 0.0, "clean": 1.0 if clean else 0.0,
            "why": why, "detail": detail}


def wall_seconds(cap: int) -> int:
    """How long one case may take, all its calls together."""
    return int(min(1800, max(300, 20 * int(cap or 0))))


# --------------------------------------------------------------------------- #
# Cases                                                                       #
# --------------------------------------------------------------------------- #

STEP_KINDS = ("handoff", "wait", "pipeline", "recover", "batch")
STEPS_TOP = 6             # the level each ladder is built to unless asked for more
STEPS_MAX = 10
_SIZES = (1, 2, 3, 5, 8, 12, 18, 27, 40, 60)          # files, or times a job runs on
_PIPE = (3, 5, 8, 12, 18, 27, 40, 60, 90, 135)        # steps of one pipeline
_BATCH = ((2, 2), (3, 2), (3, 3), (4, 4), (6, 4), (8, 5), (10, 6), (12, 8), (16, 8), (20, 10))
_RECOVER = ((2, 1), (3, 1), (4, 1), (5, 2), (6, 2), (8, 2), (12, 2), (18, 3), (27, 3), (40, 3))
LADDER_NAMES = {"handoff": "hand-over", "wait": "long-wait", "pipeline": "pipeline",
                "recover": "recover", "batch": "batch"}
# No tier is written on a case. At the sizes built by default nothing here is
# hard for Sonnet 5.5 or Opus 5.5, and the rungs above were not measured, so a
# tier on them would be a guess: the card tiers each case from what the models
# run on it did (benchcard.measured_tiers).
FAULT_SEEDS = (0, 1)


def chain(steps: int) -> list:
    """The tools of a queued chain of this many steps, in the order they run."""
    keep = set(_CHAIN_KEEP[:max(1, min(len(_CHAIN_KEEP), int(steps)))])
    return [t for t in _CHAIN_ORDER if t in keep]


def _files(rnd: random.Random, n: int) -> list:
    out: list = []
    while len(out) < n:
        f = (f"{rnd.choice(('clip', 'talk', 'interview', 'demo', 'lesson', 'promo', 'briefing'))}"
             f"_{rnd.randint(1000, 9899)}.{rnd.choice(('mov', 'wav', 'mp4'))}")
        if f.split(".")[0] not in {x.split(".")[0] for x in out}:
            out.append(f)
    return out


def _plan(rnd: random.Random, name: str, tools: list) -> dict:
    steps: list = []
    for t in tools:
        value = rnd.choice(STAGE_TOOLS[t][3])
        # not a translation into the language it was spoken in
        while t == "translate_text" and any(s[0] == "transcribe_audio" and s[2] == value
                                            for s in steps):
            value = rnd.choice(STAGE_TOOLS[t][3])
        steps.append([t, STAGE_TOOLS[t][1], value])
    return {"ask": name, "steps": steps}


def _key_at(plan: dict, real: str, upto: int) -> str:
    """The key of the job that step `upto` (0-based) of a plan queues."""
    key = real
    for tool, param, value in plan["steps"][:upto + 1]:
        key = f"{tool}({key};{param}={norm_value(value)})"
    return key


def step_tools(names, queued: bool) -> list:
    """The tool definitions a case offers, as a chat request carries them."""
    out = []
    for n in names:
        what, param, pdesc, values, _ext = STAGE_TOOLS[n]
        does = (" Queues a job and returns its job_id; the result is ready once the job is "
                "done. Every call queues a new job." if queued
                else " Returns the output_id and the url of the result.")
        out.append({"name": n, "description": what + does, "parameters": {
            "type": "object", "required": ["input_id", param], "properties": {
                "input_id": {"type": "string",
                             "description": "A file name, or the output_id of an earlier step."},
                param: {"type": "integer" if isinstance(values[0], int) else "string",
                        "description": pdesc}}}})
    if queued:
        for n, (desc, props) in _JOB_TOOLS.items():
            out.append({"name": n, "description": desc, "parameters": {
                "type": "object", "required": list(props), "properties": props}})
    return out


def task_text(plans: list) -> str:
    lines = ["Each file below goes through its steps in the order given; every step takes "
             "the output of the step before it. Hand over the link (url) of the last step's "
             "output for each file.", ""]
    for i, p in enumerate(plans, start=1):
        lines.append(f"File {i}: {p['ask']}")
        for j, (tool, param, value) in enumerate(p["steps"], start=1):
            lines.append(f"  {j}. {tool}, {param} = {value}")
    return "\n".join(lines + ["", STEPS_RULE])


def _assemble(kind: str, level: int, seed: int, plans: list, queued: bool,
              faults: list, fault: str = "") -> dict:
    """A case from its plans and the faults planted in it: [(file index, step
    index, kind, phases?)]. The reference run fixes what is wanted, how many
    calls that takes and the cap."""
    names = [p["ask"] for p in plans]
    moved, gone, planted, real = {}, [], {}, list(names)
    for f in faults:
        if f[2] == "moved":
            real[f[0]] = "archive/" + names[f[0]]
            moved[names[f[0]]] = real[f[0]]
        elif f[2] == "gone":
            real[f[0]] = ""
            gone.append(names[f[0]])
    for f in faults:
        if f[2] in ("busy", "lost", "broken", "slow") and real[f[0]]:
            planted[_key_at(plans[f[0]], real[f[0]], f[1])] = (
                {"kind": "slow", "phases": list(f[3])} if f[2] == "slow" else {"kind": f[2]})
    used: list = []
    for p in plans:
        for s in p["steps"]:
            if s[0] not in used:
                used.append(s[0])
    env = {"seed": f"{kind}|{level}|{seed}|{fault}",
           "tools": {n: {"param": STAGE_TOOLS[n][1], "ext": STAGE_TOOLS[n][4], "queued": queued}
                     for n in used},
           "files": [r for r in real if r], "moved": moved, "gone": gone, "faults": planted}
    want, ref = solve(env, plans)
    env["wanted"] = sorted(set(ref.by_key) | {o["key"] for o in ref.outputs.values()})
    env["cap"] = 2 * ref.n + CAP_SLACK
    return {"kind": kind, "level": level, "seed": seed, "fault": fault,
            "task": f"{kind}-L{level}-{seed}" + (f"-{fault}" if fault else ""),
            "messages": [{"role": "system", "content": STEPS_SYSTEM},
                         {"role": "user", "content": task_text(plans)}],
            "tools": step_tools(used, queued), "env": env, "want": want, "plans": plans,
            "needed": ref.n, "cap": env["cap"], "files": len(plans),
            "steps": len(env["wanted"])}


def steps_spec(kind: str, level: int, seed: int = 0) -> dict:
    """One case on a ladder. The dial is the number of steps: more files to
    hand over, a job that runs on more times, a longer pipeline, more files
    with something wrong, more files through more queued steps."""
    level = max(1, min(STEPS_MAX, int(level)))
    rnd = random.Random(f"steps|{kind}|{level}|{seed}")
    if kind == "handoff":
        tool = rnd.choice(("transcribe_audio", "denoise_audio", "upscale_video",
                           "stabilize_video", "encode_video"))
        plans = [_plan(rnd, f, [tool]) for f in _files(rnd, _SIZES[level - 1])]
        return _assemble(kind, level, seed, plans, True, [])
    if kind == "wait":
        tool = rnd.choice(("upscale_video", "encode_video", "transcribe_audio"))
        plans = [_plan(rnd, _files(rnd, 1)[0], [tool])]
        phases = [rnd.choice((20, 30, 40, 60, 90)) for _ in range(_SIZES[level - 1] + 1)]
        return _assemble(kind, level, seed, plans, True, [(0, 0, "slow", phases)])
    if kind == "pipeline":
        tools: list = []
        while len(tools) < _PIPE[level - 1]:
            t = rnd.choice(_VIDEO_OPS)
            if not tools or tools[-1] != t:
                tools.append(t)
        return _assemble(kind, level, seed, [_plan(rnd, _files(rnd, 1)[0], tools)], False, [])
    if kind == "recover":
        n, steps = _RECOVER[level - 1]
        plans = [_plan(rnd, f, chain(steps)) for f in _files(rnd, n)]
        # about one file in four is left alone; the rest take the faults in turn
        clean = max(1, n // 4)
        order = list(FAULTS)
        rnd.shuffle(order)
        who = list(range(n))
        rnd.shuffle(who)
        faults = [(i, rnd.randrange(steps), order[k % len(order)])
                  for k, i in enumerate(who[:n - clean])]
        return _assemble(kind, level, seed, plans, True, faults)
    if kind == "batch":
        n, steps = _BATCH[level - 1]
        plans = [_plan(rnd, f, chain(steps)) for f in _files(rnd, n)]
        count = (0, 1, 1, 2, 3, 4, 5, 6, 8, 10)[level - 1]
        who = list(range(n))
        rnd.shuffle(who)
        faults = []
        for i in who[:count]:
            k = rnd.choice(FAULTS + ("slow",))
            faults.append((i, rnd.randrange(steps), k, [rnd.choice((30, 40, 60)), 30, 20])
                          if k == "slow" else (i, rnd.randrange(steps), k))
        return _assemble(kind, level, seed, plans, True, faults)
    raise ValueError(f"unknown kind of steps case: {kind!r}")


def fault_spec(fault: str, seed: int = 0) -> dict:
    """One file, one step, one thing wrong: the five ways a call or a job
    fails here, each on its own so a miss names the failure."""
    if fault not in FAULTS:
        raise ValueError(f"unknown fault: {fault!r}")
    rnd = random.Random(f"steps|fault|{fault}|{seed}")
    tool = rnd.choice(("transcribe_audio", "denoise_audio", "upscale_video", "encode_video"))
    plans = [_plan(rnd, _files(rnd, 1)[0], [tool])]
    return _assemble("fault", 1, seed, plans, True, [(0, 0, fault)], fault)


def steps_specs(top_level: int = 0) -> list:
    """Every case of the suite: each ladder up to `top_level`, then the single
    faults."""
    top = max(2, min(STEPS_MAX, int(top_level or STEPS_TOP)))
    out = []
    for kind in STEP_KINDS:
        for level in range(1, top + 1):
            sp = steps_spec(kind, level, 0)
            sp["ladder"] = "steps:" + LADDER_NAMES[kind]
            out.append(sp)
    for fault in FAULTS:
        for seed in FAULT_SEEDS:
            sp = fault_spec(fault, seed)
            sp["ladder"] = ""
            out.append(sp)
    return out
