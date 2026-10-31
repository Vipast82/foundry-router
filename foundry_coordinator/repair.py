"""Isolated Foundry-Router repair worktrees for the lead sprint manager.

A coordinator-class repair is never made in the running checkout or in the
game repository. `RepairWorkspace` creates a separate `git worktree` of the
Foundry-Router repository on a new branch `lead/<case_id>` under the repair
root, and it is the only place a lead repair session may edit. The repair
root itself may not be, contain or lie inside a protected checkout (the
running checkout, the router checkout, the game workspace).

Before a candidate can become an upgrade request, `validate()` requires:
  * a regression test node id (`path::test`) that FAILS (a test failure, not
    a collection error) at the base commit and PASSES at the candidate
    (`reproduces`), proven from pytest's junit report;
  * every existing test the candidate modified still collected and passing
    at the candidate (the set collected at base);
  * the focused and a non-empty broader suite passing in the worktree
    (`run_tests`), each named id collected and passed (not skipped or
    deselected);
  * an independent review by a reviewer persona that is NOT the lead persona,
    validated with `review.validate` (APPROVED only without blocking findings);
  * a manifest naming the changed files, migrations, tests, review and the
    candidate commit.

Test ids are data, never pytest options: only `safe/rel/path[::node...]` ids
are accepted, and they are passed after `--`. Every test subprocess runs with
a scrubbed environment (no keys, tokens, secrets or FOUNDRY_* settings; HOME
and TEMP in a throwaway sandbox).

`commit()` refuses a dirty scope (changed files outside the declared repair
scope) and changed test expectations: a pre-existing test file whose test
functions or assertions were deleted, modified, shadowed by a duplicate
definition, neutered by an early return / try-except, re-decorated or newly
skipped; and any change to pytest configuration (conftest.py, pytest.ini,
the pytest sections of pyproject.toml / setup.cfg / tox.ini, `__init__.py`
under test dirs). A changed expectation is accepted only when it is declared
AND carries an operator approval (`approved_test_changes`, filled by the lead
only from an answered operator decision). New test files need neither.
Every model output here is untrusted data: nothing a model says marks a check
PASS.
"""

from __future__ import annotations

import ast
import collections
import configparser
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from . import review as R

_CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
DIFF_CHARS = 150_000          # the review packet's diff cap
LOG_CHARS = 20_000            # per test-run log kept in results
DEFAULT_TIMEOUT = 900.0
DEFAULT_BROADER = ("tests/coordinator",)   # the broader suite when none is named

REPAIR_REVIEW_SYSTEM = """You are an independent code reviewer for one Foundry-Router coordinator repair.
You review only the diff, files and evidence in the packet. You did not write this change.
Return ONLY a JSON object (no prose, no code fence) with exactly these keys:
  sprint_id (the case id given), project_id, reviewed_commit_sha (the full candidate commit given),
  verdict: "APPROVED" | "CHANGES_REQUIRED" | "BLOCKED",
  findings: [{"severity": "blocker|major|minor|info", "file": "<repo-relative path>", "line": <int or null>, "message": "..."}],
  evidence_inspected: [...], missing_evidence: [...], required_repairs: [...], residual_risks: [...]
APPROVED only if the change fixes the stated defect, stays in its declared files, keeps every existing test expectation,
and the regression test in the diff exercises the defect. CHANGES_REQUIRED when specific repairs would fix it.
BLOCKED when it needs a human decision. You cannot change the scope or override a failed check; say what you saw."""


class RepairError(RuntimeError):
    """A refused repair operation. `violations` lists the precise reasons."""

    def __init__(self, message: str, violations: Optional[list] = None):
        super().__init__(message)
        self.violations = list(violations or [])


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _inside(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:                     # different drives on Windows
        return False


def _git(cwd: str, *args: str, check: bool = True, timeout: float = 120,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=env, stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise RepairError(f"git {' '.join(args[:3])} failed in {cwd}: "
                          f"{(r.stderr or r.stdout).strip()[:800]}")
    return r


def _running_checkout() -> str:
    """The checkout this coordinator package runs from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    parts = p.split("/")
    base = parts[-1]
    return (any(d in R.TEST_DIRS for d in parts[:-1]) or base.startswith("test_")
            or base.endswith("_test.py") or base == "conftest.py")


def scope_match(path: str, scope: Iterable[str]) -> bool:
    """A path is in scope when it equals an entry, lies under an entry ending
    in '/', or matches an entry as a glob."""
    p = path.replace("\\", "/")
    for e in scope or []:
        e = str(e).replace("\\", "/").strip()
        if not e:
            continue
        if p == e or (e.endswith("/") and p.startswith(e)) or fnmatch.fnmatchcase(p, e):
            return True
    return False


# --------------------------------------------------------------------------- sandboxed pytest

_SECRET_NAME = re.compile(r"(_KEY|_KEYS|_TOKEN|_TOKENS|_SECRET|_SECRETS|_PASSWORD|_PASSWD|_PWD|_PAT"
                          r"|_CREDENTIALS?)$|SECRET|PASSWORD|TOKEN|API_?KEY|PRIVATE_KEY|ASKPASS"
                          r"|^SSH_AUTH_SOCK$",
                          re.IGNORECASE)
_DROP_ENV = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
             "PYTHONINSPECT", "PYTHONUSERBASE")


def sandbox_env(sandbox: str, base: Optional[dict] = None) -> dict:
    """The environment for a test subprocess: the parent's, minus every
    credential-looking name (*_KEY, *_TOKEN, *_SECRET, *_PASSWORD, ...), every
    FOUNDRY_* setting and pytest/python injection variables; HOME, the user
    profile and TEMP point into `sandbox`. PATH, SYSTEMROOT and the venv
    (VIRTUAL_ENV) are kept."""
    src = dict(os.environ if base is None else base)
    env = {}
    for k, v in src.items():
        u = k.upper()
        if u.startswith("FOUNDRY_") or u in _DROP_ENV or _SECRET_NAME.search(u):
            continue
        env[k] = v
    os.makedirs(sandbox, exist_ok=True)
    # 2026-10-30 (GF-RT1, five lead repair candidates rejected): with HOME moved into the
    # sandbox, tests that find a tool in the user's rokit storage (~/.rokit/tool-storage, e.g.
    # Lune) fell back to the rokit shim, which only runs inside a folder with a rokit.toml,
    # so test_bridge_wrapper_runs_every_suite_shape_under_luau failed on every candidate. The
    # storage folder (binaries, no credentials) stays reachable as ROKIT_TOOL_STORAGE.
    home = src.get("USERPROFILE") or src.get("HOME") or ""
    store = os.path.join(home, ".rokit", "tool-storage") if home else ""
    if store and os.path.isdir(store) and "ROKIT_TOOL_STORAGE" not in env:
        env["ROKIT_TOOL_STORAGE"] = store
    for k in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR"):
        env[k] = sandbox
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_NODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]\s]*\])?$")


def parse_test_id(tid, *, require_node: bool = False) -> tuple[str, list[str]]:
    """(path, [node parts]) of a safe test id `rel/path[::Class]::test[param]`,
    or RepairError. Never an option (no leading '-'), never absolute, never
    '..', no whitespace."""
    if not isinstance(tid, str) or not tid or tid != tid.strip() or any(c.isspace() for c in tid):
        raise RepairError(f"invalid test id {tid!r}")
    parts = tid.split("::")
    path, nodes = parts[0].replace("\\", "/"), parts[1:]
    segs = path.split("/")
    if (not _PATH_RE.match(path) or path.startswith("-") or ".." in segs or "" in segs[:-1]
            or re.match(r"^[A-Za-z]:", path)):
        raise RepairError(f"invalid test id {tid!r}: only safe repo-relative paths")
    if path.endswith("/"):
        path = path.rstrip("/")
    for n in nodes:
        if not _NODE_RE.match(n):
            raise RepairError(f"invalid test id {tid!r}: bad node {n!r}")
    if require_node and not nodes:
        raise RepairError(f"test id {tid!r} must name one test (path::test)")
    if nodes and not path.endswith(".py"):
        raise RepairError(f"invalid test id {tid!r}: a node id needs a .py file")
    return path, nodes


def check_test_ids(ids: Iterable[str], *, require_node: bool = False) -> list[str]:
    """The normalized ids, or RepairError for the first unsafe one."""
    out = []
    for tid in ids or []:
        path, nodes = parse_test_id(tid, require_node=require_node)
        norm = "::".join([path] + nodes)
        if norm not in out:
            out.append(norm)
    return out


def _junit_cases(path: str) -> Optional[list[dict]]:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return None
    out = []
    for tc in tree.iter("testcase"):
        outcome = "passed"
        for child in tc:
            if child.tag == "failure":
                outcome = "failed"
                break
            if child.tag == "error":
                outcome = "error"
                break
            if child.tag == "skipped":
                outcome = "skipped"
        out.append({"classname": tc.get("classname") or "", "name": tc.get("name") or "",
                    "outcome": outcome})
    return out


def _module_of(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    return p.replace("/", ".")


def cases_for(tid: str, cases: list[dict]) -> list[dict]:
    """The junit test cases a test id names (a directory or file: every test
    under it; a node: that test, every parametrization of it)."""
    path, nodes = parse_test_id(tid)
    mod = _module_of(path)
    if not nodes:
        return [c for c in cases if c["classname"] == mod or c["classname"].startswith(mod + ".")]
    *klass, fn = nodes
    want = ".".join([mod] + klass)
    return [c for c in cases if c["classname"] == want
            and (c["name"] == fn or ("[" not in fn and c["name"].startswith(fn + "[")))]


def id_problems(ids: list[str], cases: Optional[list[dict]]) -> list[str]:
    """Why the named ids did not all run and pass ([] = they did). A node id
    must be collected and passed (never skipped); a file or directory must
    collect at least one test, pass at least one and fail none."""
    if cases is None:
        return ["no junit report (pytest did not run the tests)"]
    out = []
    for tid in ids:
        got = cases_for(tid, cases)
        if not got:
            out.append(f"{tid}: not collected")
            continue
        bad = [c for c in got if c["outcome"] in ("failed", "error")]
        if bad:
            out.append(f"{tid}: {len(bad)} failed/errored ({bad[0]['name']})")
        elif "::" in tid and any(c["outcome"] != "passed" for c in got):
            out.append(f"{tid}: skipped, not passed")
        elif not any(c["outcome"] == "passed" for c in got):
            out.append(f"{tid}: nothing passed (all skipped)")
    return out


def run_pytest(python: str, cwd: str, ids: list[str], timeout: float, *,
               collect_only: bool = False) -> dict:
    """One sandboxed pytest subprocess over validated ids (after `--`, so an
    id is never an option), with a junit report. {ids, rc, ok, timed_out,
    seconds, log, cases, problems, collected}. ok = rc 0 and every named id
    collected and passed (collect_only: rc 0)."""
    ids = check_test_ids(ids)
    sandbox = tempfile.mkdtemp(prefix="foundry-pytest-")
    junit = os.path.join(sandbox, f"junit-{uuid.uuid4().hex[:8]}.xml")
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", cwd,
           f"--junitxml={junit}"]
    if collect_only:
        cmd.append("--collect-only")
    cmd += ["--", *ids]
    t0 = time.time()
    try:
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout,
                               env=sandbox_env(os.path.join(sandbox, "home")),
                               stdin=subprocess.DEVNULL)
            rc, log = r.returncode, (r.stdout or "") + (r.stderr or "")
            out_text = r.stdout or ""
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc, log, out_text = None, out + f"\n[timeout after {timeout}s]", ""
        cases = _junit_cases(junit) if rc is not None else None
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    res = {"ids": list(ids), "rc": rc, "timed_out": rc is None,
           "seconds": round(time.time() - t0, 2), "log": log[-LOG_CHARS:], "cases": cases,
           "problems": [], "collected": []}
    if collect_only:
        res["collected"] = [ln.strip() for ln in out_text.splitlines()
                            if "::" in ln and not ln.startswith((" ", "<", "="))]
        res["ok"] = rc == 0
        return res
    res["problems"] = id_problems(ids, cases) if rc is not None else ["timed out"]
    res["ok"] = rc == 0 and not res["problems"]
    return res


# --------------------------------------------------------------------------- test expectations

_SKIP_NAMES = ("skip", "skipif", "xfail", "skipTest")
_SUPPRESS_NAMES = ("suppress",)
PYTEST_CONFIGS = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")


def _call_name(node: ast.AST) -> str:
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _dump(n) -> str:
    return ast.dump(n, include_attributes=False)


def _asserts_skips(root) -> tuple[collections.Counter, collections.Counter]:
    asserts, skips = collections.Counter(), collections.Counter()
    for n in ast.walk(root):
        if isinstance(n, ast.Assert):
            asserts[_dump(n)] += 1
        elif isinstance(n, ast.Call):
            name = _call_name(n)
            if name.startswith("assert") or name in ("raises", "warns", "fail"):
                asserts[_dump(n)] += 1
            if name in _SKIP_NAMES:
                skips[_dump(n)] += 1
        elif isinstance(n, ast.Attribute) and n.attr in _SKIP_NAMES:
            # bare decorators such as @pytest.mark.skip / @unittest.skip
            skips[_dump(n)] += 1
    return asserts, skips


def _test_defs(tree) -> tuple[dict, collections.Counter]:
    """{qualified test name: [definitions in order]} and the count of each
    test class name per scope (a repeated name shadows the earlier one)."""
    defs = collections.defaultdict(list)
    classes = collections.Counter()

    def walk(body, prefix=""):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                defs[prefix + n.name].append(n)
            elif isinstance(n, ast.ClassDef):
                classes[prefix + n.name] += 1
                walk(n.body, prefix + n.name + ".")

    walk(tree.body)
    return defs, classes


def _fn_profile(fn) -> dict:
    asserts, _ = _asserts_skips(fn)
    returns = sum(1 for n in ast.walk(fn) if isinstance(n, ast.Return))
    tries = sum(len(n.handlers) for n in ast.walk(fn) if isinstance(n, ast.Try))
    suppress = sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and _call_name(n) in _SUPPRESS_NAMES)
    return {"asserts": asserts, "returns": returns, "tries": tries, "suppress": suppress,
            "decorators": [_dump(d) for d in fn.decorator_list], "dump": _dump(fn)}


def _test_shape(src: str) -> dict:
    """Test functions, assertion fingerprints and skip markers of one file."""
    tree = ast.parse(src)
    defs, classes = _test_defs(tree)
    asserts, skips = _asserts_skips(tree)
    return {"funcs": set(defs), "defs": defs, "classes": classes, "asserts": asserts,
            "skips": skips}


def test_expectation_changes(path: str, old: Optional[str], new: Optional[str]) -> list[str]:
    """Why a change to a test file that existed at base changes its
    expectations ([] when it only adds tests/assertions)."""
    if old is None:
        return []                                         # a new file: adds expectations
    if new is None:
        return [f"{path}: existing test file deleted"]
    if not path.endswith(".py"):
        return [] if old == new else [f"{path}: existing test data/fixture file modified"]
    if old == new:
        return []
    try:
        a = _test_shape(old)
    except SyntaxError:
        return []                                         # unparsable at base: nothing to keep
    try:
        b = _test_shape(new)
    except SyntaxError as e:
        return [f"{path}: no longer parses ({e.msg} line {e.lineno})"]
    out = []
    gone = sorted(a["funcs"] - b["funcs"])
    if gone:
        out.append(f"{path}: test function(s) removed or renamed: {', '.join(gone[:10])}")
    lost = sum((a["asserts"] - b["asserts"]).values())
    if lost:
        out.append(f"{path}: {lost} existing assertion(s) deleted or changed")
    added_skips = sum((b["skips"] - a["skips"]).values())
    if added_skips:
        out.append(f"{path}: {added_skips} skip/xfail marker(s) added")
    dups = sorted(n for n, ds in b["defs"].items() if len(ds) > len(a["defs"].get(n) or [1]))
    dups += sorted(n for n, k in b["classes"].items() if k > max(1, a["classes"].get(n, 0)))
    if dups:
        out.append(f"{path}: duplicate definition(s) shadow an existing test: "
                   f"{', '.join(dups[:10])}")
    for name in sorted(a["funcs"] & b["funcs"]):
        fa, fb = _fn_profile(a["defs"][name][-1]), _fn_profile(b["defs"][name][-1])
        if fa["dump"] == fb["dump"]:
            continue
        why = []
        if fa["asserts"] - fb["asserts"]:
            why.append("assertions removed")
        if fb["returns"] > fa["returns"]:
            why.append("early return added")
        if fb["tries"] > fa["tries"] or fb["suppress"] > fa["suppress"]:
            why.append("exception handling added around its checks")
        if fa["decorators"] != fb["decorators"]:
            why.append("decorators changed")
        if why:
            out.append(f"{path}: existing test {name} neutered: {', '.join(why)}")
    return out


def is_pytest_config(path: str) -> bool:
    """A file that changes what pytest collects or how (not a test file
    itself): conftest.py, pytest.ini, pyproject.toml, setup.cfg, tox.ini
    anywhere, and __init__.py under a test directory."""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    if base in PYTEST_CONFIGS:
        return True
    return base == "__init__.py" and any(d in R.TEST_DIRS for d in parts[:-1])


def _pytest_sections(path: str, text: Optional[str]):
    """The pytest-relevant part of a config file (comparable), or a marker
    that it can't be parsed."""
    if text is None:
        return None
    base = path.replace("\\", "/").split("/")[-1]
    try:
        if base == "pyproject.toml":
            import tomllib
            d = tomllib.loads(text)
            return {"tool.pytest": (d.get("tool") or {}).get("pytest"),
                    "pytest11": ((d.get("project") or {}).get("entry-points") or {}).get("pytest11")}
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(text)
        return {s: dict(cp.items(s)) for s in cp.sections() if "pytest" in s.lower()}
    except Exception as e:                                   # noqa: BLE001
        return {"unparsable": f"{type(e).__name__}"}


def config_expectation_changes(path: str, old: Optional[str], new: Optional[str]) -> list[str]:
    """Why a change to a pytest configuration file may change which tests run
    or how they're judged ([] = it can't)."""
    if old == new or not is_pytest_config(path):
        return []
    base = path.replace("\\", "/").split("/")[-1]
    if base in ("pyproject.toml", "setup.cfg", "tox.ini"):
        a, b = _pytest_sections(path, old), _pytest_sections(path, new)
        empty = (None, {}, {"tool.pytest": None, "pytest11": None})
        if a == b or (a in empty and b in empty):
            return []
        return [f"{path}: pytest configuration changed"]
    what = "added" if old is None else "deleted" if new is None else "modified"
    return [f"{path}: pytest configuration {what} (conftest/pytest.ini/test package)"]


# --------------------------------------------------------------------------- workspace

class RepairWorkspace:
    """Isolated Foundry-Router worktrees under `root`, branched from
    `router_repo`. `python` runs the tests (default: this interpreter, the
    coordinator's venv). `broader_default` is the broader suite used when a
    repair names none (the first entries that exist in the worktree; else
    `tests`)."""

    def __init__(self, root: str, router_repo: str, *, python: Optional[str] = None,
                 running_checkout: Optional[str] = None, protected: Iterable[str] = (),
                 broader_default: Iterable[str] = DEFAULT_BROADER):
        self.root = os.path.abspath(os.path.expanduser(root))
        top = _git(os.path.abspath(router_repo), "rev-parse", "--show-toplevel").stdout.strip()
        self.router_repo = os.path.abspath(top)
        self.python = python or sys.executable
        self.running_checkout = os.path.abspath(running_checkout or _running_checkout())
        # never operated on: the running checkout, the router checkout itself,
        # and anything the caller names (the game workspace)
        self.protected = [self.running_checkout, self.router_repo] + [os.path.abspath(p)
                                                                      for p in protected if p]
        for p in self.protected:
            if _norm(self.root) == _norm(p) or _inside(p, self.root):
                raise RepairError(f"repair root {self.root} must not be or contain the "
                                  f"protected checkout {p}")
            if _inside(self.root, p):
                raise RepairError(f"repair root {self.root} is inside the protected "
                                  f"checkout {p}")
        os.makedirs(self.root, exist_ok=True)
        cd = _git(self.router_repo, "rev-parse", "--git-common-dir").stdout.strip()
        self._common = _norm(cd if os.path.isabs(cd) else os.path.join(self.router_repo, cd))
        self.broader_default = [b for b in (broader_default or []) if b]
        self.last_reproduce: dict = {}

    # -- bookkeeping ----------------------------------------------------------------

    @staticmethod
    def _key(case_id: str) -> str:
        """The case id as used in paths and branch names: lowercase, so two
        ids differing only in case never collide on a case-insensitive
        filesystem."""
        return case_id.lower()

    def _meta_path(self, key: str) -> str:
        return os.path.join(self.root, f".{key}.json")

    def _meta(self, path: str) -> dict:
        key = os.path.basename(os.path.normpath(path)).lower()
        try:
            with open(self._meta_path(key), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _write_meta(self, key: str, meta: dict) -> None:
        p = self._meta_path(key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
        os.replace(tmp, p)

    def check_path(self, path: str) -> str:
        """The worktree path, or RepairError when it isn't one of ours: outside
        the repair root, a protected checkout, or not a worktree of the router
        repository."""
        ap = os.path.abspath(path)
        for p in self.protected:
            if _norm(ap) == _norm(p) or _inside(ap, p):
                raise RepairError(f"refusing to operate on the protected checkout {ap}")
        if not _inside(ap, self.root) or _norm(ap) == _norm(self.root):
            raise RepairError(f"refusing {ap}: outside the repair root {self.root}")
        if not os.path.isdir(ap):
            raise RepairError(f"no worktree at {ap}")
        top = _git(ap, "rev-parse", "--show-toplevel", check=False)
        cd = _git(ap, "rev-parse", "--git-common-dir", check=False)
        if top.returncode or cd.returncode or _norm(top.stdout.strip()) != _norm(ap):
            raise RepairError(f"{ap} is not a git worktree root")
        cdp = cd.stdout.strip()
        if _norm(cdp if os.path.isabs(cdp) else os.path.join(ap, cdp)) != self._common:
            raise RepairError(f"{ap} is not a worktree of {self.router_repo}")
        return ap

    def base_sha(self, path: str) -> str:
        m = self._meta(path)
        if not m.get("base_sha"):
            raise RepairError(f"no recorded base for {path}")
        return m["base_sha"]

    # -- create -----------------------------------------------------------------------

    def _worktrees(self) -> list[dict]:
        out, cur = [], {}
        for line in _git(self.router_repo, "worktree", "list", "--porcelain").stdout.splitlines():
            if not line.strip():
                if cur:
                    out.append(cur)
                cur = {}
                continue
            k, _, v = line.partition(" ")
            cur[k] = v
        if cur:
            out.append(cur)
        return out

    def _is_ancestor(self, a: str, b: str) -> bool:
        return _git(self.router_repo, "merge-base", "--is-ancestor", a, b,
                    check=False).returncode == 0

    def create(self, case_id: str, base_sha: str) -> str:
        """`git worktree add -b lead/<case_id> <root>/<case_id> <base_sha>`
        (case id lowercased in paths and branches). Idempotent and
        crash-safe: the meta is written as an intent before the add, and an
        existing worktree that `git worktree list` shows on lead/<case_id>
        (from base) is adopted."""
        if not _CASE_RE.match(case_id or ""):
            raise RepairError(f"invalid case id {case_id!r}")
        key = self._key(case_id)
        full = _git(self.router_repo, "rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        path = os.path.join(self.root, key)
        branch = f"lead/{key}"
        meta = self._meta(path)
        if meta.get("base_sha") and meta["base_sha"] != full:
            raise RepairError(f"worktree for {case_id} exists on base {meta.get('base_sha')}, "
                              f"not {full}")
        final = {"case_id": case_id, "key": key, "base_sha": full, "branch": branch,
                 "path": path, "created_ts": meta.get("created_ts") or time.time()}
        if os.path.isdir(path):
            if meta and not meta.get("intent"):
                self.check_path(path)
                return path
            wt = next((w for w in self._worktrees()
                       if w.get("worktree") and _norm(w["worktree"]) == _norm(path)), None)
            if (wt and wt.get("branch") == f"refs/heads/{branch}" and wt.get("HEAD")
                    and self._is_ancestor(full, wt["HEAD"])):
                self._write_meta(key, final)              # adopt the crashed create
                return self.check_path(path)
            raise RepairError(f"{path} exists and is not a recorded repair worktree")
        if os.path.exists(path):
            raise RepairError(f"{path} exists and is not a recorded repair worktree")
        self._write_meta(key, dict(final, intent=True))   # the intent, before the add
        _git(self.router_repo, "worktree", "prune", check=False)
        tip = _git(self.router_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                   check=False)
        if tip.returncode == 0 and tip.stdout.strip():
            if not self._is_ancestor(full, tip.stdout.strip()):
                raise RepairError(f"branch {branch} exists and is not based on {full[:12]}")
            _git(self.router_repo, "worktree", "add", path, branch)
        else:
            _git(self.router_repo, "worktree", "add", "-b", branch, path, full)
        self._write_meta(key, final)
        return self.check_path(path)

    def remove(self, path: str) -> None:
        """Remove the worktree (the branch and its commits are kept)."""
        ap = self.check_path(path)
        _git(self.router_repo, "worktree", "remove", "--force", ap, check=False)
        _git(self.router_repo, "worktree", "prune", check=False)

    # -- inspect ----------------------------------------------------------------------

    def _untracked(self, ap: str) -> list[str]:
        out = _git(ap, "ls-files", "--others", "--exclude-standard", "-z").stdout
        return [p for p in out.split("\0") if p]

    def _safe_rel(self, ap: str, rel: str) -> str:
        rel = rel.replace("\\", "/")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/"):
            raise RepairError(f"path {rel!r} is outside Foundry-Router")
        full = os.path.join(ap, rel)
        # a symlinked entry may resolve outside the worktree
        if os.path.lexists(full) and not _inside(full, ap):
            raise RepairError(f"path {rel!r} resolves outside Foundry-Router")
        return rel

    def changed_files(self, path: str, base: Optional[str] = None) -> list[str]:
        """Repo-relative files that differ from the base: committed, staged,
        unstaged and untracked (not ignored)."""
        ap = self.check_path(path)
        base = base or self.base_sha(ap)
        tracked = _git(ap, "diff", "--name-only", "-z", "--no-renames", base).stdout
        files = {p for p in tracked.split("\0") if p} | set(self._untracked(ap))
        return sorted(self._safe_rel(ap, p) for p in files)

    def diff(self, path: str, base: Optional[str] = None) -> str:
        """The candidate's diff against the base (working tree included),
        untracked files as additions."""
        ap = self.check_path(path)
        base = base or self.base_sha(ap)
        parts = [_git(ap, "diff", "--no-renames", base).stdout]
        for rel in self._untracked(ap):
            self._safe_rel(ap, rel)
            r = _git(ap, "diff", "--no-index", "--", "/dev/null", rel, check=False)
            parts.append(r.stdout)
        return "".join(parts)

    def head(self, path: str) -> str:
        return _git(self.check_path(path), "rev-parse", "HEAD").stdout.strip()

    def _show(self, ap: str, rev: str, rel: str) -> Optional[str]:
        r = _git(ap, "show", f"{rev}:{rel}", check=False)
        return r.stdout if r.returncode == 0 else None

    def _current(self, ap: str, rel: str) -> Optional[str]:
        full = os.path.join(ap, rel)
        if not os.path.isfile(full):
            return None
        with open(full, encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()

    def expectation_changes(self, path: str, base: Optional[str] = None) -> dict:
        """{file: [why its change alters test expectations]} for every changed
        test or pytest configuration file."""
        ap = self.check_path(path)
        base = base or self.base_sha(ap)
        out = {}
        for f in self.changed_files(ap, base):
            old, new = self._show(ap, base, f), self._current(ap, f)
            why = config_expectation_changes(f, old, new)
            if is_test_path(f) and not f.endswith("conftest.py"):
                why += test_expectation_changes(f, old, new)
            if why:
                out[f] = why
        return out

    def check_scope(self, path: str, scope: list[str],
                    declared_test_changes: Iterable[str] = (),
                    approved_test_changes: Iterable[str] = ()) -> list[str]:
        """Every reason the current candidate is refused ([] = acceptable). A
        changed test expectation passes only when it is declared AND approved
        by the operator (approved_test_changes)."""
        ap = self.check_path(path)
        base = self.base_sha(ap)
        if not scope:
            return ["no repair scope declared"]
        declared = {str(d).replace("\\", "/") for d in declared_test_changes or []}
        approved = {str(d).replace("\\", "/") for d in approved_test_changes or []}
        out = []
        changed = self.changed_files(ap, base)
        outside = [f for f in changed if not scope_match(f, scope)]
        if outside:
            out.append("dirty scope: files outside the declared repair scope: "
                       + ", ".join(outside[:20]))
        for f, why in sorted(self.expectation_changes(ap, base).items()):
            if f in declared and f in approved:
                continue
            if f in declared:
                out.append(f"{f}: a declared change to an existing test expectation needs an "
                           f"operator approval (approved_test_changes): " + "; ".join(why))
            else:
                out += why
        return out

    # -- commit / manifest ------------------------------------------------------------

    def commit(self, path: str, message: str, *, scope: list[str],
               declared_test_changes: Iterable[str] = (),
               approved_test_changes: Iterable[str] = ()) -> str:
        """Commit every change in the worktree on lead/<case_id>, after the
        scope and test-expectation checks. Returns the new HEAD sha."""
        ap = self.check_path(path)
        problems = self.check_scope(ap, scope, declared_test_changes, approved_test_changes)
        if problems:
            raise RepairError("repair commit refused: " + "; ".join(problems), problems)
        _git(ap, "add", "-A")
        if _git(ap, "diff", "--cached", "--quiet", check=False).returncode == 0:
            raise RepairError("nothing to commit")
        _git(ap, "-c", "user.name=foundry-lead", "-c", "user.email=lead@foundry.invalid",
             "commit", "-q", "-m", message or "lead repair")
        return self.head(ap)

    def manifest(self, path: str, *, scope: list[str], tests: dict | list,
                 review: Optional[dict] = None, migrations: Optional[list] = None,
                 declared_test_changes: Iterable[str] = (),
                 approved_test_changes: Iterable[str] = (), release: str = "",
                 lead_enabled: bool = False) -> dict:
        """What the supervisor installs: the committed candidate only (a dirty
        worktree has no manifest)."""
        ap = self.check_path(path)
        base = self.base_sha(ap)
        dirty = _git(ap, "status", "--porcelain").stdout.strip()
        if dirty:
            raise RepairError("uncommitted changes in the worktree: commit before the manifest")
        sha = self.head(ap)
        files = sorted(f for f in _git(ap, "diff", "--name-only", "-z", "--no-renames",
                                       base, sha).stdout.split("\0") if f)
        if isinstance(tests, list):
            tests = {"focused": list(tests), "broader": []}
        if migrations is None:
            migrations = [f for f in files if "migrat" in f.lower()]
        return {"case_id": self._meta(ap).get("case_id"), "branch": self._meta(ap).get("branch"),
                "worktree": ap, "base_sha": base, "candidate_sha": sha, "files": files,
                "scope": list(scope), "migrations": list(migrations),
                "tests": {"focused": list(tests.get("focused") or []),
                          "broader": list(tests.get("broader") or []),
                          "regression": tests.get("regression") or ""},
                "declared_test_changes": sorted(declared_test_changes or []),
                "approved_test_changes": sorted(approved_test_changes or []),
                "review_id": (review or {}).get("review_id") or "",
                "review_verdict": (review or {}).get("verdict") or "",
                "reviewer": (review or {}).get("persona") or "", "release": release,
                "lead_enabled": bool(lead_enabled)}

    # -- tests ------------------------------------------------------------------------

    def _pytest(self, cwd: str, ids: list[str], timeout: float, **kw) -> dict:
        return run_pytest(self.python, cwd, ids, timeout, **kw)

    @contextlib.contextmanager
    def _base_tree(self, base: str):
        """A temporary detached worktree at `base`, always removed."""
        bpath = os.path.join(self.root, f"_base-{uuid.uuid4().hex[:10]}")
        _git(self.router_repo, "worktree", "add", "--detach", bpath, base)
        try:
            yield bpath
        finally:
            _git(self.router_repo, "worktree", "remove", "--force", bpath, check=False)
            _git(self.router_repo, "worktree", "prune", check=False)
            shutil.rmtree(bpath, ignore_errors=True)

    def broader_for(self, path: str, broader: Optional[list[str]]) -> list[str]:
        """The broader suite: the named one, else the defaults present in the
        worktree, else `tests` when it exists ([] = none available)."""
        if broader:
            return list(broader)
        ap = self.check_path(path)
        got = [b for b in self.broader_default if os.path.exists(os.path.join(ap, b))]
        if not got and os.path.isdir(os.path.join(ap, "tests")):
            got = ["tests"]
        return got

    def run_tests(self, path: str, focused: list[str], broader: list[str],
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Focused tests (required, non-empty) then a non-empty broader suite
        (the default when none is named), each a sandboxed subprocess pytest
        in the worktree; every named id must be collected and pass.
        ok = both passed. `broader_ids` is the suite that ran."""
        ap = self.check_path(path)
        if not focused:
            return {"ok": False, "focused": None, "broader": None, "broader_ids": [],
                    "log": "no focused tests named"}
        broader = self.broader_for(ap, broader)
        try:
            check_test_ids(list(focused) + list(broader))
        except RepairError as e:
            return {"ok": False, "focused": None, "broader": None, "broader_ids": broader,
                    "log": str(e)}
        if not broader:
            return {"ok": False, "focused": None, "broader": None, "broader_ids": [],
                    "log": "no broader test suite named or available"}
        f = self._pytest(ap, list(focused), timeout)
        b = self._pytest(ap, list(broader), timeout) if f["ok"] else None
        ok = bool(f["ok"] and b and b["ok"])
        probs = (f["problems"] or []) + ((b or {}).get("problems") or [])
        return {"ok": ok, "focused": f, "broader": b, "broader_ids": broader,
                "problems": probs,
                "log": (("; ".join(probs) + "\n") if probs else "")
                + (f["log"] + ("\n" + b["log"] if b else ""))[-LOG_CHARS:]}

    def reproduces(self, path: str, test_id: str, base_sha: Optional[str] = None,
                   timeout: float = DEFAULT_TIMEOUT) -> bool:
        """True only when the node `test_id` (path::test) FAILS as a test (rc 1,
        its junit case a failure, not an error) at the base commit (with the
        candidate's test files copied in) and PASSES (not skipped) in the
        candidate. A test that isn't collected, an import/collection error, a
        usage error or a timeout is not a reproduction. Details in
        self.last_reproduce."""
        ap = self.check_path(path)
        base = base_sha or self.base_sha(ap)
        res = {"test_id": test_id, "base_sha": base, "base": None, "candidate": None,
               "reproduces": False, "reason": ""}
        self.last_reproduce = res
        try:
            rel, _ = parse_test_id(test_id, require_node=True)
        except RepairError as e:
            res["reason"] = str(e)
            return False
        self._safe_rel(ap, rel)
        if not os.path.isfile(os.path.join(ap, rel)):
            res["reason"] = f"{rel} is not in the candidate"
            return False
        with self._base_tree(base) as bpath:
            # the regression test (and test files the candidate adds) at base
            copies = {rel} | {f for f in self.changed_files(ap, base)
                              if is_test_path(f) and self._show(ap, base, f) is None
                              and os.path.isfile(os.path.join(ap, f))}
            for f in sorted(copies):
                dst = os.path.join(bpath, f)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(ap, f), dst)
            res["base"] = self._pytest(bpath, [test_id], timeout)
        b = res["base"]
        mine = cases_for(test_id, b["cases"] or []) if b["cases"] is not None else []
        if b["rc"] != 1 or not mine or any(c["outcome"] != "failed" for c in mine):
            res["reason"] = ("the test passes at base: it does not reproduce the defect"
                             if b["rc"] == 0 else
                             f"the test did not fail as a test at base (pytest rc {b['rc']}; "
                             f"outcomes {[c['outcome'] for c in mine] or 'not collected'}): "
                             f"an import/collection error is not a reproduction")
            return False
        res["candidate"] = self._pytest(ap, [test_id], timeout)
        if not res["candidate"]["ok"]:
            res["reason"] = (f"the test does not pass at the candidate (rc "
                             f"{res['candidate']['rc']}; {'; '.join(res['candidate']['problems'])})")
            return False
        res["reproduces"], res["reason"] = True, "fails at base, passes at the candidate"
        return True

    def existing_tests_hold(self, path: str, files: list[str],
                            timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Every test the base collects from `files` (existing test files the
        candidate modified) is still collected at the candidate and passes
        (or is skipped). {ok, base_ids, problems}."""
        ap = self.check_path(path)
        base = self.base_sha(ap)
        out = {"ok": True, "base_ids": [], "problems": [], "files": list(files)}
        if not files:
            return out
        with self._base_tree(base) as bpath:
            col = self._pytest(bpath, list(files), timeout, collect_only=True)
        ids = [i for i in col["collected"] if i.split("::")[0] in files]
        out["base_ids"] = ids
        if not col["ok"] and not ids:
            return out                       # nothing collectable at base: nothing to keep
        run = self._pytest(ap, list(files), timeout)
        cases = run["cases"]
        if cases is None:
            out.update(ok=False, problems=["no junit report at the candidate"])
            return out
        for tid in ids:
            try:
                got = cases_for(tid, cases)
            except RepairError:
                continue                     # an id pytest printed that we can't map
            if not got:
                out["problems"].append(f"{tid}: collected at base, not at the candidate")
            elif any(c["outcome"] in ("failed", "error") for c in got):
                out["problems"].append(f"{tid}: fails at the candidate")
        out["ok"] = not out["problems"]
        return out

    # -- review -----------------------------------------------------------------------

    def review(self, P, path: str, diff: Optional[str] = None, *, case_id: str,
               lead_persona: str, reviewer_persona: str, project_id: str = "",
               context: str = "", max_tokens: int = 8000) -> dict:
        """An independent Foundry review of the committed candidate. The
        reviewer must not be the lead persona. {verdict, findings, approved,
        review_id, persona, served_model, error, review}."""
        ap = self.check_path(path)
        lead = (lead_persona or "").strip()
        rev = (reviewer_persona or "").strip()
        if not lead:
            raise RepairError("the lead persona is required to prove the review is independent")
        if not rev:
            raise RepairError("no reviewer persona configured")
        if rev.casefold() == lead.casefold():
            raise RepairError(f"reviewer {rev!r} is the lead persona: the review must be "
                              f"independent")
        base = self.base_sha(ap)
        sha = self.head(ap)
        if _git(ap, "status", "--porcelain").stdout.strip():
            raise RepairError("commit the candidate before review: the review binds one commit")
        all_files = sorted(f for f in _git(ap, "diff", "--name-only", "-z", base, sha).stdout
                           .split("\0") if f)
        # B4 (2026-10-21): after a CHANGES_REQUIRED review of an earlier candidate of this
        # case, only the diff since it is sent, with its findings (one line each); files
        # reviewed there and unchanged since are named, not re-sent
        last = self._last_review(ap, case_id)
        dbase, earlier = base, []
        if diff is None and last and last.get("verdict") == "CHANGES_REQUIRED" and \
                last.get("sha") and last["sha"] != sha and \
                _git(ap, "merge-base", "--is-ancestor", last["sha"], sha,
                     check=False).returncode == 0:
            dbase = last["sha"]
            since = {f for f in _git(ap, "diff", "--name-only", "-z", dbase, sha).stdout
                     .split("\0") if f}
            earlier = (["", f"## Earlier review {last.get('review_id')} of candidate "
                            f"{dbase[:12]}: CHANGES_REQUIRED (incremental re-review)",
                        f"The diff below is {dbase[:12]}..{sha[:12]}; the candidate diff from "
                        f"{base[:12]} was reviewed there. Check that these findings are "
                        f"resolved and that the changes since are correct.",
                        *[f"- {' '.join(str(x).split())[:300]}"
                          for x in (last.get("findings") or [])[:20]],
                        f"Reviewed there and unchanged since (not re-sent): "
                        + (", ".join(sorted(set(all_files) - since)[:40]) or "(none)")])
        d = diff if diff is not None else _git(ap, "diff", "--no-renames", dbase, sha).stdout
        files = all_files
        cut = len(d) > DIFF_CHARS
        packet = "\n".join([
            "# Coordinator repair review",
            f"case id (sprint_id): {case_id}", f"project_id: {project_id}",
            f"base commit: {base}", f"candidate commit (reviewed_commit_sha): {sha}",
            "files changed:", *[f"- {f}" for f in files],
            "", "## Context", (context or "(none)")[:20_000], *earlier,
            "", "## Diff" + (f" ({dbase[:12]}..{sha[:12]}, since the earlier review)"
                             if dbase != base else "")
            + (" (truncated: the diff exceeds the packet)" if cut else ""),
            "```diff", d[:DIFF_CHARS], "```"])
        rsid = f"lead-review-{case_id}-{sha[:12]}"
        text, meta = P.foundry.chat(rev, [{"role": "system", "content": REPAIR_REVIEW_SYSTEM},
                                          {"role": "user", "content": packet}],
                                    session_id=rsid, schema=R.SCHEMA, max_tokens=max_tokens)
        rid = "rr-" + hashlib.sha256(f"{sha}:{rev}:{text}".encode("utf-8", "replace")).hexdigest()[:12]
        out = {"review_id": rid, "persona": rev, "reviewed_sha": sha,
               "served_model": (meta or {}).get("served_model"), "error": "", "review": None}
        out["incremental_from"] = dbase if dbase != base else None
        try:
            v = R.validate(text, project_id=project_id, task_id=case_id, head_sha=sha, scope=files)
        except R.ReviewInvalid as e:
            out.update(verdict="INVALID", findings=[], approved=False, error=str(e))
            return out
        out.update(verdict=v["verdict"], findings=v["findings"], review=v,
                   approved=v["verdict"] == "APPROVED")
        self._remember_review(ap, case_id, {"sha": sha, "verdict": v["verdict"],
                                            "review_id": rid,
                                            "findings": R.findings_for_repair(v)})
        return out

    def _review_memory_path(self, ap: str) -> Optional[str]:
        r = _git(ap, "rev-parse", "--absolute-git-dir", check=False)
        d = (r.stdout or "").strip()
        return os.path.join(d, "foundry-repair-reviews.json") if r.returncode == 0 and d else None

    def _last_review(self, ap: str, case_id: str) -> Optional[dict]:
        p = self._review_memory_path(ap)
        try:
            with open(p or "", encoding="utf-8") as f:
                got = (json.load(f) or {}).get(case_id)
            return got if isinstance(got, dict) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _remember_review(self, ap: str, case_id: str, rec: dict) -> None:
        """The last review of each case, in the worktree's git dir (never committed;
        survives a coordinator restart): the next review of the case is incremental."""
        p = self._review_memory_path(ap)
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                mem = json.load(f) or {}
        except (OSError, ValueError):
            mem = {}
        mem[case_id] = rec
        try:
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(mem, f)
            os.replace(p + ".tmp", p)
        except OSError:
            pass

    # -- the whole gate ---------------------------------------------------------------

    def validate(self, P, path: str, *, case_id: str, regression_test: str,
                 focused: list[str], broader: list[str], scope: list[str],
                 lead_persona: str, reviewer_persona: str, project_id: str = "",
                 declared_test_changes: Iterable[str] = (),
                 approved_test_changes: Iterable[str] = (), migrations: Optional[list] = None,
                 timeout: float = DEFAULT_TIMEOUT, context: str = "",
                 lead_enabled: bool = True) -> dict:
        """The candidate gate, in order; stops at the first failure.
        {ok, reasons, reproduce, existing, tests, review, manifest}."""
        ap = self.check_path(path)
        out = {"ok": False, "reasons": [], "reproduce": None, "existing": None, "tests": None,
               "review": None, "manifest": None}
        if _git(ap, "status", "--porcelain").stdout.strip():
            out["reasons"].append("uncommitted changes in the worktree")
            return out
        problems = self.check_scope(ap, scope, declared_test_changes, approved_test_changes)
        if problems:
            out["reasons"] += problems
            return out
        if not regression_test:
            out["reasons"].append("no reproducing regression test named")
            return out
        try:
            check_test_ids([regression_test], require_node=True)
            check_test_ids(list(focused or []) + list(broader or []))
        except RepairError as e:
            out["reasons"].append(f"invalid test id: {e}")
            return out
        rep = self.reproduces(ap, regression_test, timeout=timeout)
        out["reproduce"] = dict(self.last_reproduce)
        if not rep:
            out["reasons"].append(f"regression test does not reproduce: {self.last_reproduce['reason']}")
            return out
        base = self.base_sha(ap)
        approved = {str(d).replace("\\", "/") for d in approved_test_changes or []}
        modified = [f for f in self.changed_files(ap, base)
                    if f.endswith(".py") and is_test_path(f) and not f.endswith("conftest.py")
                    and f not in approved and self._show(ap, base, f) is not None
                    and os.path.isfile(os.path.join(ap, f))]
        ex = self.existing_tests_hold(ap, modified, timeout)
        out["existing"] = ex
        if not ex["ok"]:
            out["reasons"].append("existing tests no longer hold: " + "; ".join(ex["problems"][:10]))
            return out
        tests = self.run_tests(ap, focused, broader, timeout)
        out["tests"] = tests
        if not tests["ok"]:
            out["reasons"].append("focused or broader tests failed"
                                  + (": " + "; ".join(tests.get("problems") or [])[:500]
                                     if tests.get("problems") else
                                     f": {tests['log'][:300]}" if tests.get("focused") is None
                                     else ""))
            return out
        rv = self.review(P, ap, case_id=case_id, lead_persona=lead_persona,
                         reviewer_persona=reviewer_persona, project_id=project_id,
                         context=context)
        out["review"] = rv
        if not rv["approved"]:
            out["reasons"].append(f"review verdict {rv['verdict']}"
                                  + (f": {rv['error']}" if rv.get("error") else ""))
            return out
        out["manifest"] = self.manifest(ap, scope=scope, review=rv, migrations=migrations,
                                        declared_test_changes=declared_test_changes,
                                        approved_test_changes=approved_test_changes,
                                        tests={"focused": focused,
                                               "broader": tests["broader_ids"],
                                               "regression": regression_test},
                                        lead_enabled=lead_enabled)
        out["ok"] = True
        return out
