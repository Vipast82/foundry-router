# Operating conventions

Working agreements for changes to this repository and its deployment. These
are routine, not optional — each exists because skipping it has already cost
debugging time.

## Version bump per shipped change

Every user-visible change bumps `__version__` in `foundry_router/__init__.py`
(patch for fixes, minor for features). The UI header and `/api/version` read
it, so a deployment's behavior is always attributable to an exact version.

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
