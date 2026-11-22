"""One switch for how hard the sprints lean on the paid models
(COORDINATOR_AUTOMATION_NEEDS item 16).

The agent caps were three separate settings (`max_agents`, `resources.claude_agents`,
`resources.claude_agents_over` at `resources.claude_usage_pct`), the cutoffs a fourth place
(Router settings), and saving meant editing each of them and putting each back afterwards.
`[automation] budget_mode` is the one switch:

  normal        today's values, nothing changed.
  save_claude   Claude sprint sessions at once: `save_claude_agents` (default 1), whether or
                not resources.claude_agents is set. Claude's usage cutoff is at most
                `save_claude_cutoff_pct` (default 75).
  save_all      the same, plus: sprints at once across the project `save_all_agents`
                (default 1), and the cutoff of every other paid backend is at most
                `save_all_cutoff_pct` (default 75).

The agent caps are this module's: an overlay read where the caps are read, rewriting no
setting, so switching back to normal restores everything by itself. The cutoffs are the
router's (foundry_router/budget_caps.py): when the project's settings are deployed it lowers
the backends' cutoffs to the mode's, and a call past a cutoff goes to the persona's next
route, exactly as with the cutoffs under Router settings - the order of the routes is the
owner's and is not changed. Back in normal the router puts its own values back.
"""

from __future__ import annotations

from typing import Optional

from .automation import get as auto_get

MODES = ("normal", "save_claude", "save_all")
LABEL = {"normal": "normal", "save_claude": "save Claude", "save_all": "save all"}


def mode(P) -> str:
    try:
        m = str(auto_get(P, "budget_mode") or "normal")
    except Exception:                                            # noqa: BLE001
        return "normal"
    return m if m in MODES else "normal"


def _int(P, key: str, default: int) -> int:
    try:
        return max(0, int(auto_get(P, key)))
    except Exception:                                            # noqa: BLE001
        return default


def claude_agents(P, agents: int) -> int:
    """resources.claude_agents as the mode sees it (0 = no cap, as before, in normal)."""
    if mode(P) == "normal":
        return agents
    cap = max(1, _int(P, "save_claude_agents", 1))
    return cap if agents <= 0 else min(agents, cap)


def max_agents(P, n: int) -> int:
    """[automation] max_agents as the mode sees it."""
    if mode(P) != "save_all":
        return n
    return max(1, min(n, max(1, _int(P, "save_all_agents", 1))))


def describe(P) -> dict:
    """The mode and the numbers it puts in force, for the board and the log."""
    m = mode(P)
    out = {"mode": m, "label": LABEL[m]}
    if m != "normal":
        out["claude_agents"] = max(1, _int(P, "save_claude_agents", 1))
        out["claude_cutoff_pct"] = _int(P, "save_claude_cutoff_pct", 75)
    if m == "save_all":
        out["max_agents"] = max(1, _int(P, "save_all_agents", 1))
        out["other_cutoff_pct"] = _int(P, "save_all_cutoff_pct", 75)
    return out


def words(d: dict) -> str:
    if d["mode"] == "normal":
        return "budget mode is normal: the caps and cutoffs are the project's own settings"
    cut = lambda v: f"at most {v}%" if v else "unchanged"        # noqa: E731
    s = (f"budget mode is {d['label']}: {d['claude_agents']} Claude sprint session(s) at once, "
         f"Claude's cutoff {cut(d['claude_cutoff_pct'])}")
    if d["mode"] == "save_all":
        s += (f"; {d['max_agents']} sprint(s) at once, every other paid model's cutoff "
              f"{cut(d['other_cutoff_pct'])}")
    return s


def tick(c, P) -> Optional[dict]:
    """Say once, in the events (and so the watch log), when the mode was switched and what
    that puts in force. Never raises."""
    try:
        d = describe(P)
        key = f"budget_mode:{P.id}"
        before = c.store.meta(key) or "normal"
        if before == d["mode"]:
            return None
        c.store.set_meta(key, d["mode"])
        detail = {**d, "before": before, "text": words(d), "digest": True}
        c.store.event(P.id, "", "budget_mode_changed", detail=detail)
        return detail
    except Exception:                                            # noqa: BLE001
        return None
