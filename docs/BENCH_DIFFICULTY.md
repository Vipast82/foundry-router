# Benchmark difficulty: what the tiers mean and how to raise them

Written for v0.181.223 to v0.181.228 (2026-11-21). Code:
`foundry_router/benchhard.py`, `foundry_router/bench.py`,
`foundry_router/protocolbench.py`. Covers every suite.

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
contribute a quarter (`bench.tier_weights`). A suite is weighed this way only
when every tier in it has at least four cases: tool picking has one medium
case, which would otherwise be half its score.

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

## Long-context recall (v0.181.224)

The grid of single notes is kept: it says where a model stops finding things,
out to the end of its window. Every case in it is now **easy**, because that is
what was measured: flash found 141 of 141 out to 815,000 characters, the brain
lane 122 of the 123 that fit its window.

Added: ladders that put many notes into 32,000 characters of the same filler
and ask for something that needs the right ones put together.

| Kind | The question | In the suite |
|---|---|---|
| chain | a sprint's code, given through other sprints' codes (some "plus N") | hops: 12 and 40 |
| asof | the code in force on a date, from dated notes out of order | notes: 27 and 100 |
| top | which sprint spent the most, and how much | amounts: 30 and 140 |
| blocked | which sprints are blocked by their latest status note | sprints: 12 to 160 |
| ledger | one sprint's total over every ledger note | amounts: 20 to 450 |

| Kind and level | flash | Sonnet 5.5 | Opus 5.5 | Tier |
|---|---|---|---|---|
| chain, asof, blocked, ledger, levels 1-4 | 15/16 | - | - | easy |
| the same four, levels 3, 5, 7 | - | 12/12 | 11/12 | - |
| chain, asof, levels 6, 8, 10 | 6/6 | right to level 7 | right to level 7 | easy |
| top, level 8 / level 10 | right / wrong | right / right | right / right | easy / medium |
| blocked, ledger, level 6 | 2/2 | - | - | easy |
| blocked, ledger, levels 8-9 | 0/4 | 4/4 | 3/3 | medium |
| blocked, ledger, level 10 (100 sprints, 270 amounts) | 0/2 | 0/2, over budget | 2/2 | hard |
| blocked, ledger, levels 11-12 | - | 0/4 | 0/3, and one call that failed | expert |

Following a chain or finding the note in force on a date is solved at every
size tried. What separates the models is putting a great many notes together
inside the budget. The suite takes blocked and ledger to level 12 (two sets
from level 9) and the other three at levels 6 and 10: 28 cases. "top level"
beside *Build the needle suite* adds rungs to blocked and ledger.

## Tool picking (v0.181.224)

The 48 cases over the live catalogue are kept and are **easy**: both local
models chose right 48 times in 48.

Added: generated catalogues of near-identical tools - an action, a kind of
file, a mode, where it runs, where the result goes, a size limit and a price -
where the request is worded differently from the descriptions.

| Kind | The right answer |
|---|---|
| fit | the one tool that meets every part of the request |
| none | "none": a sibling meets all but one part, nothing meets all |
| best | of several tools that qualify, the cheapest that can take the file (from level 5: lowest price per megabyte) |

Level sets the catalogue (80 to 1,200 tools; 1,200 is about 48,000 tokens,
which is as far as the brain lane's window allows), the siblings one step from
the answer (4 to 18) and, for *best*, the tools that all qualify (4 to 160).

**There is no hard tier here.** Sonnet 5.5 and Opus 5.5 chose right in every
case sent (28 calls), to 1,200 tools and 160 qualifying rivals, in about a
thousand tokens each. The one case both "missed" was the answer key's fault: the request
said "without touching the graphics card", which an edge-node tool also
satisfies, so they were right and the key was not. Every request phrase now
fits one value only, and a test holds that.

qwen3.8-flash chose right in 10 of 11: every *fit* and *none* to 1,200 tools,
*best* to 96 rivals, and missed *best* at 160 rivals. That rung is the suite's
one medium case; everything else in tool picking is easy. The ladder is there
for a smaller model than the ones run today, and "top level" beside *Build the
tool-picking suite* raises *best*.

## Find and use tools, coordinator protocol, real work replay (v0.181.225)

Sonnet 5.5 was run on the router against all three (2026-11-21). None of them
has a tier that is hard for a frontier model, and for the replay suite none
can: its answer key is a frontier session's own move. What reading the misses
found was four faults in the cases and the scoring.

| Suite | flash | brain lane | Sonnet 5.5 | What its misses were |
|---|---|---|---|---|
| Find and use tools | 93.8 | 86.2 | 96.2 | 2 of 40, both the cases' fault |
| Coordinator protocol | 86.5 | 59.0 | 91.8 | 18 of 18 scenarios; 9 real turns, all the key's fault |
| Real work replay (30 cases) | 79.9 | 68.3 | - | of the 20 that ran: 14 matched, 1 same move with another tool, 3 cases with no task in them, 2 misses; 10 calls failed (backend away) |

**Find and use tools.** Two cases every model misses: asked for "the FastFlag
prefix types" a model answers from what it knows and calls nothing, and "wait
for text or time" gives nothing to wait for, so it asks. The request now says
the answer has to come from a tool, and a tool with no required input is given
one. `found` was judged on the finder's first eight results while the model
was shown the twenty it asked for; it is now judged on what was shown. Added:
six cases over the look-alike catalogues (300 to 1,200 tools) reached through
the finder, with the request in other words than the descriptions and the
call's arguments checked (0.3 found, 0.4 chosen, 0.3 called with the inputs
given). flash got 6 of 6 in two to nine searches, Sonnet and Opus 4 of 4 each
in one: every rung is easy.

**Coordinator protocol.** All nine of Sonnet's misses were "wrong status" on
real turns, seven of them NOT DONE where the session had said DONE - the miss
flash makes too. The coordinator's first prompt tells a session "BUILD session
for sprint X ... This session's job: the plan's remaining BUILD steps ... then
the gate", so it is DONE at the gate and closeout belongs to a CLOSE session.
Cut to the newest turns, the case had lost that prompt. A real case now says
which kind of session it is and quotes its job; a conversation that never says
is not used. The 18 scenarios are unchanged: flash 97.0, the brain lane 69.7,
Sonnet 18 of 18.

**Real work replay.** A case could open on "Attached media from tool result:"
with the picture gone and nothing else in it; three of Sonnet's misses were
those, answered "no task came through". A case never opens on one now. The
suite has no tiers of its own, so the card tiers each case from what every
model run on that version of the suite did: easy when every local model
passed, medium when one missed and no frontier model did, hard when a frontier
model missed, expert when two or more frontier models ran it and all missed. A
call that failed is not a miss.

## Grounded answers (v0.181.228)

A new suite, the first addition in `docs/BENCH_GAPS.md`. A stack of short work
records - an id, a collection, a tier, a date, then a client's retainer,
renewal, account lead and payment terms, written one of three ways - and a
question with one right reply: `ANSWER: <value> [<id>]`, or `ANSWER: NOT FOUND`.

| Kind | The right reply |
|---|---|
| cite | the value and the id of the one record that has it, among near-named clients |
| join | a project record names the client, the client's record has the value: both ids |
| conflict | the same fact in several dated records: the latest, not the others |
| absent | NOT FOUND: no record is about that client, or its record leaves the field out |
| counter | a record says a 4-day week, HTTPS on 8443: the record, not what a model believes |
| scope | "use only work_general": the confidential record has the answer and may not be used |
| tally | how many clients a named lead has and what their retainers add up to |

| Kind and level | flash | Sonnet 5.5 | Opus 5.5 | Tier |
|---|---|---|---|---|
| the six look-ups, 12 to 160 records | 28/28 | 13/13 | 13/13 | easy |
| tally, levels 2 to 6 (5 to 26 records to add) | 5/5 | 2/2 | 2/2 | easy |
| tally, levels 7 to 9 (36 to 70) | 0/3 | 1/2 | 2/2 | medium |
| tally, levels 10 and 11 (100, 140) | - | 2/2 | 2/2 | medium |
| tally, level 12 (190) | - | over budget | right | hard |
| tally, levels 13 and 14 (240, 290) | - | 0/2, over budget | 0/2, over budget | expert |

A look-up over records is easy for all three, the tier rule and NOT FOUND
included. That is worth knowing and worth keeping as the floor: a smaller
model that fails it cannot be given work documents. What separates models is
adding up across records, the same finding as the recall suite's ledgers.
The suite is 39 cases: 24 look-ups and a 15-case tally ladder, with four
cases at level 12 and two each at 13 and 14 so no tier rests on one case.

Found while calibrating, both first seen as Sonnet and Opus scoring exactly
the same partial mark: a tally whose records shared ids, and a tally whose key
counted more records than there were client names to write. A tally too big
for the names is now refused when the suite is built.

## Rebuilding

Rebuild all three suites to use this. Sonnet's protocol and replay numbers
above are from the cases before these fixes.
