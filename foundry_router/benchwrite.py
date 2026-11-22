"""Code that compiles (v0.181.246).

Every code case in the bench asks what code does; none asks for code. Scoring
written code properly means running it against tests, and the bench's rule is
that nothing a model writes is ever run - that waits for the workspace server
(docs/BENCH_GAPS.md, addition 8). Until then this checks what can be checked
without running anything: whether a model can write the language at all.

A case is a module to write to a spec: function names, parameters and types
given exactly, what each does said in a line. Luau cases are Roblox
ModuleScripts in strict mode, checked by the type checker the operator set up
under Code checks (luau-lsp with Roblox's own type definitions): a member that
does not exist, a nil not handled, a table returned where a number was
promised and a missing `end` are all caught. Python cases are compiled with
Python's own compiler (never run) and read with its parser: every function
there, with its parameters, annotated, and nothing imported from outside the
standard library.

0 when the code does not compile or type check, or there is none. 50 when it
does but the module is not the one asked for (a function missing, renamed or
with other parameters). 100 when it is both. Says nothing about whether the
code is right.
"""
from __future__ import annotations

import ast
import random
import re
import sys

WRITE_TOP = 8
WRITE_MAX = 8
SIZES = (1, 2, 4, 6, 9, 13, 18, 24)            # functions in the module, by level
LANGS = ("luau", "python")
LUAU_SYSTEM = ("You write Roblox Luau. Reply with the whole module in one ```luau code block.")
PY_SYSTEM = "You write Python. Reply with the whole module in one ```python code block."

# (name, params, return type, what it does, a reference body that type checks)
LUAU_FUNCS = (
    ("countPlayers", "", "number", "returns how many players are in the game",
     'return #game:GetService("Players"):GetPlayers()'),
    ("paint", "part: BasePart, color: Color3", "()", "sets the part's colour",
     "part.Color = color"),
    ("distance", "a: BasePart, b: BasePart", "number",
     "returns the distance in studs between the two parts' positions",
     "return (a.Position - b.Position).Magnitude"),
    ("findChildPart", "parent: Instance, name: string", "BasePart?",
     "returns the child with that name if it is a BasePart, otherwise nil",
     "local c = parent:FindFirstChild(name)\n\tif c and c:IsA(\"BasePart\") then\n\t\t"
     "return c :: BasePart\n\tend\n\treturn nil"),
    ("makePart", "size: Vector3, position: Vector3, parent: Instance", "Part",
     "creates an anchored Part of that size at that position under the parent and returns it",
     "local p = Instance.new(\"Part\")\n\tp.Size = size\n\tp.Position = position\n\t"
     "p.Anchored = true\n\tp.Parent = parent\n\treturn p"),
    ("moveTo", "part: BasePart, goal: Vector3, seconds: number", "Tween",
     "starts a TweenService tween moving the part to the goal position over that many "
     "seconds and returns the tween",
     "local tw = game:GetService(\"TweenService\"):Create(part, TweenInfo.new(seconds), "
     "{Position = goal})\n\ttw:Play()\n\treturn tw"),
    ("playerNames", "", "{string}", "returns the names of every player, sorted",
     "local out = {}\n\tfor _, p in game:GetService(\"Players\"):GetPlayers() do\n\t\t"
     "table.insert(out, p.Name)\n\tend\n\ttable.sort(out)\n\treturn out"),
    ("onTouched", "part: BasePart, callback: (Player) -> ()", "RBXScriptConnection",
     "connects to the part's Touched event and calls the callback with the player whose "
     "character touched it",
     "return part.Touched:Connect(function(hit: BasePart)\n\t\tlocal model = "
     "hit:FindFirstAncestorOfClass(\"Model\")\n\t\tlocal player = model and "
     "game:GetService(\"Players\"):GetPlayerFromCharacter(model)\n\t\tif player then\n\t\t\t"
     "callback(player)\n\t\tend\n\tend)"),
    ("setLeaderstat", "player: Player, stat: string, value: number", "()",
     "sets the player's leaderstats IntValue with that name, making the leaderstats "
     "Folder and the IntValue if they are missing",
     "local ls = player:FindFirstChild(\"leaderstats\")\n\tif not ls then\n\t\tlocal f = "
     "Instance.new(\"Folder\")\n\t\tf.Name = \"leaderstats\"\n\t\tf.Parent = player\n\t\t"
     "ls = f\n\tend\n\tlocal v = (ls :: Instance):FindFirstChild(stat)\n\tif not v then\n\t\t"
     "local iv = Instance.new(\"IntValue\")\n\t\tiv.Name = stat\n\t\tiv.Parent = ls\n\t\t"
     "v = iv\n\tend\n\t(v :: IntValue).Value = value"),
    ("sumValues", "folder: Folder", "number",
     "returns the sum of the Value of every IntValue and NumberValue child of the folder",
     "local total = 0\n\tfor _, c in folder:GetChildren() do\n\t\tif c:IsA(\"IntValue\") then\n"
     "\t\t\ttotal += (c :: IntValue).Value\n\t\telseif c:IsA(\"NumberValue\") then\n\t\t\t"
     "total += (c :: NumberValue).Value\n\t\tend\n\tend\n\treturn total"),
    ("humanoidOf", "player: Player", "Humanoid?",
     "returns the Humanoid of the player's character, or nil when there is none",
     "local ch = player.Character\n\tif not ch then\n\t\treturn nil\n\tend\n\t"
     "return ch:FindFirstChildOfClass(\"Humanoid\")"),
    ("heal", "player: Player, amount: number", "boolean",
     "adds the amount to the player's humanoid health, no higher than its MaxHealth; "
     "returns whether there was a humanoid",
     "local ch = player.Character\n\tlocal h = ch and ch:FindFirstChildOfClass(\"Humanoid\")\n"
     "\tif not h then\n\t\treturn false\n\tend\n\th.Health = math.min(h.MaxHealth, "
     "h.Health + amount)\n\treturn true"),
    ("raycastDown", "origin: Vector3, ignore: {Instance}", "RaycastResult?",
     "casts a ray 500 studs straight down from the origin, excluding the given instances, "
     "and returns the result",
     "local params = RaycastParams.new()\n\tparams.FilterType = Enum.RaycastFilterType.Exclude\n"
     "\tparams.FilterDescendantsInstances = ignore\n\treturn workspace:Raycast(origin, "
     "Vector3.new(0, -500, 0), params)"),
    ("nearestPlayer", "position: Vector3", "Player?",
     "returns the player whose character's HumanoidRootPart is nearest the position, or nil",
     "local best, bestD = nil, math.huge\n\tfor _, p in game:GetService(\"Players\"):GetPlayers()"
     " do\n\t\tlocal ch = p.Character\n\t\tlocal root = ch and ch:FindFirstChild("
     "\"HumanoidRootPart\")\n\t\tif root and root:IsA(\"BasePart\") then\n\t\t\tlocal d = "
     "((root :: BasePart).Position - position).Magnitude\n\t\t\tif d < bestD then\n\t\t\t\t"
     "best, bestD = p, d\n\t\t\tend\n\t\tend\n\tend\n\treturn best"),
    ("tellEveryone", "event: RemoteEvent, message: string", "()",
     "fires the remote event to every client with the message",
     "event:FireAllClients(message)"),
    ("debounce", "fn: () -> (), seconds: number", "() -> ()",
     "returns a function that calls fn, but at most once in any span of that many seconds",
     "local last = -math.huge\n\treturn function()\n\t\tlocal now = os.clock()\n\t\t"
     "if now - last >= seconds then\n\t\t\tlast = now\n\t\t\tfn()\n\t\tend\n\tend"),
    ("saveScores", "store: DataStore, key: string, data: {[string]: number}", "boolean",
     "saves the data under the key with SetAsync inside pcall and returns whether it worked",
     "local ok = pcall(function()\n\t\tstore:SetAsync(key, data)\n\tend)\n\treturn ok"),
    ("weld", "a: BasePart, b: BasePart", "WeldConstraint",
     "joins the two parts with a new WeldConstraint parented to the first and returns it",
     "local w = Instance.new(\"WeldConstraint\")\n\tw.Part0 = a\n\tw.Part1 = b\n\tw.Parent = a\n"
     "\treturn w"),
    ("setGroup", "model: Model, group: string", "()",
     "puts every BasePart in the model, at any depth, in that collision group",
     "for _, d in model:GetDescendants() do\n\t\tif d:IsA(\"BasePart\") then\n\t\t\t"
     "(d :: BasePart).CollisionGroup = group\n\t\tend\n\tend"),
    ("clockText", "seconds: number", "string",
     "returns the whole number of seconds as minutes and two-digit seconds, like 3:07",
     "local s = math.floor(seconds)\n\treturn string.format(\"%d:%02d\", s // 60, s % 60)"),
    ("countTagged", "tag: string", "number",
     "returns how many instances carry the CollectionService tag",
     "return #game:GetService(\"CollectionService\"):GetTagged(tag)"),
    ("blend", "a: Color3, b: Color3, t: number", "Color3",
     "returns the colour that is the fraction t of the way from a to b",
     "return a:Lerp(b, t)"),
    ("attributeOr", "inst: Instance, name: string, default: number", "number",
     "returns the instance's attribute with that name if it is a number, otherwise the default",
     "local v = inst:GetAttribute(name)\n\tif type(v) == \"number\" then\n\t\treturn v\n\tend\n"
     "\treturn default"),
    ("clearClass", "parent: Instance, className: string", "number",
     "destroys every child of the parent that is of that class and returns how many it destroyed",
     "local n = 0\n\tfor _, c in parent:GetChildren() do\n\t\tif c:IsA(className) then\n\t\t\t"
     "c:Destroy()\n\t\t\tn += 1\n\t\tend\n\tend\n\treturn n"),
)

PY_FUNCS = (
    ("mean", "values: list[float]", "float", "returns the arithmetic mean; 0.0 for an empty list",
     "return sum(values) / len(values) if values else 0.0"),
    ("word_counts", "text: str", "dict[str, int]",
     "counts each lower-cased word, words being runs of letters",
     "import re\n    out: dict[str, int] = {}\n    for w in re.findall(r'[a-z]+', text.lower()):\n"
     "        out[w] = out.get(w, 0) + 1\n    return out"),
    ("chunks", "items: list[int], size: int", "list[list[int]]",
     "splits the list into consecutive pieces of that size, the last possibly shorter",
     "return [items[i:i + size] for i in range(0, len(items), size)]"),
    ("parse_duration", "text: str", "int",
     "turns text like '1h30m' or '45s' into a number of seconds",
     "import re\n    units = {'h': 3600, 'm': 60, 's': 1}\n    return sum(int(n) * units[u] "
     "for n, u in re.findall(r'(\\d+)([hms])', text))"),
    ("flatten", "nested: list[list[str]]", "list[str]", "joins the inner lists in order",
     "return [x for inner in nested for x in inner]"),
    ("is_palindrome", "text: str", "bool",
     "whether the letters and digits read the same both ways, ignoring case",
     "s = [c.lower() for c in text if c.isalnum()]\n    return s == s[::-1]"),
    ("top_n", "scores: dict[str, int], n: int", "list[str]",
     "the n names with the highest scores, highest first, ties by name",
     "return [k for k, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]"),
    ("merge_ranges", "ranges: list[tuple[int, int]]", "list[tuple[int, int]]",
     "merges overlapping (start, end) ranges and returns them sorted",
     "out: list[tuple[int, int]] = []\n    for a, b in sorted(ranges):\n        if out and a <= "
     "out[-1][1]:\n            out[-1] = (out[-1][0], max(out[-1][1], b))\n        else:\n"
     "            out.append((a, b))\n    return out"),
    ("read_config", "path: str", "dict[str, str]",
     "reads key=value lines from the file, skipping blank lines and lines starting with #",
     "out: dict[str, str] = {}\n    with open(path, encoding='utf-8') as fh:\n        for line in "
     "fh:\n            line = line.strip()\n            if line and not line.startswith('#') "
     "and '=' in line:\n                k, v = line.split('=', 1)\n                "
     "out[k.strip()] = v.strip()\n    return out"),
    ("slug", "title: str", "str",
     "lower-case, runs of anything but letters and digits become one hyphen, none at the ends",
     "import re\n    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')"),
    ("days_between", "a: str, b: str", "int",
     "the number of days from ISO date a to ISO date b",
     "from datetime import date\n    return (date.fromisoformat(b) - date.fromisoformat(a)).days"),
    ("dedupe", "items: list[str]", "list[str]", "drops repeats, keeping the first of each in order",
     "seen: set[str] = set()\n    out: list[str] = []\n    for x in items:\n        if x not in "
     "seen:\n            seen.add(x)\n            out.append(x)\n    return out"),
    ("transpose", "rows: list[list[int]]", "list[list[int]]", "swaps rows and columns",
     "return [list(col) for col in zip(*rows)]"),
    ("median", "values: list[float]", "float | None", "the median, or None for an empty list",
     "import statistics\n    return statistics.median(values) if values else None"),
    ("group_by_first", "words: list[str]", "dict[str, list[str]]",
     "groups the words by their first letter, lower-cased",
     "out: dict[str, list[str]] = {}\n    for w in words:\n        if w:\n            "
     "out.setdefault(w[0].lower(), []).append(w)\n    return out"),
    ("roman", "n: int", "str", "the Roman numeral for 1 to 3999",
     "vals = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'), "
     "(50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]\n    "
     "out = ''\n    for v, s in vals:\n        while n >= v:\n            out += s\n            "
     "n -= v\n    return out"),
    ("csv_column", "text: str, column: str", "list[str]",
     "the values of the named column of CSV text with a header row",
     "import csv\n    import io\n    return [row[column] for row in "
     "csv.DictReader(io.StringIO(text))]"),
    ("retry_delays", "attempts: int, base: float", "list[float]",
     "the waits of an exponential back-off: base, then twice that, and so on",
     "return [base * 2 ** i for i in range(attempts)]"),
    ("bytes_text", "n: int", "str", "a byte count as text like '1.5 KB', '3.0 MB'",
     "units = ['B', 'KB', 'MB', 'GB', 'TB']\n    x = float(n)\n    for u in units:\n        "
     "if x < 1024 or u == 'TB':\n            return f'{x:.1f} {u}'\n        x /= 1024\n    "
     "return f'{x:.1f} TB'"),
    ("rotate", "items: list[int], k: int", "list[int]", "rotates the list left by k places",
     "if not items:\n        return []\n    k %= len(items)\n    return items[k:] + items[:k]"),
    ("hash_file", "path: str", "str", "the SHA-256 of the file's bytes, as hex",
     "import hashlib\n    with open(path, 'rb') as fh:\n        return "
     "hashlib.sha256(fh.read()).hexdigest()"),
    ("window_sums", "values: list[int], width: int", "list[int]",
     "the sum of every run of `width` consecutive values",
     "return [sum(values[i:i + width]) for i in range(len(values) - width + 1)]"),
    ("env_flag", "name: str, default: bool", "bool",
     "reads an environment variable as yes/no (1, true, yes, on), the default when unset",
     "import os\n    v = os.environ.get(name)\n    if v is None:\n        return default\n    "
     "return v.strip().lower() in ('1', 'true', 'yes', 'on')"),
    ("nth_prime", "n: int", "int", "the n-th prime, counting 2 as the first",
     "count, k = 0, 1\n    while count < n:\n        k += 1\n        if all(k % d for d in "
     "range(2, int(k ** 0.5) + 1)):\n            count += 1\n    return k"),
)


# --------------------------------------------------------------------------- #
# Reading a reply without running it                                          #
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^[ \t]*```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)^[ \t]*```",
                    re.S | re.M)
_LANG_TAGS = {"luau": ("luau", "lua"), "python": ("python", "py", "python3")}


def code_of(text: str, lang: str) -> str:
    """The reply's code in this language: every fenced block tagged with it,
    in order. "" when there is none."""
    tags = _LANG_TAGS[lang]
    got = [m.group(2) for m in _FENCE.finditer(str(text or ""))
           if (m.group(1) or "").strip().lower() in tags]
    return "\n".join(got).strip("\n")


def _params(sig: str) -> list:
    """Parameter names of a written-out parameter list: "a: X, b: (Y) -> ()" -> [a, b]."""
    out, depth, cur = [], 0, ""
    for ch in sig:
        if ch in "({[<":
            depth += 1
        elif ch in ")}]>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.split(":")[0].strip() for p in out if p.split(":")[0].strip()]


_LUAU_DEF = re.compile(r"^\s*function\s+M\s*[.:]\s*(\w+)\s*(?:<[^>]*>)?\s*\(", re.M)
_LUAU_ASSIGN = re.compile(r"^\s*M\s*\.\s*(\w+)\s*=\s*function\s*(?:<[^>]*>)?\s*\(", re.M)


def _paren_body(code: str, start: int) -> str:
    """The text inside the parentheses that open just before `start`."""
    depth, i = 1, start
    while i < len(code) and depth:
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
        i += 1
    return code[start:i - 1]


def luau_shape(code: str, want: list) -> list:
    """What is wrong with the module's shape (not its types): [reasons]."""
    first = next((ln.strip() for ln in code.splitlines() if ln.strip()), "")
    probs = []
    if first != "--!strict":
        probs.append("not_strict")
    if re.search(r"--!\s*(nocheck|nonstrict)", code):
        probs.append("checking_off")
    if not re.search(r"^\s*return\s+M\s*$", code, re.M):
        probs.append("no_return_M")
    found = {}
    for rx in (_LUAU_DEF, _LUAU_ASSIGN):
        for m in rx.finditer(code):
            found.setdefault(m.group(1), _params(_paren_body(code, m.end())))
    for name, params, *_ in want:
        if name not in found:
            probs.append("missing:" + name)
        elif found[name] != _params(params):
            probs.append("params:" + name)
    return probs


def py_shape(code: str, want: list) -> list:
    """Compiled (never run) and read: [reasons], "syntax:..." first when it
    does not compile."""
    try:
        compile(code, "<model>", "exec")
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax:line_{e.lineno}"]
    except ValueError as e:
        return ["syntax:" + str(e).replace(" ", "_")[:40]]
    probs = []
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module]
        for m in mods:
            if std and m.split(".")[0] not in std:
                probs.append("import:" + m.split(".")[0])
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, params, *_ in want:
        fn = defs.get(name)
        if fn is None:
            probs.append("missing:" + name)
            continue
        args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        if [a.arg for a in args] != _params(params):
            probs.append("params:" + name)
        elif any(a.annotation is None for a in args) or fn.returns is None:
            probs.append("untyped:" + name)
    return probs


WHY_WORDS = {"none": "wrote no code in the language", "check": "does not type check",
             "loose": "not in strict mode, or checking turned off",
             "syntax": "does not compile", "shape": "not the module asked for",
             "unchecked": "no checker to run"}


def write_score(text: str, ref: dict, checker=None) -> dict:
    """{score, why, detail}. `checker(code)` returns the type checker's
    complaint ("" when happy) for Luau; it is never given the code to run."""
    lang = ref["lang"]
    want = [tuple(f) for f in ref["funcs"]]
    code = code_of(text, lang)
    reason, shape = "", []
    if not code:
        why = "none"
    elif lang == "python":
        shape = py_shape(code, want)
        if shape and shape[0].startswith("syntax:"):
            why, reason = "syntax", shape[0]
        else:
            why = "shape" if shape else ""
    else:
        shape = luau_shape(code, want)
        if checker is None:
            why = "unchecked"
        else:
            reason = str(checker(code) or "")
            if reason:
                why = "syntax" if "SyntaxError" in reason else "check"
            elif "not_strict" in shape or "checking_off" in shape:
                why = "loose"          # checked, but not as strictly as asked
            else:
                why = "shape" if shape else ""
    score = 100.0 if not why else 50.0 if why == "shape" else 0.0
    lines = len(code.splitlines()) if code else 0
    first = reason.strip().splitlines()[0] if reason.strip() else ""
    detail = f"lines={lines} funcs={len(want)}"
    if why:
        detail += f" why={why}"
    if shape:
        detail += " shape=" + ",".join(shape[:8])
    if first:
        detail += " first=" + re.sub(r"\s+", "_", first)[:160]
    return {"score": score, "why": why, "shape": shape, "reason": reason, "lines": lines,
            "detail": detail}


# --------------------------------------------------------------------------- #
# Cases                                                                        #
# --------------------------------------------------------------------------- #

def _order(lang: str) -> list:
    funcs = list(LUAU_FUNCS if lang == "luau" else PY_FUNCS)
    random.Random(f"write|{lang}").shuffle(funcs)
    return funcs


def _ask(lang: str, funcs: list) -> str:
    if lang == "luau":
        items = "\n".join(f"{i}. `function M.{n}({p}): {r}` - {w}."
                          for i, (n, p, r, w, _b) in enumerate(funcs, 1))
        return ("Write a Roblox ModuleScript in strict Luau. Its first line is `--!strict`; it "
                "builds a table with `local M = {}` and ends with `return M`. It defines "
                f"{'this function' if len(funcs) == 1 else f'these {len(funcs)} functions'}, "
                "with exactly these names, parameters and types:\n\n" + items +
                "\n\nIt has to pass the Luau type checker in strict mode with Roblox's type "
                "definitions. Reply with the whole module in one ```luau block.")
    items = "\n".join(f"{i}. `def {n}({p}) -> {r}` - {w}."
                      for i, (n, p, r, w, _b) in enumerate(funcs, 1))
    return ("Write a Python 3.12 module that defines "
            f"{'this function' if len(funcs) == 1 else f'these {len(funcs)} functions'} at the "
            "top level, with exactly these names and parameters and with type annotations on "
            "every parameter and return:\n\n" + items +
            "\n\nUse only the standard library. Reply with the whole module in one ```python "
            "block.")


def ideal(lang: str, funcs: list) -> str:
    """A module that meets the spec (each reference body)."""
    if lang == "luau":
        parts = ["--!strict", "local M = {}", ""]
        for n, p, r, _w, body in funcs:
            parts += [f"function M.{n}({p}): {r}", "\t" + body, "end", ""]
        return "```luau\n" + "\n".join(parts + ["return M"]) + "\n```"
    parts = []
    for n, p, r, _w, body in funcs:
        parts += [f"def {n}({p}) -> {r}:", "    " + body, "", ""]
    return "```python\n" + "\n".join(parts).rstrip() + "\n```"


def write_spec(lang: str, level: int = 0, single: str = "") -> dict:
    """A ladder rung (`level` functions from a fixed order) or one function alone."""
    if single:
        funcs = [f for f in (LUAU_FUNCS if lang == "luau" else PY_FUNCS) if f[0] == single]
        task, ladder, lv = f"{lang}-{single}", "", 0
    else:
        lv = max(1, min(WRITE_MAX, int(level)))
        funcs = _order(lang)[:SIZES[lv - 1]]
        task, ladder = f"{lang}-L{lv}", f"code:{lang} module size"
    return {"task": task, "lang": lang, "level": lv, "ladder": ladder, "single": single,
            "funcs": [list(f[:4]) for f in funcs],
            "messages": [{"role": "system", "content": LUAU_SYSTEM if lang == "luau"
                          else PY_SYSTEM},
                         {"role": "user", "content": _ask(lang, funcs)}],
            "ideal": ideal(lang, funcs)}


def write_specs(top_level: int = 0) -> list:
    """Both ladders up to `top_level`, then every Luau function alone."""
    top = max(2, min(WRITE_MAX, int(top_level or WRITE_TOP)))
    out = [write_spec(lang, lv) for lang in LANGS for lv in range(1, top + 1)]
    out += [write_spec("luau", single=f[0]) for f in LUAU_FUNCS]
    return out


def luau_rule(cfg):
    """The operator's Luau type check under Code checks: the first enabled rule
    for luau whose command runs an analyzer (luau-lsp analyze). A formatter
    (stylua --check) says nothing about whether the code is right, so it is not
    used. None when there is no such rule."""
    for r in getattr(cfg, "rules", None) or []:
        d = r if isinstance(r, dict) else r.model_dump()
        langs = [str(x).strip().lower() for x in (d.get("languages") or [])]
        if d.get("enabled", True) and ("luau" in langs or "lua" in langs) \
                and "analyze" in str(d.get("command") or ""):
            return d
    return None
