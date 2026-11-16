# code-checks — the Luau checkers, in a container of their own

Foundry Router can check the code a model writes before the answer reaches you
(Routing and brain → Code checks). `python` and `json` checks need nothing, but
Luau needs `luau-lsp`, `selene` and `stylua`. Rather than put them in the
router's image, they live here: a small service the router asks over HTTP, so
the router is unchanged and the whole thing is optional.

## Start it

```
docker compose --profile code-checks up -d --build
```

Without `--profile code-checks` it is not started, and nothing else notices.
It has no port on the host: only the router's own network can reach it.

## Point a rule at it

Routing and brain → Code checks → Add a check:

| field | value |
|---|---|
| name | `luau-analyze` |
| code blocks labelled | `luau`, `lua` |
| how | `service` |
| service address | `http://code-checks:8099` |
| command | `luau-lsp analyze {file}` |

`selene {file}` and `stylua --check {file}` are rules of their own. "Try it"
on that page runs one against the service with a snippet, so you can see what it
says before you turn checking on.

## What it does

```
POST /run  {"command": "luau-lsp analyze {file}", "code": "...",
            "suffix": ".luau", "timeout": 20}
        -> {"status": 0, "output": ""}         the code passed
        -> {"status": 1, "output": "..."}      the checker complained; this text
                                               is what the model is told
GET /health -> {"ok": true, "programs": {"luau-lsp": "...", ...}}
```

The code is written to a file in a temporary directory, the checker runs on that
file, and the directory is removed. Only `luau-lsp`, `selene` and `stylua` are
run, never a shell, and never the code itself.

## Why it is a separate container

What this service looks at was written by a model. It holds no credentials, has
no volumes, drops every Linux capability and cannot write outside `/tmp`. If you
later add a checker that *runs* what it is given — a headless Blender script, a
test — this is where that belongs: add the program to the image and to `ALLOWED`
in `server.py`, and it is still the only thing that runs it.

## Versions

The three tools are pinned in the `Dockerfile` with the checksums GitHub
publishes for those releases, so a changed file fails the build. To move to a
newer release, change the version and its `*_SHA256` together:

```
docker compose build --build-arg STYLUA_VERSION=2.7.0 --build-arg STYLUA_SHA256=<digest> code-checks
```

The digest is in the release's asset listing
(`https://api.github.com/repos/<owner>/<repo>/releases/latest`, field `digest`).
