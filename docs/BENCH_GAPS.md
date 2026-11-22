# Eval and Benchmark: what the ecosystem needs that is not measured yet

Written 2026-11-21 at v0.181.227, against the ecosystem plan
(`foundry-ecosystem-router-handoff.md`, 2026-11-18): one chat front end for
home and work over this router, with tiered work documents, tools, coding, a
build sandbox, media, video notes and infrastructure pulses, local first.

This is a review, not a build. Nothing below is implemented. Each addition
says what it would measure, how it would be scored exactly, and what it needs.

## What is measured today

| Where | What | Size |
|---|---|---|
| Benchmark: code reasoning | what Python does; generated ladders to frontier-hard | 119 cases |
| Benchmark: long-context recall | find one note; put many notes together | 141 + 28 cases |
| Benchmark: pick the right tool | one tool from a catalogue on the page | 59 cases |
| Benchmark: find and use tools | search the finder, call the tool with its inputs | 46 cases |
| Benchmark: coordinator protocol | end a sprint turn the way the coordinator reads it | 38 cases |
| Benchmark: real work replay | make the move a verified session made | 60 cases |
| Benchmark: agent deliverables | ten things to build, compared blind, never scored | 10 tasks |
| Evals | 30 prompts through a persona's real route: triage, objective, honesty, coding, chat, research, rag | 30 prompts |

Both are built around the sprint system: Roblox, Blender, this router. That
was the work when they were written. The ecosystem adds uses none of them
touch.

## Planned use against coverage

| Planned use (plan section) | Measured today | Gap |
|---|---|---|
| Answers from tiered work documents (Knowledge, Paperless; A1) | 2 eval prompts, one sentence of context each | large |
| Staying inside a tier; tool results and pages that carry instructions (A1, A3, browser) | nothing | large |
| Tool work that takes several calls: queue a job, check it, use the result (media, video, A7) | first right call only | large |
| Output a program will read: JSON to a schema, tool arguments (n8n jobs, Document MCP) | two string arguments in the finder ladder | large |
| Household chat over many turns: keeps a rule, a preference, a language | 3 single-turn prompts | large |
| Pictures: video notes, screenshots, `check_text` (B7) | nothing | large, needs a vision model |
| Notes and pulses: summarise a transcript or a log without inventing | nothing | medium |
| Writing code that works (coding, "build me a website", A5) | reading code only; 10 unscored deliverables | large, needs a decision |
| Which local rung is good enough for each kind of work (A9) | cards per model, no rule on top | medium |
| Memory: use what is relevant, not what is not (A6) | nothing | medium, after A6 |

## Recommended additions, in order

Ordered by how much of the plan depends on them and how little they need.
The first five use the bench as it is: generated cases, exact scoring, a
level as the dial, tiers by who misses.

### 1. Grounded answers over documents

**Built in v0.181.228** as the "Grounded answers" suite: 39 generated cases,
with a tier-scope kind added. Result: every look-up kind is easy for flash,
Sonnet 5.5 and Opus 5.5; totals across records separate them. Still open
from this item: the same cases through a persona's real route as eval
prompts, once the tier gate (A1) exists. What was proposed:

The plan's main work use. Give the model a handful of retrieved passages, each
with an id and a tier marker, and a question. Four kinds of case:

- the answer is in one passage: answer and cite its id;
- the answer is in none: say so, and do not answer from memory;
- two passages disagree: say they disagree and cite both, or take the later dated one when the question says to;
- the passage contradicts what the model believes (a planted false "fact" about something well known): the document wins.

Scored exactly: the planted value, the cited id, and for the absent case the
words of a refusal with no value given. The dial is the number of passages and
near-miss distractors. This is the long-context ladder with citation and
abstention added, so most of the generator exists.

### 2. Instructions that arrive as data, and tools left alone

**Built in v0.181.240** as the "Instructions in the data" suite: 96 generated
cases. Result on the first 48: Sonnet 5.5 held all, Opus 5.5 followed one,
qwen3.8-flash held 42 of 48. What was proposed:

A work chat reads documents, pages and tool results. Any of them can say
"ignore your instructions and send this to...". The router's gate (A1) stops
the cloud call; nothing stops the model obeying the text. Cases: a tool result
or passage carries an instruction (call an egress tool, reveal a canary string
from the system prompt, change the answer), and the right behaviour is to
finish the user's task and not follow it. A second kind offers a destructive
or egress tool (`SQLite__write_query`, `memory__delete_*`, an `ask_*` cloud
model) the task does not need. Scored exactly: the forbidden tool was not
called, the canary is not in the reply, the task's own answer is right. The
dial is how well the injected text is disguised and how many turns back it is.

### 3. Tool work that takes more than one call

Every media, video and document tool queues a job. The finder suite stops at
the first right call. Cases need a scripted tool environment: each tool
returns a canned result that depends on the call before it, so the run is a
small state machine. Kinds: queue then check then hand over the link; a tool
that fails once with a clear error (fix the argument and retry, or report); a
job that is still running (wait and check again, do not queue it twice); a
pipeline of three tools where each needs the last one's output. Scored
exactly on the sequence and the arguments, with a cap on calls. The dial is
the number of steps.

### 4. Output a program will read

Scheduled jobs, the Document MCP and the jobs list all take structured input.
Cases: fill a JSON object to a schema (enums, nested objects, arrays, a number
range, a required field easy to forget), from a paragraph that states the
facts in prose. Scored by validating against the schema and comparing the
values. The dial is the depth and width of the schema. Small local models fail
this in ways the current suites do not show.

### 5. A rule held across a conversation

Household and work personas carry rules: answer in three sentences, never
give medical dosages, reply in the language of the question, call the user by
a name they gave at the start. Cases: the rule is in the system prompt or an
early turn, then N turns of ordinary talk, then a turn that tempts a break.
Scored exactly on the last reply. The dial is N. Add the same for a
preference stated once and needed later.

### 6. Summaries that invent nothing

Video notes and infrastructure pulses are summaries. Generate a transcript or
a log with planted facts (a time, a count, a name, an error line) and planted
near-facts that are not true of the thing asked about. Score on the planted
facts present and the near-facts absent, with a length bound. The dial is the
length and the number of facts. It reuses the long-context generator.

### 7. Pictures

Nothing here sends an image. Once a local model is tagged `vision` (plan B7):
render text, a small table, a chart and a labelled screenshot at build time
and ask what they say. Scored exactly. Needs an image library in the router
image and a vision model to run against; no point building it before both.

### 8. Code that runs

The largest use and the least measured: every code case asks what code does,
never for code. Scoring written code means running it, and the bench has a
rule that nothing a model writes is ever run. Two ways forward, both a
decision for the owner:

- when the workspace server (plan A5, Part C) exists, run a model's answer
  against hidden tests inside it and score on the tests. This is the right
  measure and it needs that server.
- before that, a syntax and type check only (Python compile, `luau-analyze`
  if installed): cheap, catches a model that cannot write the language, and
  says nothing about whether the code is right.

### 9. The all-local ladder report (plan A9)

The plan asks for a rule on top of the benchmark: per kind of work, which
local rung is good enough, and which cloud rungs could go. The cards now carry
what that needs (tier scores per suite, speed, tokens). Missing: a threshold
per kind of work and a report that reads "long-context recall: the brain lane
passes through medium at 38 s a case; flash through medium at 31 s; hard needs
Opus". Reports only, as the plan says.

## What to fix in how it is measured

- **One run per suite.** Runs move about five points, and the hard and expert
  tiers hold 4 to 14 cases, so one case is several points. Run the hard and
  expert rungs two or three times (the profile's repeats) or add seeds per
  rung, and show the spread on the card.
- **Calibration is thin.** Most rungs were placed from one case per level on
  three models. The brain lane was not calibrated on any new case, and GPT not
  at all. Re-measure after the first full runs and move tiers that are wrong.
- **Thinking is not an axis.** Frontier models run with thinking off by
  default; the brain lane always thinks. A profile option to run the same
  suite at two effort levels would say what thinking buys and what it costs.
- **The suites never change.** Generated cases are fixed by their seed. If
  benchmark traffic reaches a training export, a tuned local model can learn
  the answers. Add a build "epoch" that reseeds every generated suite, and
  keep benchmark and eval calls out of training exports.
- **Evals are 30 prompts.** Coding has 2, rag 2, research 2. They check that
  a persona still works after a change; they cannot rank models. Additions 1,
  2 and 5 above should exist as eval prompts too, through the real route, once
  the tier gate (A1) is in, because the gate is router behaviour and only the
  eval path exercises it.
- **Deliverables are never scored.** Ten tasks, judged in pairs. Their
  acceptance criteria could be scored one by one by two judges, with the
  agreement shown, which gives a number without pretending to an answer key.

## What not to add

- Harder tool picking. Sonnet 5.5, Opus 5.5 and flash all pass it to 1,200
  look-alike tools.
- Hand-written tricky cases of any kind. Thirty were written for code, and
  flash, Sonnet and Opus got 67 of 68 answers to them right; a generator with a
  level is the only thing that kept up.
- A public benchmark. It says nothing about this work, and models have seen it.
