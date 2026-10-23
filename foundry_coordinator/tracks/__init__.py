"""Team Lead tracks (2026-10-22, reports/TEAM_LEAD_DESIGN.md pieces 1-3):

  analysis.py   deterministic sprint-plan analysis: needs, dependency graph, file
                overlap, ordered tracks and the serial pairs with their reasons
  resources.py  capacity resources in the `locks` table (studio, gpu, blender), the
                Studio scheduler (main first, bounded merge-gate leases) and the
                journal the GUI timeline reads
  manager.py    the `tracks` table, git worktrees per track, sprints queued "on a
                track", the live `max_agents` cap and the track lifecycle
  merge.py      merging a finished track back at a quiescent point of the main sprint

Nothing here calls a model. The chat (router chat_engine.py) and the control
channel (control.py) drive it through typed actions; the engine calls
TrackManager.tick / project_for / writer_slots.
"""



def __getattr__(name):                     # lazy: analysis.py imports without the manager
    if name in ("TrackManager", "TrackError"):
        from . import manager
        return getattr(manager, name)
    raise AttributeError(name)
