# UI sprint writer policy

Victor selected Claude Sonnet 5.5 as the primary implementer for all six
currently planned UI sprints on 2026-10-18. This is a task-specific writer
selection, not a temporary frontier phase or another concurrent writer.

The workstation Animal Uprising profile contains:

```toml
[role_overrides.ui]
tasks = ["UI-01", "UI-02", "UI-03", "UI-04", "UI-05", "UI-06"]
implementer = "Foundry-Kilo-Sonnet"
reviewer = "Foundry-Kilo-Deep-Review"
```

The default `[roles] implementer` stays `Foundry-Kilo-Local`. Intervening
security sprints and every other task retain that default. Add a future UI
sprint explicitly if its ID is outside this list. Overrides never approve
queue entries, expand sprint scope, waive checks, or alter publishing rules.

Model selection applies to implementation, repair, closeout, supported
same-session resume and helper feedback delivery. It is read from the profile
on coordinator startup. Resume registers the selected persona with Foundry
before inference. Session records retain previous writer personas so they
cannot become the independent final reviewer after a model change.

`Foundry-Kilo-Sonnet` is an existing strict Meridian persona, pinned and
allowlisted to `claude-sonnet-5-5`, with fallback refused. Kilo's inference
client needs this persona in its allowlist. No client scopes or persona
prompts need changing. The UI reviewer uses Deep-Review's existing Astra/Opus
route instead of the Sonnet writer. Other helper limits and quota policies
remain in force.

To hand off an active sprint: stop the coordinator owner, use the supported
pause, confirm Kilo has stopped that turn, validate both keys with doctor,
then resume the saved session. Confirm a new assistant message names the
Sonnet persona and Foundry reports Meridian / `claude-sonnet-5-5`. If the
switch is refused, keep the task paused; never launch a second writer.

Focused regression coverage is in
`tests/coordinator/test_task_role_overrides.py`: exact task matching,
restart persistence, invalid/overlapping rules, independent review, unchanged
queue authority, resume binding order, author history and helper delivery.
