"""Move the 11 queued AU commits of the retired bash watcher (au_safe_point.sh) into the
coordinator's prep queue (self-driving part 3, foundry_coordinator/prep_queue.py).

Dry run by default: prints the `prep add` commands. `--apply` runs them against the
coordinator store (the running coordinator then lands them at its next safe point,
after GF-GATE1 is COMPLETE, in this order, all or nothing, and pushes once).

    python tools/migrate_au_watcher.py            # show what would be queued
    python tools/migrate_au_watcher.py --apply    # queue it (run from foundry-deploy)

After --apply: stop the bash watcher; `prep list animaluprising` shows the entries,
Automation -> Repo changes too.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

CO = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
SCRATCH = r"C:\Roblox\_scratch"
SCRATCHPAD = (r"C:\Users\shock\AppData\Local\Temp\claude"
              r"\C--Users-shock-OneDrive-Documents-ClaudeCode-Foundry-Router"
              r"\2ada3eb0-6be1-45ea-947b-2aff521abc81\scratchpad")
PROJECT = "animaluprising"
AFTER_TASK = "GF-GATE1"


def _p(name: str) -> str:
    return os.path.join(SCRATCH, name)


# (kind, source-or-command, paths, subject, body paragraphs) in the watcher's order
ENTRIES = [
    ("script", f'"{{python}}" "{os.path.join(SCRATCHPAD, "au_env_amend.py")}"',
     ["docs/ENVIRONMENT.md"],
     "prep: ENVIRONMENT.md: Studio auto-reopen under the coordinator (owner-approved 2026-10-27)",
     []),
    ("patch", _p("lune-parallel-stylua.patch"), [],
     "prep: parallel Lune runner (one process per spec file, identical output) and exact seed "
     "split for MapV2Regions/Walls/Lane",
     ["run.luau runs spec files in parallel (min(cpus-2,12) workers, memory budget from free "
      "RAM), replays output in file order, re-runs a worker that died once alone. --serial / "
      "AU_LUNE_WORKERS=1 / a name filter keep the old loop. Validated identical to serial "
      "(1609/1609, negative control 319/319)."]),
    ("patch", _p("au-mediagen.patch"), [],
     "prep: docs - Media_Gen replaces local ComfyUI, acestep-music, stable-audio, kokoro-tts "
     "and chatterbox-tts (owner-approved 2026-10-27)",
     ["2D generation, when the owner picks it, uses Media_Gen's image tools (generate_image "
      "klein/krea, edit_image, remove_background, upscale_image; library game); otherwise 2D "
      "comes from the owner via ChatGPT (foundry-media). AS-P1b is now the Media_Gen 2D check. "
      "Blender stays local.",
      "Audio (SFX, BGM, music, VO, dialogue) is made with the Media_Gen MCP: library game, one "
      "job_id then one media_check_job, .ogg/.mp3 for Roblox; an audio sprint's plan lists "
      "audio under ## Needs so the coordinator turns the tools on. Audio is no longer gated by "
      "AS-P1b (owner, 2026-10-27); AS-P1b now gates new 2D (ComfyUI) only. Voice keeps "
      "Media_Gen's -18 LUFS: audio_post --kind voice trims and converts but no longer "
      "peak-normalises (selftest 8/8). 2D image assets come from the owner (ChatGPT for now): "
      "ask first."]),
    ("patch", _p("au-context.patch"), [],
     "prep: context zones follow the session model's usable window (owner-approved 2026-10-27)",
     ["Amber 75% / Red 90% of (context - output reserve); checkpoint.py reads "
      ".plan/tmp/context_window.json written by the coordinator at each session start (local "
      "Qwen 262,144/32,768: Amber ~172k, Red ~206k, handoff ~218k). The 140k/190k upper-bound "
      "line is removed; without the file the old numbers apply."]),
    *[("patch", _p(f"au-rules-p{n}.patch"), [],
       f"prep: rules streamline phase {n} (owner-approved 2026-10-27)",
       ["Phase 1: done_check --skip-rows, clean-tree and memory fixes; checkpoint.py audits only "
        "the Kilo session; one stop protocol (blocker_kind product/tooling/user_action); the "
        "coordinator always pushes; track-aware paths. Phase 2: docs/rules/SESSION_CORE.md is "
        "the one always-read rule set; AGENT_RULES is reference only; one memory store "
        "(docs/memory via MEMORY_NOTE). Phase 3: Studio CALL lines are appended to the "
        "studio_lines file as each call returns (live counts in the control center). Plan: "
        "Foundry-Router reports/RULES_STREAMLINE_PLAN.md."]) for n in (1, 2, 3)],
    ("patch", _p("au-lune-cache.patch"), [],
     "prep: Lune per-spec cache with resume (owner-approved 2026-10-27)",
     ["run.luau reuses a spec file's PASS when its own file and every file it loaded, read or "
      "listed (recorded by the Loader) are unchanged; FAIL is never reused; global key = "
      "runner, tests/lune/lib, Lune version, Packages. Cached lines end ' (cached)'; the LUNE "
      "line ends '(C cached, R run)'. Each spec's entry is written as it finishes, so a stopped "
      "run resumes. --full / AU_LUNE_FULL=1 ignores the cache. Validated: identical lines to "
      "--full; cached rerun ~4 s vs ~6 min."]),
    ("patch", _p("au-lune-chunks.patch"), [],
     "prep: split the Scenery / Trails / PoiSolver seed sweeps into 50-seed spec files "
     "(owner-approved 2026-10-27)",
     ["Each 200-seed sweep is 4 spec files of 50 seeds; whole-set rules are certified per range "
      "or recomputed exactly over all 200 seeds, so the suite fails exactly when the old test "
      "did. Full parallel run 139 s (longest file 137 s, was ~5 min). 1618 tests (+9)."]),
    ("patch", _p("au-studio-calls.patch"), [],
     "prep: scripted Studio half: STUDIO_GATE request, the runner takes studio.lock "
     "(owner-approved 2026-10-27)",
     ["SESSION_CORE 5: when the prompt says the Studio half is scripted, end the turn with a "
      "STUDIO_GATE block; the coordinator runs host half + CALLs + -Finish and sends back only "
      "the failing rows; never run CALLs or -Finish yourself in that mode. 9: the runner takes "
      "studio.lock itself. gate.ps1 NEXT prints the scripted alternative. S17 (2 players) stays "
      "a manual row."]),
    ("patch", _p("au-audit.patch"), [],
     "prep: automation audit fixes (owner-approved 2026-10-28)",
     ["gate -Finish accepts verdicts across evidence/memory/plan-only commits; gate runs Lune "
      "and the negative control in parallel, --quiet, summary-only evidence (full output in "
      "tmp/guard/lune); Lune cache key ignores tests/ mounts, AU_LUNE_CACHE_DIR; gate.ps1 ASCII "
      "+ UTF-8 reads; run.py runs executables directly, streams, --bg/--wait, no Bypass; "
      "hash_fnv one cat-file --batch (41 s -> 2.5 s); done_check --phase build; plan_new "
      "--set-next; outline --width; checkpoint byte reads; lock.py stale + renew; AGENT_RULES "
      "contradictions removed; tests/checks/ folder mount for new checks. Plan: Foundry-Router "
      "reports/AUTOMATION_GAP_AUDIT_2026-10-28.md."]),
]
assert len(ENTRIES) == 11

# the watcher's tail, run once after the whole batch landed and was pushed (on the last
# entry): untracked Kilo rules/skills (TRIAL.md moved aside, overwritten files backed up),
# kilo-config --apply, Rojo restart (new tests/checks mount), Kilo reload, HK-CLOSE1 resumed
AFTER_LAST = [
    "move_aside:.kilo/TRIAL.md",
    "copy:" + os.path.join(SCRATCH, "au-untracked", ".kilo") + "=>.kilo",
    "kilo_config_apply",
    'run:"{python}" -m foundry_client restart rojo',
    "reload_kilo",
    "resume:HK-CLOSE1",
]
HOLD = "HK-CLOSE1"


def commands(prefix: list[str]) -> list[list[str]]:
    out = []
    for i, (kind, src, paths, subject, body) in enumerate(ENTRIES):
        cmd = [*prefix, "prep", "add", PROJECT, "--kind", kind, "-m", subject,
               "--after-task", AFTER_TASK, "--after-state", "COMPLETE", "--by", "migration"]
        cmd += ["--command", src] if kind == "script" else ["--source", src]
        for p in paths:
            cmd += ["--path", p]
        for b in [*body, CO]:
            cmd += ["--body", b]
        if i == len(ENTRIES) - 1:
            for a in AFTER_LAST:
                cmd += ["--after-action", a]
            cmd += ["--hold", HOLD]
        out.append(cmd)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="queue the entries (default: dry run)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--state", default=os.path.join(os.path.expanduser("~"),
                                                    ".foundry-coordinator"))
    ap.add_argument("--profiles", default=r"C:\Users\shock\foundry-deploy\foundry_coordinator"
                                          r"\projects")
    ap.add_argument("--deploy", default=r"C:\Users\shock\foundry-deploy",
                    help="the checkout whose foundry_coordinator runs (PYTHONPATH)")
    a = ap.parse_args(argv)
    prefix = [a.python, "-m", "foundry_coordinator", "--state", a.state, "--profiles", a.profiles]
    missing = [src for kind, src, *_ in ENTRIES if kind == "patch" and not os.path.isfile(src)]
    if missing:
        print("warning: patch files not found (the entry is refused until they exist):\n  "
              + "\n  ".join(missing), file=sys.stderr)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=a.deploy)
    rc = 0
    for cmd in commands(prefix):
        if not a.apply:
            print(subprocess.list2cmdline(cmd))
            continue
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        print((r.stdout or r.stderr).strip())
        if r.returncode != 0:
            print(f"stopped: exit {r.returncode}; later entries were not queued (remove the "
                  f"queued ones with `prep remove` before re-running)", file=sys.stderr)
            return r.returncode
    if not a.apply:
        print(f"\ndry run: {len(ENTRIES)} entries; add --apply to queue them", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
