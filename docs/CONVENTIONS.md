# Operating conventions

Working agreements for changes to this repository and its deployment. These
are routine, not optional — each exists because skipping it has already cost
debugging time.

## Version bump per shipped change

Every user-visible change bumps `__version__` in `foundry_router/__init__.py`
(patch for fixes, minor for features). The UI header and `/api/version` read
it, so a deployment's behavior is always attributable to an exact version.
`foundry_coordinator/__init__.py` carries the same version (bump both;
`foundry_client` imports the router's). `tests/test_version_parity.py` fails
when they disagree or when the newest `docs/RELEASE_NOTES.md` entry is not
this version.

## Version tag per version

Every version that lands on `main` gets an annotated tag `vX.Y.Z` on the
commit that set it, pushed with it (`git tag -a vX.Y.Z -m "Foundry Router
vX.Y.Z"`, then `git push origin vX.Y.Z`). Tags make each version selectable
in git and GitHub (`git checkout v0.115.0`, `git diff v0.114.1 v0.115.0`).
Versions v0.1.0–v0.115.0 were back-filled on 2026-10-17. v0.98.0 points at
147c098; a parallel CLIProxyAPI-branch commit (5d93d36) also called itself
0.98.0 and was merged back in v0.101.0.

## `main` / `cline` branch parity

`main` and `cline` track the same history. After landing work on `main`, push
the same commits to `cline` (`git push origin main:cline`) so both entry
points build identically. Divergence between them is a bug, not a workflow.

## Eval run per persona/prompt change

Any change to a persona (description, category, bias, pins, weights, judges,
review settings) or to routing prompts should be followed by an eval-harness
run for the affected persona, and the score delta reported as part of that
change:

- GUI: **Evals tab → Run eval** (pick the persona; judge model optional —
  shape checks alone are still a meaningful signal).
- API: `POST /admin/api/eval/run {"persona": "...", "judge_model": "...",
  "wait": true}` returns the scored run synchronously.

The runs table shows the Δ against the persona's previous completed run —
that Δ is the number to report. Scores accumulate in `eval_runs` /
`eval_results`, so trends stay visible over time; a single before/after
snapshot is the minimum, not the goal.

## Quality-tracking hygiene

- Corrections and cache hits are always visibly marked in the response
  (🔎 review, ⚡ cache) — never silent.
- New spend paths must route through `GuardrailEngine.check_paid_call`; the
  conservation ladder is the single authority on paid-tier usage.
- The insight digest (Dev Log tab) is read-only reporting: the system never
  rewrites its own prompts or config from accumulated stats — the operator
  reviews and decides.

## Tests run in parallel

`pytest` runs on pytest-xdist workers by default (`addopts` in
`pyproject.toml`; `pytest -n 0` for one process). Every worker is a separate
process sharing this machine, so a test must not depend on anything another
test can touch at the same time:

- files and dirs only under `tmp_path` / `tmp_path_factory` — never the
  checkout, `%TEMP%` as a whole, the home dir or the live client's state; a
  test that inspects the temp dir points `tempfile.tempdir` at its own;
- ports only from a bind to port 0, held for as long as the test relies on
  them (a "closed" port stays bound without listening);
- no waits on real clocks that a fake clock or a direct call can replace.

The default `--dist worksteal` balanced this suite best (8 workers: 10.5 min
against 13 min for `--dist load`; 12 workers were slower and left under 2 GB
free). No test needs serializing today. One that truly cannot share the
machine gets `@pytest.mark.xdist_group("name")` with its reason, and addopts
then switches to `--dist loadgroup` (worksteal ignores groups); skipping or
weakening a test to make it parallel-safe is not an option. Compare with
`pytest -n 0` when a failure only shows in parallel.
