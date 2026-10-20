"""Foundry development coordinator.

Runs an explicitly approved queue of development tasks (sprints) through a
durable lifecycle: a fresh coding session per attempt (Kilo, through Foundry),
the project's own mechanical verification, a bounded frontier review of the
exact commit, closeout, and only then the next eligible task.

Three layers, kept apart:
  engine      (this package: states, store, engine, review contract) knows
              nothing about any particular game, repository or document format;
  project     profiles (TOML) and narrow adapters supply the task format,
              verification commands, prompts, approval and publishing policy;
  Foundry     supplies model personas, strict routing, budgets and
              observability; the coordinator only asks it for inference and
              reports state to it.

Stdlib only (Python 3.11+), so it runs on the workstation next to the
repository, Kilo and Roblox Studio without installing anything.
"""

__version__ = "0.130.0"
