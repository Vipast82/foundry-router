# Benchmark difficulty: what the tiers mean and how to raise them

Written for v0.181.223 (2026-11-21). Code: `foundry_router/benchhard.py`,
`foundry_router/bench.py`. Covers the code-reasoning suite; the other suites
are listed at the end with where they stand.

## What a tier means

A tier is a statement about who misses the case, measured, not a guess about
how the case looks.

| Tier | Meaning | Measured as |
|---|---|---|
| easy | the local models pass it | qwen3.8-flash (3-bit) at 90% or more |
| medium | local models miss it, frontier models do not | flash about half, Sonnet 5.5 and Opus 5.5 all |
| hard | frontier models miss some of it | Sonnet 5.5 / Opus 5.5 between about 50% and 90% |
| expert | frontier models fail it inside the budget | Sonnet 5.5 / Opus 5.5 at about a third or less |

Before this, tiers were assigned by eye. On two full profiles both local
models passed every case named easy, medium and hard in code reasoning and in
long-context recall, and 75-100% of the ones named expert.

## The conditions every model gets

* **Reasoning is allowed; one line is scored.** A hard case ends with
  "End your reply with one line of the form `ANSWER: <value>`". Only that line
  is read (`bench.final_answer`). A model that thinks out of sight and one that
  writes its working in the reply are on the same footing.
* **8,192 tokens** (`bench.THINK_ROOM`) for the whole reply, working included.
* **A reply past the budget is out of room, whatever it says**
  (`bench.over_budget`, 10% tolerance). The Claude subscription backend does
  not honour `max_tokens`: asked for at most 16,000, one Sonnet 5.5 reply ran
  to 84,485 tokens over eleven minutes. Local backends are cut off at the
  budget, so everything is held to it.
* **300 seconds** a case (`bench.CASE_SECONDS`), for cases with their own
  budget. Past it is out of time and scores 0.
* **No tools.** Checked: asked for a SHA-256, both Claude models said they
  could not run code and did not guess.

## What was tried, and what it showed

All with reasoning allowed. Sonnet 5.5 and Opus 5.5 through the router,
thinking off (the router's default for them). GPT was not used: its weekly
usage stood at 93% of a 95% cutoff.

| Family | flash | Sonnet 5.5 | Opus 5.5 | Tier |
|---|---|---|---|---|
| 16 programs stacking several Python rules | 16/16 | 16/16 | - | easy |
| 14 programs whose names and comments lie | 8/8 | 13/14 | 14/14 | easy |
| traces, 8-16 steps | 18/20 | 19/20 | - | easy |
| traces, 32-64 steps | 13/20 | 20/20 | - | medium |
| traces, 128-256 steps | - | 9/10 | 6/10 | hard |
| counting, tables, cycles, tours to level 4 | 13/24 at levels 2-4 | 36/36 | 35/36 | easy to medium |
| orderings on a random allowed list, 7-8 items | - | 1/2 | 2/2 | hard |
| the same, 9 items | - | timed out | timed out | expert |
| random 3-term rules, 13 variables | - | right | right | hard |
| the same, 15 variables | - | over budget (10.6k) | right (6.2k) | hard |
| random one-way route, 9 stops | - | right (6.0k) | right (9.0k) | hard |
| the same, 10 stops | - | wrong | over budget (11.5k) | expert |
| count over 500,000 numbers | - | wrong after 84k tokens | - | expert |
| iterated map, modulus about 20,000 | - | wrong after 20k tokens | - | expert |

What this says about frontier models: short code is not hard for them however
it is written, and neither is anything with a table to fill in. They worked an
8-city tour, a 12-node graph colouring and a count over 80,000 numbers by hand
and got them right. Two things do stop them: a problem with no structure to
lean on once it is big enough (the method doubles with every step up), and a
structured one too big to finish inside the budget.

## What is in the suite

119 cases: the 44 original snippets (36 easy, 8 medium), 16 of the stacked and
lying programs (easy), and 59 rungs of 11 generated ladders.

| Ladder | What the program does | Level sets |
|---|---|---|
| trace: ledger, grid, queue, mix | follow state through a loop | steps: 8, 16, 32, 64, 128, 256 |
| hampaths | count orderings where each neighbouring pair is on a random list | items: 5 to 9 |
| sat | count true/false assignments satisfying random 3-term rules | variables: 7 to 15 |
| route | cheapest round trip, random one-way costs | stops: 6 to 10 |
| count | count numbers under N meeting a digit rule | N: 2,000 to 3,000,000 |
| coins | ways to make a total from coins | total: 47 to 677 |
| cycle | a squaring map iterated a million times | modulus: 200 to 80,000 |
| lcs | longest common subsequence of two strings | length: 9 to 32 |

Each program is generated from (kind, level, seed) and its answer is computed
by running it when the suite is built. Nothing a model writes is ever run.

**The score weights every tier the same.** 70 easy cases would otherwise
outvote 9 expert ones: each case carries a weight so the four tiers each
contribute a quarter (`bench.tier_weights`).

**A ladder stops being climbed.** After two levels of one ladder are missed in
a row, the levels above are not sent: they score 0 and the card says "above
the level it stopped passing (not sent)". Without this a local model would
spend about five minutes a case failing the top rungs. One miss does not close
a ladder.

## When a model clears the top

1. Bench tab, "top level" beside *Build the code suite*: raise it by one and
   build. Every ladder gets one more rung, twice the size, tiered as the last
   entry in its row of `benchhard.LADDERS` until it is measured.
2. Run the suite on the models you are comparing and read "How far up each
   ladder" on the card: that is the number that moves.
3. If a rung's tier is wrong (everything passes a "hard" one), move the word in
   its row of `LADDERS` and rebuild. The tiers are a table for that reason.

Limits, set by build time rather than by difficulty: hampaths stops at 9 items
and route at 10 stops (a brute-force count over orderings), sat at 16
variables. Past those, add a kind rather than a level.

## The other suites

Not changed in v0.181.223. Where they stand on the two local profiles:

| Suite | flash | brain lane | Note |
|---|---|---|---|
| Long-context recall | 100.0 | 99.2 | at the ceiling: needs multi-hop and counting kinds |
| Tool picking | 100.0 | 100.0 | at the ceiling: needs indirect needs and near-twins |
| Find and use tools | 93.8 | 86.2 | near it: needs paraphrased tasks and argument checks |
| Coordinator protocol | 86.5 | 59.0 | separates the local models; frontier not measured |
| Real work replay | 79.9 | 68.3 | separates the local models; frontier not measured |
