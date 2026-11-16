"""Checking the code a model writes, before the answer reaches the client (v0.181.164).

A model that writes Luau, Python or JSON is often almost right: a missing `end`,
a member that does not exist, a trailing comma. The reader finds it, pastes it,
and comes back. This runs the checks the operator configured on the code in a
reply and, when one fails, hands the errors back to the model for another try —
so what reaches the client has been through the same check it would have failed.

A rule says which fenced languages it covers and how to check them:

    builtin:python   the code is compiled (never run) with Python's own compiler
    builtin:json     the text is parsed as JSON
    command          an operator-configured program is run with the code in a
                     file, e.g. `luau-lsp analyze {file}`, `selene {file}`,
                     `stylua --check {file}`
    service          the same, asked of the code-checks container over HTTP
                     (contrib/code-checks), so the router's image needs nothing

A check that cannot run — a missing program, a service that is not up, a broken
rule — is reported to the operator and the reply goes through untouched. Only
what a checker actually says about the code is ever sent back to the model.

Only the checker is run, never the model's code, and only a program the operator
named: the code goes into a private temporary file and the file's path is passed
as an argument. A checker that writes nothing and exits 0 is a pass; anything
else is a failure with its output as the reason.

Off until rules are configured (Routing → Code checks). A persona can be left
out, and so can work a coordinator task owns, whose gate checks it anyway.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
import time
from typing import Optional

FENCE = re.compile(r"^[ \t]*(?:```|~~~)[ \t]*([A-Za-z0-9_+#.-]*)[ \t]*\r?\n(.*?)(?:^[ \t]*(?:```|~~~)[ \t]*$)",
                   re.S | re.M)
MAX_BLOCK = 400_000           # a block larger than this is not checked
MAX_OUTPUT = 4000             # characters of a checker's output kept
EXT = {"luau": ".luau", "lua": ".lua", "python": ".py", "py": ".py", "json": ".json",
       "jsonc": ".json", "typescript": ".ts", "javascript": ".js", "yaml": ".yaml"}


class Unavailable(RuntimeError):
    """The check could not be run (a missing program, a service that is down, a
    rule with nothing to run). Never the code's fault, so never sent to the model."""


def blocks(text: str) -> list:
    """The fenced code blocks of a reply, as (language, code). A block with no
    language is reported as "" and is only checked by a rule that asks for it."""
    out = []
    for m in FENCE.finditer(text or ""):
        lang = (m.group(1) or "").strip().lower()
        code = m.group(2)
        if code.strip():
            out.append((lang, code))
    return out


def rules_for(cfg, lang: str) -> list:
    """The enabled rules that cover this language, in order."""
    out = []
    for r in getattr(cfg, "rules", None) or []:
        d = r if isinstance(r, dict) else r.model_dump()
        if not d.get("enabled", True):
            continue
        langs = [str(x).strip().lower() for x in (d.get("languages") or []) if str(x).strip()]
        if lang in langs or ("*" in langs and lang):
            out.append(d)
    return out


def on_for(cfg, persona: Optional[dict], task_bound: bool = False) -> bool:
    """Whether this request's code is checked here. A persona can be left out
    (`code_checks` = 0, e.g. a coding persona whose work a gate already checks),
    and `skip_gated_tasks` leaves out every request a coordinator task owns."""
    if not getattr(cfg, "enabled", False) or not (getattr(cfg, "rules", None) or []):
        return False
    if task_bound and getattr(cfg, "skip_gated_tasks", False):
        return False
    return bool((persona or {}).get("code_checks", 1))


def task_bound() -> bool:
    """True when a coordinator task owns this request (it was bound through the
    work API), so its own gate will check what comes out of it."""
    try:
        from . import client_auth
        c = client_auth.current() or {}
        return bool(c.get("work_task_id") or c.get("run_id"))
    except Exception:                                             # noqa: BLE001
        return False


def _suffix(lang: str) -> str:
    return EXT.get(lang, ".txt")


def _builtin_python(code: str) -> str:
    try:
        compile(code, "<model>", "exec")          # compiled, never run
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except ValueError as e:
        return str(e)
    return ""


def _builtin_json(code: str) -> str:
    try:
        json.loads(code)
    except ValueError as e:
        return str(e)
    return ""


def _env() -> dict:
    """What a checker is given. NOT the router's own environment: that holds the
    backends' API keys, and a checker has no business seeing them."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT")
    out = {k: v for k, v in os.environ.items() if k.upper() in keep}
    out["NO_COLOR"] = "1"
    return out


async def _command(rule: dict, code: str, lang: str) -> str:
    """Run the configured checker on the code in a file. Its output is the reason
    when it fails. The code itself is never executed by us."""
    argv = shlex.split(str(rule.get("command") or ""))
    if not argv:
        return ""
    d = tempfile.mkdtemp(prefix="foundry-check-")
    path = os.path.join(d, "snippet" + _suffix(lang))
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        argv = [a.replace("{file}", path).replace("{dir}", d) for a in argv]
        if not any("{file}" in str(a) or path in str(a) for a in shlex.split(str(rule["command"]))):
            argv.append(path)                     # no placeholder: the file goes last
        try:
            p = await asyncio.create_subprocess_exec(
                *argv, cwd=d, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=_env())
        except FileNotFoundError:
            raise Unavailable(f"the checker {argv[0]!r} is not installed on the router "
                              f"(contrib/code-checks runs the Luau ones in a container "
                              f"of their own)") from None
        except OSError as e:
            raise Unavailable(f"the checker could not be started: {e}") from None
        try:
            out, _ = await asyncio.wait_for(p.communicate(),
                                            float(rule.get("timeout_seconds") or 20))
        except asyncio.TimeoutError:
            p.kill()
            raise Unavailable(f"the checker did not finish within "
                              f"{rule.get('timeout_seconds') or 20}s") from None
        if p.returncode == 0:
            return ""
        text = (out or b"").decode("utf-8", "replace").strip().replace(path, "the file")
        return text[:MAX_OUTPUT] or f"the checker ended with status {p.returncode}"
    finally:
        for p_ in (path,):
            try:
                os.unlink(p_)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


async def _service(rule: dict, code: str, lang: str) -> str:
    """Ask the code-checks container (contrib/code-checks) to run the checker."""
    import httpx
    url = str(rule.get("url") or "").rstrip("/")
    if not url:
        raise Unavailable("no service address is set for this check")
    timeout = float(rule.get("timeout_seconds") or 20)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url + "/run", timeout=timeout + 10, json={
                "command": rule.get("command") or "", "code": code,
                "suffix": _suffix(lang), "timeout": timeout})
    except Exception as e:                                        # noqa: BLE001
        raise Unavailable(f"the code-checks service at {url} could not be reached "
                          f"({type(e).__name__})") from None
    try:
        body = r.json()
    except ValueError:
        raise Unavailable(f"the code-checks service answered with no JSON (HTTP {r.status_code})") \
            from None
    if r.status_code >= 400 or body.get("error"):
        raise Unavailable(f"the code-checks service refused this check: "
                          f"{body.get('error') or r.status_code}")
    if int(body.get("status") or 0) == 0:
        return ""
    return str(body.get("output") or "").strip()[:MAX_OUTPUT] or "the checker reported a problem"


async def run_one(rule: dict, code: str, lang: str) -> str:
    """The checker's complaint about this code, or "" when it is happy.
    Unavailable when the check itself could not run."""
    kind = str(rule.get("kind") or "command")
    if len(code) > MAX_BLOCK:
        return ""
    if kind == "python":
        return _builtin_python(code)
    if kind == "json":
        return _builtin_json(code)
    if kind == "service":
        return await _service(rule, code, lang)
    return await _command(rule, code, lang)


async def check(cfg, text: str, log=None) -> list:
    """Every problem found in a reply's code blocks: [{rule, language, reason,
    block, seconds}]. An empty list means nothing to fix."""
    found = []
    for i, (lang, code) in enumerate(blocks(text), start=1):
        for rule in rules_for(cfg, lang):
            t0 = time.monotonic()
            try:
                reason = await run_one(rule, code, lang)
            except Unavailable as e:
                reason = ""                   # the operator's problem, not the model's
                if log:
                    log(f"code check {rule.get('name')} could not run: {e}")
                continue
            except Exception as e:                                # noqa: BLE001
                reason = ""                                       # a broken rule never blocks a reply
                if log:
                    log(f"code check {rule.get('name')}: {type(e).__name__}: {e}")
            secs = time.monotonic() - t0
            if log:
                log(f"code check {rule.get('name') or rule.get('kind')} on the {lang or 'unlabelled'} "
                    f"block {i}: {'failed' if reason else 'passed'} in {secs:.1f}s")
            if reason:
                found.append({"rule": rule.get("name") or rule.get("kind") or "check",
                              "language": lang, "reason": reason, "block": i,
                              "seconds": round(secs, 2)})
                break                     # one failing rule per block is enough to go back
    return found


def feedback(problems: list) -> str:
    """What the model is told, so it can fix the code itself."""
    lines = ["The code you wrote did not pass this router's checks. Fix it and give the whole "
             "answer again, with the corrected code. Do not explain the fix at length; just "
             "make the code right.", ""]
    for p in problems:
        lines.append(f"- code block {p['block']} ({p['language'] or 'no language given'}), "
                     f"{p['rule']}:\n{p['reason']}")
    return "\n".join(lines)


def summary(problems: list) -> str:
    return "; ".join(f"block {p['block']} {p['rule']}" for p in problems)
