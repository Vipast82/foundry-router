---
name: roblox-luau
description: Write, review or debug Luau for Roblox - language semantics, --!strict types and the new type solver, module and object patterns, the task library, cleanup, errors and retries, buffers. Load for any Luau code; project skills (au-*) add their own conventions on top.
---

# Luau for Roblox

Load before writing or reviewing any Luau. Project conventions (an `au-*` skill, the repo's selene/StyLua config) win over this file.

## Language traps (most bugs)
- Only `false` and `nil` are falsy: `0`, `""` and `{}` are truthy. `x or fallback` replaces a valid `false`; use `if x == nil then` when false is a value.
- Equality never coerces (`0 == "0"` is false); not-equal is `~=`. NaN fails every comparison: test `x ~= x`.
- Arrays are 1-based; `#t` is only meaningful without nil gaps. Dictionary order is unspecified.
- `table.clone` is shallow; assigning a table copies the reference.
- String patterns are not regex. Prefer backtick interpolation `` `{name}: {score}` ``; build big strings with `table.concat`.
- Prefer `if a then b else c` expressions over `a and b or c` when `b` can be falsy.
- No JS/Python carry-overs: no `===`, `null`, `?.`, spread, comprehensions, 0-based indexing, exceptions-as-flow.

## Types
- `--!strict` for maintained code. Annotate boundaries (params, returns, exported types); let locals infer.
- New-solver features (`keyof`, `setmetatable<T,M>`, type functions, `read` members) need the new solver; an error there usually means the wrong solver, not wrong syntax.
- Tagged unions + discriminant narrowing over loose unions; `{[K]: V?}` when a lookup can miss.
- `::` casts narrow, never hide errors. Types are not runtime checks: remote args, DataStore values, attributes and HTTP bodies still need validation.

## Modules and objects
- Plain functions first; a module with private state for one subsystem owner; a metatable class only for many independent instances. Constructors `.new`, methods `:`.
- Keep module top level cheap and non-yielding; one bootstrap orders startup. No circular requires.
- Create an instance, set its properties, parent it last.

## Concurrency and cleanup
- `task.spawn` / `task.defer` / `task.delay` / `task.wait` / `task.cancel`; never legacy `wait()`, `spawn()`, `delay()`.
- Every connection, thread and created instance has an owner that disconnects / cancels / destroys it. Leaked connections are the most common Roblox memory leak.
- Prefer a real signal over polling; if you poll, choose and measure a cadence.

## Errors
- `pcall` returns `(ok, valueOrErr)`. Keep "call failed" separate from "call succeeded and returned nil".
- Retry only operations that are safe to repeat, with capped exponential backoff + jitter.

## Data
- `buffer` for binary data (0-based offsets, fixed widths). `vector` for hot 3D math when the project uses it.

## References
- `references/luau-core.md` - semantics and translation traps (roblox-brain).
- `references/luau-types.md` - types, generics, narrowing, solver details (roblox-brain).
- `references/luau-patterns.md` - module shapes, lifecycles, signals, scheduling (roblox-brain).
- `references/luau-quick.md`, `luau-stdlib-cheatsheet.md`, `luau-type-system.md` - compact cheat sheets and a legacy-to-current API table (EL4CTEO).
