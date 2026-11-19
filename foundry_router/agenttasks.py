"""Agent tasks: deliverables from the work this ecosystem actually does.

Every other suite has an answer. These do not - they ask for a thing to be
built, which is what a model is for here and the one thing none of the other
suites measure. A deliverable has no key, so it is **never given a score**:
`score_one` returns None for it, the run average leaves it out, and the
deliverable is stored so two models' answers to the same task can be compared
blind with the judge protocol (eval tier 4). That refusal is the point. The
needle suite shipped a scorer that gave 100 to a model that found nothing,
because `answer_score` is a length heuristic and `code_score` only compiles -
and both of those would fire on every task here.

The tasks are the operator's own domains rather than a public benchmark's:
Luau and Roblox server code, Blender scripting, this router's own Python
(asyncio, SQLite, FastAPI), and operating the sprint system. A model that
cannot do these cannot run this ecosystem, which is the question being asked -
"if I change models, will this one handle my work".

Each task carries **acceptance criteria**, and the judge is shown them with the
two answers. That is deliberate: a judge asked "which is better" answers with
its taste, and a judge asked "which meets these" answers about the work. The
criteria are what makes a deliverable comparison repeatable rather than a
preference poll, and they are written from the failure modes this project has
actually hit - the client-trusting RemoteEvent, the pivot that arrives rotated,
the migration that assumes a fresh database.

Nothing here is executed. A Luau deliverable is prose to this router, and
running model-written code is a separate decision with a sandbox behind it.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

SYSTEM = ("You are a senior engineer. Produce the deliverable asked for, in "
          "full, ready to use. Prefer correct and complete over long; do not "
          "restate the question or explain what you are about to do.")

# (key, tags, task, [criteria]) - criteria are shown to the judge with both
# answers, so the comparison is against the requirement and not against taste.
TASKS = [
    ("remote_authority", ["luau", "roblox", "security"],
     "A Roblox server script handles a RemoteEvent `BuyItem` like this:\n\n"
     "```lua\nBuyItem.OnServerEvent:Connect(function(player, itemId, price)\n"
     "    player.leaderstats.Coins.Value -= price\n"
     "    giveItem(player, itemId)\nend)\n```\n\n"
     "Rewrite it so it is safe against a malicious client.",
     ["the price comes from a server-side table, never from the client",
      "the item id is validated against that table before anything is given",
      "the purchase is refused when the player cannot afford it, and the "
      "balance is not changed in that case",
      "two rapid calls cannot buy one item twice (the check and the deduction "
      "are not separated by a yield, or a per-player guard covers them)"]),

    ("connection_leak", ["luau", "roblox", "lifecycle"],
     "This runs once per player and the server slows down over a long session:\n\n"
     "```lua\nPlayers.PlayerAdded:Connect(function(player)\n"
     "    player.CharacterAdded:Connect(function(char)\n"
     "        char.Humanoid.Died:Connect(function() onDeath(player) end)\n"
     "        RunService.Heartbeat:Connect(function() track(char) end)\n"
     "    end)\nend)\n```\n\n"
     "Find why and give the corrected script.",
     ["names the leak: a Heartbeat connection per character, never "
      "disconnected, so respawns accumulate them",
      "disconnects per-character connections when the character is removed "
      "or dies, not only when the player leaves",
      "the corrected script holds its connections somewhere it can reach them "
      "again (a table keyed per player, or a connection set)",
      "does not replace the per-frame work with a polling loop that has the "
      "same cost"]),

    ("pivot_rotation", ["luau", "roblox", "assets"],
     "Assets imported into a Roblox place arrive with a PivotOffset rotated 90 "
     "degrees on X, so every placed model lies on its side. Write a Studio "
     "script that corrects the already-placed models in a folder without "
     "moving them in the world.",
     ["corrects the pivot rather than the model's CFrame, so the visible "
      "position does not change",
      "handles a Model whose PrimaryPart is nil instead of erroring on it",
      "is idempotent, or says plainly that it is not safe to run twice",
      "reports what it changed rather than running silently"]),

    ("blender_export", ["blender", "python", "assets"],
     "Write a Blender Python script that exports each selected mesh object to "
     "its own FBX in a chosen folder, with the object's origin at the world "
     "origin in the exported file and the scene left exactly as it was.",
     ["exports one file per object, named from the object",
      "the object is at the origin in the export and the scene is restored "
      "afterwards (the transform is put back, or the export uses its own copy)",
      "selection and active object are restored, since the script changes them",
      "uses bpy.ops.export_scene.fbx with use_selection, rather than writing "
      "FBX by hand"]),

    ("async_pace", ["python", "asyncio", "router"],
     "Write an asyncio helper that guarantees a minimum gap between calls to "
     "the same host, shared across every caller in the process, where "
     "different hosts never wait on each other.",
     ["the gap is per host, and two hosts proceed concurrently",
      "waiting does not busy-wait: it awaits rather than spinning",
      "two coroutines starting at once on the same host are spaced, not both "
      "released after one wait (the gap is claimed before awaiting, or a "
      "per-host lock covers it)",
      "a zero or missing gap disables the wait rather than erroring"]),

    ("sqlite_migration", ["python", "sqlite", "router"],
     "A running service has a SQLite table `results` created without a "
     "`deliverable` column. Write the migration code that adds it safely on a "
     "database that may or may not already have it, and say what it cannot do.",
     ["checks the existing columns (PRAGMA table_info or equivalent) instead "
      "of relying on catching an error",
      "is safe to run on every startup, including on a fresh database",
      "does not rebuild or copy the table to add a nullable column",
      "says what ALTER TABLE in SQLite cannot do - drop or retype a column in "
      "older versions, add a NOT NULL column without a default"]),

    ("slow_run_diagnosis", ["ops", "router", "diagnosis"],
     "A benchmark run of a local model scored 61.5 where comparable runs of "
     "another model scored 72.9 on average (sd 3.9, range 62.8-79.9), took "
     "about three times as long per case, and had 16 cases with a zero tool "
     "score against the other model's 6 - while its prose score was higher. "
     "Write the plan you would follow to find out why, in order.",
     ["notices the gap is below the whole range of the comparison runs, so it "
      "is not run-to-run noise",
      "separates the two candidate causes - the model is slow, or the model "
      "is failing to emit tool calls the harness can read - and says how each "
      "would be told apart",
      "proposes reading what the model actually emitted on a zero-tool case, "
      "not only the scores",
      "does not conclude from the numbers given alone"]),

    ("jsonl_checker", ["python", "tooling", "router"],
     "A benchmark suite is a .jsonl file, one case per line, each an object "
     "with id, task_id, tags (list), messages (list of {role, content}) and "
     "reference (object). Write a checker that reports every problem in such a "
     "file and exits non-zero if any line is bad.",
     ["reports every bad line rather than stopping at the first",
      "names the line number and what is wrong with it",
      "catches a line that is not valid JSON separately from one that is "
      "valid JSON but the wrong shape",
      "exits non-zero only when something was actually wrong"]),

    ("datastore_save", ["luau", "roblox", "persistence"],
     "Write the Luau for saving a player's data that does not lose it when the "
     "player leaves, and does not get throttled by DataStore limits during a "
     "busy server.",
     ["saves on PlayerRemoving AND binds to game close, since the server can "
      "shut down with players still in it",
      "retries a failed SetAsync/UpdateAsync with a backoff rather than once",
      "respects the request budget - does not save on every change - and says "
      "what rate it assumes",
      "uses UpdateAsync or a session lock rather than Get-then-Set, which "
      "loses a concurrent write"]),

    ("review_the_diff", ["python", "review", "router"],
     "Review this change and say what is wrong with it:\n\n"
     "```python\ndef pick_judge(models, used):\n"
     "    free = [m for m in models if m not in used]\n"
     "    return free[0] if free else models[0]\n\n"
     "async def judge_all(pairs, models, used=set()):\n"
     "    out = []\n"
     "    for a, b in pairs:\n"
     "        m = pick_judge(models, used)\n"
     "        used.add(m)\n"
     "        out.append(await score(m, a, b))\n"
     "    return out\n```",
     ["names the mutable default argument: `used=set()` is created once and "
      "shared by every call, so a second call starts with the first's state",
      "notices the fallback `models[0]` can pick a model already used, which "
      "the function exists to avoid",
      "gives corrected code",
      "does not invent a problem that is not there"]),
]


def build(svc, name: str = "agent", limit: int = 0, tasks=None) -> dict:
    """Write the agent-task suite. Cases carry `reference.deliverable`, which
    is what stops them being scored."""
    from .bench import _dir
    rows = list(tasks if tasks is not None else TASKS)
    out = []
    for key, tags, task, criteria in rows:
        if not criteria:
            # a task whose quality nothing states is a preference poll
            continue
        out.append({
            "id": f"AGENT-{key}",
            "task_id": key,
            "tags": ["agent", "deliverable"] + list(tags),
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": task}],
            "reference": {
                # no content and no must_include: there is no answer key, and
                # this flag is what keeps the length heuristic off it
                "deliverable": True,
                "criteria": list(criteria),
            },
        })
    if limit:
        out = out[:int(limit)]
    f = _dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in out:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    try:
        svc.db.log_event(
            "info", "bench",
            f"built the {name} suite: {len(out)} deliverable task(s). These are "
            f"NOT scored - a deliverable has no answer key - and are compared "
            f"between two runs instead (Evals - Pairwise)")
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(out), "path": str(f),
            "scored": False,
            "note": "deliverable tasks are not scored: run two models on this "
                    "suite, then compare the runs pairwise"}


def criteria_for(svc, suite: str, task_id: str) -> list:
    """The acceptance criteria of one case, read back from the built suite.

    Read from the suite rather than from TASKS so a comparison uses the
    criteria the run was actually made with, not whatever this module says
    today."""
    from .bench import cases
    for c in cases(svc, suite):
        if (c.get("task_id") or "") == task_id:
            return list((c.get("reference") or {}).get("criteria") or [])
    return []


def deliverables(db, run_id: int) -> list:
    """What one run produced, per task."""
    return db.query(
        "SELECT case_id, task_id, COALESCE(deliverable,'') AS deliverable, "
        "       answer_chars, COALESCE(error,'') AS error "
        "FROM bench_results WHERE run_id=? ORDER BY id", (int(run_id),))


def missing_reason(rows: list) -> Optional[str]:
    """Why a run cannot be compared, or None when it can.

    A run made before v0.181.206 stored only `answer_chars`, so there is
    nothing to compare and saying so beats returning an empty result."""
    if not rows:
        return "that run has no results"
    if not any((r["deliverable"] or "").strip() for r in rows):
        return ("that run stored no deliverables - it either ran a suite with "
                "answer keys, or it ran before v0.181.206, when only the "
                "answer's length was kept")
    return None
