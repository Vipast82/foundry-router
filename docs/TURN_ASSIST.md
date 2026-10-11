# Turn assist — supervisor, pre-judge, auditor and advisor for Cline

Cline runs its own agent loop and Foundry forwards its tools in `direct` mode,
so Foundry sees the whole conversation on every request and the model's whole
reply before Cline does. Turn assist uses that position to help the local
model: it fixes and blocks commands, notices loops, checks "done" reports, and
lets the model ask Claude or ChatGPT for help — all inside the router, with no
Cline change.

Everything is configured per persona in the **turn assist (JSON obj)** field
(Routing → Personas). `{}` turns it all off. Results show in
**Dashboard → Scoreboard**.

## The settings

| Key | Values | What it does |
|---|---|---|
| `think_policy` | `client` · `persona` · `adaptive` | `adaptive` picks the thinking level per turn from what just happened, using `think_levels` |
| `think_levels` | `{"routine", "after_edit", "after_failure"}` | Levels for: a plain read/search result · the turn after an edit · a failed tool call, a new user message or a supervisor note |
| `dedupe_reads` | bool | A `read_files` result identical to an earlier one becomes "identical to the read at message N" |
| `roblox_cleanup` | bool | Strips Roblox Studio's Assistant-plugin stack prefix from Luau errors (the real error and line stay); adds "call list_roblox_studios" to stale-studio errors |
| `image_keep` | N (0 = all) | Keep only the newest N screenshots in context |
| `soft_budget_tokens` | N (0 = off) | Extra context ceiling for the guard, without changing what Ollama loads |
| `prejudge` | `off` · `observe` · `enforce` | Rules applied to the model's tool calls before Cline runs them (below) |
| `prejudge_max_edit_chars` | N (default 12000) | An `editor` replacement longer than this is blocked in enforce mode |
| `supervisor` | `off` · `observe` · `nudge` · `escalate` | Loop / error-streak / cycling detectors (below) |
| `auditor` | `off` · `observe` · `enforce` | Checks a final report (below) |
| `auditor_min_tool_turns` | N (default 20) | Only audit sessions with at least this many tool turns |
| `auditor_max_rejections` | N (default 2) | Rejections before the report is let through with the verdict attached |
| `auditor_required_patterns` | [regex] | The report must match each, e.g. `["done_check.*PASS"]` |
| `auditor_forbidden_patterns` | [regex] | Replaces the default "should work now" style checks |
| `advisor` | bool | Offers the model a `consult_senior` tool (run by Foundry; Cline never sees it) |
| `frontier_models` | [model ids] | Tried in order for the auditor and advisor, each through the paid-call guardrail |
| `frontier_max_calls` | N (default 3) | Frontier calls per conversation (auditor + advisor together) |
| `frontier_rounds` | N (default 4) | Extra in-request rounds for audits and advice, on top of `worker_tool_max_steps` |
| `prejudge_disabled_rules` | [rule names] | Skip these block / fix rules for this persona |
| `sup_loop_repeats` … `sup_capture_repeats` | N | Every supervisor threshold (see "Tuning" below) |
| `sup_escalate_advice_at`, `sup_escalate_block_at` | N (3, 4) | Escalate mode: which finding asks the advisor / returns BLOCKED |
| `dedupe_min_chars` | N (400) | Shorter read results are never deduped |
| `auditor_min_report_chars` | N (40) | Shorter final replies aren't audited |
| `auditor_prompt`, `advisor_prompt` | text | Replace the built-in frontier instructions |
| `packet_turns`, `packet_max_chars`, `frontier_max_tokens` | N (15, 60000, 1500) | Frontier packet and answer sizes |

The seeded `claude-cline-act` starts with the free features on and the rest in
observe mode:

```json
{"think_policy": "adaptive",
 "think_levels": {"routine": "off", "after_edit": "low", "after_failure": "high"},
 "dedupe_reads": true, "roblox_cleanup": true, "image_keep": 3,
 "prejudge": "observe", "supervisor": "observe", "auditor": "observe",
 "advisor": false, "frontier_models": []}
```

On llama.cpp, Qwen's thinking is effectively on or off (the level only matters
for gpt-oss-style models), which is why routine turns use `off`.

## Global settings and precedence

Routing → Guardrails → **Turn assist (global)** edits, live:

* **defaults** — any key from the table above, for every persona;
* **block rules** — `{pattern, rule, advice, unless, enabled}`; `unless` exempts
  a command that also matches it (scratch files, the temp folder);
* **fix rules** — `{pattern, replacement, desc, enabled}`;
* **error markers** — the regexes that mark a tool result as failed.

The tables are pre-filled with the built-ins; "Use built-in rules" clears them.
Each rule's and detector's hit count is shown underneath. Per request:
built-in defaults < global defaults < the persona's `assist` JSON.

## Tuning: replaying recorded sessions

```bash
python -m foundry_router.evalharness replay ~/.cline/data/sessions --sweep --json out.json
python -m foundry_router.evalharness replay <dir> --set sup_play_calls=12
```

This converts each recorded Cline session into the messages Foundry receives
and runs the supervisor and pre-judge over it. With no ground truth, each
finding is scored by what happened next in the recording: **kept going** (the
trouble continued — a note would have helped) or self-corrected (noise).

Results on 165 sessions / 48,050 tool calls (2026-08-18 → 2026-10-11), which
set the defaults:

| Detector | Threshold | Fired | Kept going | Alternative tried |
|---|---|---|---|---|
| cycling | 10 start/stop in 40 calls | 45 | 80% | 8 → 86 fired, 56% |
| edit | 3 failed edits, same file | 30 | 67% | 2 → 123 fired, 45% |
| polling | 6 sleeps in a row | 13 | 46% | 4 → 46 fired, 35% |
| errors | 5 in a row / 6 of 8 | 42 | 33% | 4 → 68 fired, 27% |
| loop | 3 identical call + result | 14 | 36% | 2 → 94 fired, 31% |
| capture | output lost twice | 4 | 25% | — |

S43 (1,027 calls, 244 playtest calls): cycling first fires at call 681.
Pre-judge on the same corpus (observe): 144 npm→npm.cmd and 94 npx→npx.cmd
fixes; 135 shell rewrites of tracked files, 94 `git add -A`, 48 `-NoNewline`
writes, 24 oversized edits, 13 `--no-verify`, 12 recursive deletes would be
blocked. The editor limit is 12,000 chars because 6,000 would have blocked 3.4%
of all edits.

## The prompt-cache rule

llama.cpp keeps the previous turn's prompt cached; changing an older message
forces a re-read of everything after it. Every history change turn assist makes
is a pure function of the messages before it, so the same history is rewritten
the same way every turn and the cached prefix still matches. Only the newest
messages are ever new.

## Pre-judge

**Blocked** (enforce mode: the call is dropped, the model gets
`[Foundry pre-judge] blocked before it ran: <rule>. Instead: <advice>` and is
asked again inside the same request):
force push · push of all tags · `git reset --hard` · `--no-verify` ·
`git add -A` / `.` · `git clean -f` · `Set-Content` / `Out-File` /
`Add-Content` on `.md` `.lua` `.luau` `.json` · `-NoNewline` · recursive
delete outside `tmp/` · reading credential files · an `editor` replacement over
`prejudge_max_edit_chars`.

**Rewritten** (enforce mode; Cline runs the fixed command):
`ls -la` → `Get-ChildItem -Force` · `tail -n N` → `Select-Object -Last N` ·
`head -n N` → `Select-Object -First N` · `npx` → `npx.cmd` · `npm` → `npm.cmd`.

Observe mode logs every hit without changing anything.

## Supervisor

Detectors run over the whole history each turn:

| Finding | When |
|---|---|
| `loop` | the same call with the same result 3 times in a row |
| `errors` | 5 failures in a row, or 6 of the last 8 calls |
| `edit` | two failed `editor` edits to the same file |
| `cycling` | 8 playtest start/stop calls within 40 tool calls |
| `polling` | 4 `Start-Sleep` commands in a row |
| `capture` | the same command's output "could not be observed" twice |

* `observe` — logged only.
* `nudge` — a `[Foundry supervisor] …` note is appended to the tool result where
  it showed, so the model reads it; the next turn thinks at `after_failure`.
* `escalate` — counted since your last message: 1st and 2nd finding are notes
  (the 2nd asks for a STUCK note and a different approach), the 3rd also asks
  the advisor automatically, the 4th returns
  `BLOCKED by Foundry supervisor: … Needs: a decision from you` to Cline without
  a model call. Your next message resets the count.

## Auditor

Runs when the model's reply has no tool calls (Cline's "done"), the session has
at least `auditor_min_tool_turns` tool turns, and it isn't Cline's compaction
summary.

1. **Rule check (free):** required patterns present, no unverified-claim
   phrases, and no recent failed tool call left unmentioned.
2. **Judge (enforce mode only):** a frontier model gets a packet built by code —
   the kickoff, the last 15 turns head+tail, the latest evidence outputs — and
   the report, and answers `ACCEPT` or a numbered defect list.

On rejection the report and `[Foundry auditor] NOT ACCEPTED (n/max): …` stay in
Cline's chat, and the model keeps working in the same request. Observe mode logs
what it would have rejected and never calls a paid model.

## Advisor (`consult_senior`)

With `advisor: true` and `frontier_models` set, the model gets one extra tool.
Its description limits it to "after two failed approaches or before an
irreversible decision". Foundry sends the question plus a code-built packet to
the first allowed frontier model; the answer (≤ 300 words: root cause, next 3
actions, what not to do) goes back to the model and is also written into the
reply, so it stays in Cline's history. When the budget is used or nothing is
allowed, the model is told no senior is available.

## Subscription budgets

$20 plans report no usage to Foundry. Set call budgets on the backend instead:

```yaml
- name: meridian
  type: anthropic-compatible
  call_budget_5h: 40
  call_budget_daily: 150
- name: cliproxy                 # ChatGPT via CLIProxyAPI
  type: openai-compatible
  subscription: true             # paid for the guardrails, never metered dollars
  call_budget_5h: 30
  call_budget_daily: 100
```

Every guardrail check counts that backend's calls in Foundry's usage log over
the last 5 / 24 hours and denies past the budget.

## Plan: refuse instead of a silent local fallback

`fallback_policy: refuse` (persona editor: **fallback when paid denied**)
makes a direct persona return an error — "every frontier model is unavailable
… no local model was used" — when the guardrail denies every paid model. The
seeded `claude-cline-plan` has it on.

## Scoreboard and export

**Dashboard → Scoreboard** shows, per conversation: sprint id (from the
kickoff), turns, tool calls, errors per 100 calls, largest context, runaway
outputs (≥ 30K tokens), interventions by kind and frontier calls; plus errors
per 100 calls by context zone and the latest interventions.

**Export JSONL** (`/admin/api/interventions/export?hours=720`) writes one line
per intervention: the trigger excerpt, the finding, the text the model got,
`model_next_action` (the model's very next reply — its text and tool calls)
and the next intervention in the same conversation — trigger → intervention →
response, ready as fine-tuning pairs. The Scoreboard also shows the
subscription call budgets (used / budget / left per backend).

## Data: retention and export

Logs → **Data** lists every table with its size and age. Log tables can be
cleared (all rows or older than N days) and given a retention (`data_retention`
in config; defaults: interventions 180 days, assist_turns 90), pruned every
hour. Any table exports as JSONL / CSV / JSON, filtered by time and by any
column (`/admin/api/data/export?table=interventions&format=csv&conversation_key=…`),
or everything at once as a zip with a `manifest.json` describing each table
(`/admin/api/data/export-all?format=jsonl`). The `kv` table (auth tokens) is
never exported.

## Suggested rollout

1. One sprint as seeded (observe). Compare Scoreboard findings with what you
   would have stopped by hand.
2. `prejudge: enforce`, `supervisor: nudge`.
3. Set `frontier_models` (e.g. `["claude-sonnet-5", "cpa-gpt-5"]`) and
   `auditor: enforce` on a UI sprint; then `advisor: true`.
4. `supervisor: escalate` once nudges are trusted.
