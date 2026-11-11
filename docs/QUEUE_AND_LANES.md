# Queue classes and lanes

Since v0.181.118. Code: `foundry_router/queue_class.py`, `foundry_router/lanes.py`, `pool/internal.py` (`FairGate`), `budget.py` (`reserve_wait`).

## Why

Strata serves one stream. A sprint holds it for hours, because each of its calls ends in a tool call and the session keeps its stream between calls (interleaving another conversation would evict the sprint's prompt cache, a re-read of minutes). Everything else waits behind it. Two things decide how that wait goes:

1. **Queue classes** put the waiters in order.
2. **Lanes** let short jobs skip the wait on a second, smaller backend.

## Queue classes

| Class | Who | Waits (300 s backend) | Aging |
|---|---|---|---|
| A interactive | a person: chat front ends, Cline, operator chat | 300 s | always first |
| B required | coordinator work a sprint can't continue without | 600 s | none |
| C optional | coordinator helper lanes, exploration, research, pre-work | 900 s | counts as B after 10 minutes |
| D background | scheduled pulses, indexing, anything unattended | 1800 s | up one class every 15 minutes, never above B |

A request's class comes from, in order:

1. the coordinator purpose bound to its session (`orchestration.REQUIRED_PURPOSES` are B, `OPTIONAL_PURPOSES` are C);
2. the persona's `queue_class`;
3. the client's `queue_class` (Access, Clients and Keys);
4. `queue.default_class` (A).

`x-foundry-priority: A|B|C|D` (or `interactive`, `required`, `optional`, `background`) lowers a request's own class. It never raises it.

Where a call waits, and what the class does there:

- **The stream wait** (backends with `helpers_allowed`): the best class waiting takes the next free stream; a newcomer goes behind those already waiting in its class or a better one. A session that already holds a stream is never queued.
- **The slot gate** (`max_concurrent`): best class first, then clients take turns inside the class.

A queued streaming call reports its place every 5 seconds. Clients that show Foundry's status lines read "queued behind 2 for its stream on strata (class B)". That report counts as proof of life, so queue time doesn't run the stall clock.

Settings: Access, Helpers and Limits (queue classes card), or `GET/POST /admin/api/setup/queue`. Turning queue classes off makes every waiter class A with the backend's own timeout.

## Lanes

A lane is a backend with `lane: true` (Access, Helpers and Limits, the "lane for short jobs" column).

- Its models are left out of automatic routing. A persona reaches a lane by naming the lane's model in its allowlist, or through divert.
- A request must fit: at most `lane_max_prompt_tokens` (3,000, estimated), no tools unless `lane_allow_tools`, and no earlier assistant turn unless the conversation is pinned to the lane.
- On the lane, replies are capped at `lane_max_output_tokens` (400) and thinking is off unless `lane_thinking`.
- Failover never puts a request on a lane it doesn't fit.

### Try, then divert

Persona setting **divert when busy**. When the local model a request would use has no free stream or slot right now:

- a **strict** persona uses the next free local model in its own chain (paid models keep their places);
- a **flexible** persona uses a free local model on its allowlist, else any free lane the request fits.

Nothing free: it waits in the queue. Flexible personas with **escalate when local busy** then try a paid model, gated by the usage guardrail.

### Pinning

A conversation served by a lane stays on that lane while it still fits. Persona setting **pin conversation** keeps every direct-mode conversation on the model that answered its first turn, while that model is on the route and allowed. Pins live in memory for six hours.

## Hold for a backend coming back

A strict persona whose only models sit on a backend that just went unhealthy waits `queue.recovery_hold_seconds` (90 s; class A waits `recovery_hold_interactive_seconds`, 20 s) and is routed normally if the backend returns. After that the usual refusal follows.

## Setting up the brain lane

1. Backends, Pool: add the brain host as a backend (`openai-compatible`, flavor `llamacpp`, `http://192.168.0.111:8081/v1`), max concurrent 1, queue timeout 60.
2. Access, Helpers and Limits: tick **lane** on it.
3. Make a `Foundry-Utility` persona: direct mode, strict, chain = the lane model then the Strata model, **divert when busy** on, queue class D if it serves scheduled jobs.
4. Give chat personas **divert when busy** if short chats should use the lane while a sprint holds Strata.

The brain keeps using the same host through its own client; the lane's slot gate does not count the brain's routing calls.
