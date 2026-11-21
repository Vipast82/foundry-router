"""Harder benchmark cases, with difficulty as a dial (v0.181.223).

Read on two finished profiles, the tiers said nothing: both local models, at
three bits a weight, passed every "hard" case in code reasoning and in
long-context recall and 75-100% of "expert". A tier a small local model clears
cannot tell a good model from a better one, and a frontier model was never
going to miss any of it.

Three rules for what is here:

* **Reasoning is allowed and the answer is one line.** A hard case says so:
  work it out, then end with ``ANSWER: <value>``. Only that line is scored
  (bench.final_answer), so a thinking model and one that writes its working in
  the reply are on the same footing, and a reply is never scored on something
  it only mentioned on the way. These cases get THINK_ROOM tokens.
* **Every answer is computed, never recalled.** Code is run when the suite is
  built; a long-context case is built around a value drawn from a seed.
* **Difficulty is a number, not a list.** The trace ladder and the long-context
  kinds are generated from (kind, level, seed): when a level is cleared by
  everything, the next one is one integer away (bench.hard_levels), with no new
  cases to write. Tiers are set from measured pass rates and written down
  beside each family, with the models and the date.

Nothing here is ever run on a model's output; the snippets below are this
module's own code, run once at build time to get their answers.
"""

from __future__ import annotations

import random
from typing import Optional

FINAL_RULE = ("You may work it out step by step first. End your reply with one line of "
              "the form `ANSWER: <value>` - only that line is read.")

# --------------------------------------------------------------------------- #
# Code: stacked semantics                                                     #
# --------------------------------------------------------------------------- #
# Each program stacks several rules that interact, so recalling one rule gives
# a plausible wrong answer. (key, source). The answer is `result`.

CODE_STACKED = [
    ("aug_tuple_mutation", """\
t = ([1], [2])
log = []
try:
    t[0] += [9]
except TypeError:
    log.append('E')
u = t[0]
u *= 2
log.append(len(t[0]))
result = (t, log)"""),
    ("nested_list_alias", """\
grid = [[0] * 3] * 2
grid[0][1] = 5
rows = [r[:] for r in grid]
rows[1][0] = 7
grid.append(grid[0])
grid[2][2] = 9
result = (grid, rows, sum(map(sum, grid)))"""),
    ("finally_loop_else", """\
def f(n):
    out = []
    for i in range(n):
        try:
            if i == 1:
                continue
            if i == 3:
                break
            out.append(i)
        finally:
            out.append(-i)
    else:
        out.append('else')
    return out

def g():
    try:
        return 'try'
    finally:
        return 'fin'

result = (f(5), f(1), g())"""),
    ("except_name_unbound", """\
e = 'outer'

def run(v):
    try:
        return 10 // v
    except ZeroDivisionError as e:
        msg = type(e).__name__[:4]
    try:
        return e
    except NameError:
        return msg + '!'

result = (run(5), run(0), e)"""),
    ("class_scope_comprehension", """\
class K:
    n = 3
    xs = [i * 2 for i in range(n)]
    try:
        ys = [n * i for i in range(2)]
    except NameError:
        ys = 'hidden'
    zs = (lambda n=n: [n + i for i in range(2)])()

result = (K.xs, K.ys, K.zs)"""),
    ("generator_binding", """\
nums = [1, 2, 3]
gen = (x * k for x in nums for k in (1, 10))
nums.append(4)
k = 100
first = next(gen)
nums = [9]
rest = list(gen)
fns = [lambda y, m=m: y * m for m in (2, 3)]
late = [lambda y: y * m for m in (2, 3)]
result = (first, rest, [f(1) for f in fns], [f(1) for f in late])"""),
    ("dict_views_default", """\
d = {'a': 1, 'b': 2}
keys = d.keys()
vals = list(d.values())
d['c'] = d.pop('a') + 10
d.setdefault('b', 99)
d.setdefault('z', []).append(len(keys))
shared = dict.fromkeys('xy', [])
shared['x'].append(1)
result = (list(keys), vals, d['z'], shared, list(d)[0])"""),
    ("sort_stability_reverse", """\
recs = [('k', 2), ('a', 1), ('m', 2), ('b', 1), ('z', 3)]
one = sorted(recs, key=lambda r: r[1], reverse=True)
two = sorted(sorted(recs), key=lambda r: r[1] % 2)
names = lambda rs: ''.join(n for n, _ in rs)
top = max(recs, key=lambda r: r[1] % 2)[0]
low = min(recs, key=lambda r: -r[1] // 2)[0]
result = (names(one), names(two), top, low)"""),
    ("int_float_rules", """\
r = [round(v) for v in (0.5, 1.5, 2.5, -0.5, -1.5)]
q = (-7 // 2, -7 % 2, 7 // -2, 7 % -2, divmod(-7, -2))
p = (-2 ** 2, 2 ** -1, 2 ** 3 ** 2 % 7, int(-3.9), round(-3.5))
b = (True + True, sum([True, False, True]) * '1', not 0 == 1, 1 < 2 == 2)
result = (r, q, p, b)"""),
    ("property_shadow", """\
class Temp:
    def __init__(self):
        self._c = 0
        self.sets = 0
    @property
    def c(self):
        return self._c
    @c.setter
    def c(self, v):
        self.sets += 1
        self._c = max(v, -273)
    def __getattr__(self, name):
        return name.upper()

t = Temp()
t.c = -500
t.c += 10
t.__dict__['c'] = 'shadow'
t.k = 5
result = (t.c, t.sets, t.f, t.k, 'c' in vars(t))"""),
    ("mro_kwargs", """\
class Base:
    def __init__(self, **kw):
        self.log = ['Base' + ''.join(sorted(kw))]
class A(Base):
    def __init__(self, a=1, **kw):
        super().__init__(**kw)
        self.log.append(f'A{a}')
class B(Base):
    def __init__(self, b=2, **kw):
        super().__init__(a=b, **kw)
        self.log.append(f'B{b}')
class C(A, B):
    def __init__(self, **kw):
        super().__init__(a=7, **kw)
        self.log.append('C')
class D(B, A):
    pass

result = (C(b=3, z=0).log, D(b=4).log, [k.__name__ for k in D.__mro__][1:3])"""),
    ("iterator_traps", """\
it = iter([1, 2, 3, 4, 5, 6])
pairs = list(zip(it, it))
it2 = iter(range(7))
chunks = [x for x in it2 if next(it2, None) is not None]
data = [1, 2, 3, 4]
seen = []
for x in data:
    seen.append(x)
    if x % 2 == 0:
        data.remove(x)
e = enumerate('abc', 1)
next(e)
result = (pairs, chunks, seen, data, dict(e), 3 in it)"""),
    ("closure_cells", """\
def make():
    fs = []
    for i in range(3):
        def f(j=i):
            return i * 10 + j
        fs.append(f)
    i = 7
    return fs

a, b, c = make()

def counter():
    n = 0
    def inc():
        nonlocal n
        n += 1
        return n
    def peek():
        return n
    return inc, peek

inc, peek = counter()
inc(); inc()
inc2, peek2 = counter()
result = (a(), b(5), c(), peek(), peek2(), inc2() + inc())"""),
    ("context_managers", """\
log = []

class Res:
    def __init__(self, name, swallow):
        self.name, self.swallow = name, swallow
    def __enter__(self):
        log.append('+' + self.name)
        return self
    def __exit__(self, et, ev, tb):
        log.append('-' + self.name + ('!' if et else ''))
        return self.swallow

def work():
    with Res('a', True), Res('b', False):
        log.append('body')
        raise ValueError('x')
    log.append('after')
    return 'ok'

try:
    with Res('c', False):
        out = work()
        1 / 0
except ZeroDivisionError:
    log.append('Z')
result = (out, log)"""),
    ("yield_from_return", """\
def inner():
    x = yield 1
    y = yield (x or 0) * 2
    return (x or 0) + (y or 0)

def outer():
    total = yield from inner()
    yield total
    yield from 'ab'
    return 'end'

g = outer()
out = [next(g), g.send(5), g.send(3)]
out += list(g)

def wrap():
    r = yield from outer()
    yield r

result = (out, [v for v in wrap()])"""),
    ("slice_assignment", """\
xs = list(range(8))
xs[1:4] = 'ab'
xs[::3] = [0, 0, 0]
ys = xs
ys = ys + [9]
xs += [8]
del xs[-3:-1]
zs = xs[:]
zs[2:2] = xs[:1]
result = (xs, ys, zs, xs is ys)"""),
]

# --------------------------------------------------------------------------- #
# Code: names that lie                                                        #
# --------------------------------------------------------------------------- #
# The identifiers, docstrings and comments say one thing and the code does
# another. Read for what it claims, each has an obvious wrong answer; the right
# one is only what the statements do.

CODE_LYING = [
    ("shadowed_builtins", """\
len = lambda xs: sum(1 for x in xs if x)
sorted_desc = sorted          # sorts in descending order

def maximum(xs):
    \"\"\"Return the largest element.\"\"\"
    return min(xs, key=lambda v: -abs(v))

data = [3, 0, -8, 5, 0, -1]
list = lambda it: [v for v in it][::-1]
top = sorted_desc(data)[:2]
result = (len(data), maximum(data), top, list(range(len(data))))"""),
    ("operators_that_lie", """\
class Money:
    def __init__(self, c): self.c = c
    def __add__(self, o): return Money(self.c - getattr(o, 'c', o))
    def __radd__(self, o): return Money(self.c + o * 2)
    def __eq__(self, o): return self.c != getattr(o, 'c', o)
    def __bool__(self): return self.c < 0
    def __repr__(self): return f'M{self.c}'

a, b = Money(10), Money(4)
total = sum([a, b])
result = (repr(a + b), repr(total), a == b, a == Money(10), bool(a), bool(a + 20),
          [a, b].count(a))"""),
    ("helpers_that_lie", """\
def remove_duplicates(items):
    # keeps the first occurrence of each item
    out = []
    for x in items:
        if x in out:
            out.remove(x)
        out.append(x)
    return out

def is_even(n):
    return n % 2 == 1 if n > 10 else n % 2 == 0

def double_all(xs):
    xs = xs + xs
    return xs[1::2]

vals = [3, 12, 3, 8, 12, 15, 8]
u = remove_duplicates(vals)
result = (u, [v for v in u if is_even(v)], double_all(u), len(vals))"""),
    ("stack_that_is_a_queue", """\
class Stack:
    def __init__(self): self.items = []
    def push(self, x): self.items.insert(0, x); return self
    def pop(self): return self.items.pop()
    def peek(self): return self.items[0]

s = Stack()
s.push(1).push(2).push(3)
first = s.pop()
Stack.push = lambda self, x: self.items.append(x * 2) or self
s.push(4)
s.pop = lambda: s.items.pop(0)
second = s.pop()
t = Stack()
t.push(5).push(6)
result = (first, second, s.peek(), s.items, t.pop(), t.items)"""),
    ("protocols_that_lie", """\
class Bag:
    def __init__(self, *xs): self.xs = list(xs)
    def __len__(self): return 0
    def __iter__(self): return iter(self.xs[::2])
    def __contains__(self, v): return v not in self.xs
    def __getitem__(self, i): return self.xs[-i - 1]

b = Bag(4, 7, 9, 2)
status = 'empty' if not b else 'full'
result = (status, list(b), 7 in b, 5 in b, b[0], sum(b), max(b),
          [x for x in b if x in b])"""),
    ("scopes_that_lie", """\
total = 100

def add_to_total(x):
    total = x          # update the running total
    return total

def reset():
    global total
    total += 1
    return total

def scale(values, total=total):
    return [v * total for v in values]

add_to_total(5)
reset()
count = 0

def bump(count=count):
    count += 1
    return count

bump(); bump()
result = (total, scale([1, 2]), bump(), count, add_to_total(3))"""),
    ("cache_that_forgets", """\
calls = []

def memoize(fn):
    cache = {}
    def wrapper(n, _seen=[]):
        key = n % 3                    # cache by argument
        if key not in cache:
            calls.append(n)
            cache[key] = fn(n)
        _seen.append(n)
        return cache[key] + len(_seen)
    return wrapper

@memoize
def square(n):
    return n * n

out = [square(n) for n in (2, 5, 3, 8, 6)]
result = (out, calls)"""),
    ("search_that_misses", """\
def binary_search(xs, target):
    # index of target in sorted xs, or -1
    lo, hi = 0, len(xs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < target:
            lo = mid + 1
        elif xs[mid] > target:
            hi = mid - 1
        else:
            return mid
    return -1

def sort_list(xs):
    # sort ascending
    xs = list(xs)
    for i in range(len(xs) - 1):
        if xs[i] > xs[i + 1]:
            xs[i], xs[i + 1] = xs[i + 1], xs[i]
    return xs

data = sort_list([7, 3, 9, 1, 12, 5])
result = (data, [binary_search([1, 3, 5, 7, 9, 11], t) for t in (1, 5, 9, 11, 4)])"""),
    ("text_helpers_that_lie", """\
def is_palindrome(s):
    s = s.lstrip().lower()
    return s == s[::-1]

def count_words(text):
    return len(text.split(' '))

def reverse_words(text):
    return ' '.join(w[::-1] for w in text.split())

def title_case(text):
    return text[:1].upper() + text[1:]

line = '  Never odd  or even '
result = (is_palindrome(' Level'), is_palindrome('level '), count_words(line),
          reverse_words(line), title_case('hello world'))"""),
    ("math_helpers_that_lie", """\
def clamp(x, lo, hi):
    return max(hi, min(lo, x))

def average(xs):
    kept = [x for x in xs if x >= 0]
    return sum(kept) // len(xs)

def fizzbuzz(n):
    if n % 3 == 0:
        return 'Fizz'
    elif n % 5 == 0:
        return 'Buzz'
    elif n % 15 == 0:
        return 'FizzBuzz'
    return str(n)

def rotate_left(xs, n):
    n %= len(xs)
    return xs[-n:] + xs[:-n]

result = ([clamp(v, 0, 10) for v in (-5, 5, 15)], average([4, -2, 9, -1]),
          '-'.join(fizzbuzz(n) for n in (9, 10, 15, 7)), rotate_left([1, 2, 3, 4, 5], 2),
          rotate_left([1, 2, 3], 3))"""),
    ("collections_that_lie", """\
def merge(a, b):
    # merge two dicts; values in b win
    out = dict(b)
    for k, v in a.items():
        out.setdefault(k, v)
        if v is not None:
            out[k] = v
    return out

def unique_sorted(xs):
    return sorted(set(xs), key=str)

def flatten(xs):
    # flatten every level of nesting
    out = []
    for x in xs:
        out.extend(x if isinstance(x, list) else [x])
    return out

def top_two(scores):
    return sorted(scores, key=scores.get)[:2]

result = (merge({'a': 1, 'b': None, 'c': 7}, {'b': 2, 'c': 3}), unique_sorted([10, 9, 100, 9, 2]),
          flatten([1, [2, [3, 4]], 'ab', [[5]]]), top_two({'x': 3, 'y': 9, 'z': 5}))"""),
    ("state_that_lies", """\
class Counter:
    count = 0
    def increment(self):
        self.count += 1
        return self
    def reset(self):
        Counter.count = 10

class Account:
    def __init__(self, balance=0, history=[]):
        self.balance = balance
        self.history = history
    def deposit(self, n):
        self.history.append(n)
        self.balance += n
        return self.balance

a, b = Counter(), Counter()
a.increment().increment()
b.reset()
b.increment()
x, y = Account(), Account(5)
x.deposit(10)
y.deposit(1)
result = (a.count, b.count, Counter.count, Counter().count, x.balance, y.balance,
          x.history, y.history is x.history)"""),
    ("loops_that_lie", """\
def first_even(xs):
    for i, x in enumerate(xs):
        if x % 2 == 0:
            break
    else:
        i = -1
    return i

def countdown(n):
    out = []
    while n:
        out.append(n)
        n -= 2
        if n < -3:
            break
    return out

def pairs(xs):
    return [(a, b) for i, a in enumerate(xs) for b in xs[i:]]

def running_max(xs):
    best, out = 0, []
    for x in xs:
        best = max(best, x)
        out.append(best)
    return out

result = (first_even([3, 5, 8]), first_even([1, 3]), countdown(5), countdown(4),
          len(pairs([1, 2, 3])), running_max([-4, -2, -9]))"""),
    ("validators_that_lie", """\
def all_positive(xs):
    return any(x > 0 for x in xs)

def none_empty(rows):
    return all(rows)

def first_true(xs, default=None):
    for x in xs:
        if x is True:
            return x
    return default

rows = [[0], [], [1, 2]]
checks = (all_positive([-1, 0, 2]), none_empty(rows), none_empty([]),
          first_true([1, 'yes', 2.0], 'none'), first_true([0, True]))
flags = [bool(r) and r[-1] for r in rows]
result = (checks, flags, sum(map(len, rows)) == len(sum(rows, [])))"""),
]

# --------------------------------------------------------------------------- #
# Code: the trace ladder                                                      #
# --------------------------------------------------------------------------- #
# Programs generated from (kind, level, seed). The program text is the same
# shape at every level; the level sets how many steps have to be carried
# without slipping (8, 16, 32, 64, 128...). One slip anywhere is a wrong
# answer, so this measures how far a model can be trusted to follow state.

TRACE_KINDS = ("ledger", "grid", "text", "queue", "mix")


def trace_steps(level: int) -> int:
    return 8 * 2 ** (max(1, int(level)) - 1)


def _ledger(rnd: random.Random, steps: int) -> str:
    names = ["ana", "bo", "cy", "di"]
    bal = {n: rnd.choice((0, 15, 20, 35, 50, 60)) for n in names}
    ops = []
    for _ in range(steps):
        k = rnd.random()
        if k < 0.6:
            a, b = rnd.sample(names, 2)
            ops.append(("pay", a, b, rnd.choice((5, 10, 15, 25, 40))))
        elif k < 0.85:
            ops.append(("fee", rnd.choice(names), rnd.choice((3, 7, 12))))
        else:
            ops.append(("split", rnd.choice(names), rnd.choice((2, 3))))
    return f"""\
bal = {bal!r}
ops = {ops!r}
bounced = 0
for op in ops:
    if op[0] == 'pay':
        _, a, b, amt = op
        if bal[a] < amt:
            bounced += 1
            if bal[a] > 0:
                bal[a] -= 1
            continue
        bal[a] -= amt
        bal[b] += amt
        if bal[b] >= 100:
            bal[b] -= 10
            bal['cy'] += 10
    elif op[0] == 'fee':
        _, a, amt = op
        bal[a] = max(0, bal[a] - amt)
    else:
        _, a, n = op
        share = bal[a] // n
        bal[a] -= share
        poorest = min(bal, key=lambda k: (bal[k], k))
        bal[poorest] += share
result = (bal, bounced)"""


def _grid(rnd: random.Random, steps: int) -> str:
    moves = "".join(rnd.choice("FFFLR") for _ in range(steps))
    walls = sorted({(rnd.randint(0, 4), rnd.randint(0, 4)) for _ in range(4)} - {(0, 0)})
    return f"""\
moves = {moves!r}
walls = set({walls!r})
x, y, d = 0, 0, 0                    # d: 0 north, 1 east, 2 south, 3 west
seen = {{(0, 0)}}
bumps = 0
for m in moves:
    if m == 'L':
        d = (d - 1) % 4
    elif m == 'R':
        d = (d + 1) % 4
    else:
        dx, dy = ((0, 1), (1, 0), (0, -1), (-1, 0))[d]
        nx, ny = x + dx, y + dy
        if (nx, ny) in walls or not (0 <= nx < 5 and 0 <= ny < 5):
            bumps += 1
            d = (d + 2) % 4 if bumps % 2 else (d + 1) % 4
        else:
            x, y = nx, ny
            seen.add((x, y))
result = (x, y, d, len(seen), bumps)"""


def _text(rnd: random.Random, steps: int) -> str:
    s = "".join(rnd.sample("abdehikmoqrtux", 8))
    return f"""\
s = {s!r}
for step in range({steps}):
    c = s[step % len(s)]
    if c in 'aeiou':
        s = s[1:] + s[0].upper()
    elif c.isupper():
        s = s[::-1].lower()
    elif ord(c) % 2:
        s = s.replace(c, chr(ord(c) + 1), 1)
    else:
        s = s[-1] + s[:-1]
result = s"""


def _queue(rnd: random.Random, steps: int) -> str:
    n = max(3, steps // 4)
    jobs = [(chr(97 + i % 26) + (str(i // 26) if i >= 26 else ""), rnd.randint(1, 6))
            for i in range(n)]
    return f"""\
from collections import deque
q = deque({jobs!r})
t, done, QUANTUM = 0, [], 2
while q:
    name, left = q.popleft()
    run = min(QUANTUM, left)
    t += run
    if left - run:
        if t % 3 == 0:
            q.appendleft((name, left - run))
        else:
            q.append((name, left - run))
    else:
        done.append(name + str(t))
result = ' '.join(done)"""


def _mix(rnd: random.Random, steps: int) -> str:
    word = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(steps))
    return f"""\
h, flips = 7, 0
for i, ch in enumerate({word!r}):
    h = (h * 31 + ord(ch)) % 997
    if h % 5 == 0:
        h ^= 0x55
        flips += 1
    elif i % 4 == 3:
        h = h // 2 + flips
result = (h, flips)"""


_TRACE = {"ledger": _ledger, "grid": _grid, "text": _text, "queue": _queue, "mix": _mix}


def trace_source(kind: str, level: int, seed: int) -> str:
    rnd = random.Random(f"trace-{kind}-{level}-{seed}")
    return _TRACE[kind](rnd, trace_steps(level))


# --------------------------------------------------------------------------- #
# Code: understand it, because it cannot be traced                            #
# --------------------------------------------------------------------------- #
# Programs that loop thousands or millions of times, or search every
# arrangement. Nobody traces a million iterations: the answer comes from seeing
# what the program computes - a cycle, a count, a table, a best arrangement -
# and working THAT out. Sonnet 5.5 followed every 64-step trace above without a
# slip (2026-11-21); following is not what is hard for a frontier model.
# Generated from (kind, level, seed); the level sets the size of the problem.

INSIGHT_KINDS = ("cycle", "count", "coins", "paths", "lcs", "calls", "josephus",
                 "tour", "subset", "perms", "colorings")


def _size(table: tuple, level: int):
    """The size for a level: the table's entry, or its last two entries'
    growth carried on, so a level past the table is still a bigger problem."""
    level = max(1, int(level))
    if level <= len(table):
        return table[level - 1]
    step = table[-1] - table[-2]
    return table[-1] + step * (level - len(table))


def _cycle(rnd: random.Random, level: int) -> str:
    m = rnd.choice(((47, 53, 59), (193, 211, 223), (997, 1009, 1013), (4999, 5003, 5009),
                    (20011, 20021, 20023), (80021, 80039, 80051),
                    (320009, 320011, 320027))[min(7, max(1, level)) - 1])
    n = rnd.choice((10 ** 6, 2 * 10 ** 6, 10 ** 6 + 7)) + rnd.randint(1, 99)
    return f"""\
x = {rnd.randint(2, m - 2)}
for _ in range({n}):
    x = (x * x + {rnd.randint(1, 9)}) % {m}
result = x"""


def _count(rnd: random.Random, level: int) -> str:
    top = (300, 2000, 12000, 80000, 500000, 3000000)[min(6, max(1, level)) - 1]
    a = rnd.choice((7, 11, 13))
    r = rnd.randint(0, a - 1)
    pred = rnd.choice((
        f"n % {a} == {r} and str(n) == str(n)[::-1]",
        f"sum(map(int, str(n))) == {rnd.randint(9, 14)} and n % {a} == {r}",
        f"len(set(str(n))) == len(str(n)) and n % {a} == {r} and n % 10 > n // 10 % 10",
        f"bin(n).count('1') == {rnd.randint(3, 5)} and n % {a} == {r}",
    ))
    return f"result = sum(1 for n in range(1, {top}) if {pred})"


def _coins(rnd: random.Random, level: int) -> str:
    target = (23, 47, 89, 173, 331, 677, 1361)[min(7, max(1, level)) - 1] + rnd.randint(0, 6)
    coins = sorted(rnd.sample((2, 3, 5, 7, 9, 11, 13, 17), 3 + (level > 2) + (level > 5)))
    return f"""\
coins, target = {coins!r}, {target}
ways = [1] + [0] * target
for c in coins:
    for t in range(c, target + 1):
        ways[t] += ways[t - c]
result = ways[target]"""


def _paths(rnd: random.Random, level: int) -> str:
    n = _size((4, 5, 7, 9, 11, 13, 15), level)
    cells = [(r, c) for r in range(n) for c in range(n) if (r, c) not in ((0, 0), (n - 1, n - 1))]
    walls = sorted(rnd.sample(cells, max(2, n * n // 6)))
    return f"""\
n = {n}
walls = set({walls!r})
ways = [[0] * n for _ in range(n)]
ways[0][0] = 1
for r in range(n):
    for c in range(n):
        if (r, c) in walls:
            ways[r][c] = 0
            continue
        if r:
            ways[r][c] += ways[r - 1][c]
        if c:
            ways[r][c] += ways[r][c - 1]
        if r and c and (r + c) % 3 == 0:
            ways[r][c] += ways[r - 1][c - 1]
result = ways[n - 1][n - 1]"""


def _lcs(rnd: random.Random, level: int) -> str:
    k = _size((6, 9, 13, 18, 24, 32, 42), level)
    a = "".join(rnd.choice("abcd") for _ in range(k))
    b = "".join(rnd.choice("abcd") for _ in range(k + 1))
    return f"""\
a, b = {a!r}, {b!r}
t = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
for i, x in enumerate(a, 1):
    for j, y in enumerate(b, 1):
        t[i][j] = t[i - 1][j - 1] + 1 if x == y else max(t[i - 1][j], t[i][j - 1])
edits = len(a) + len(b) - 2 * t[-1][-1]
result = (t[-1][-1], edits)"""


def _calls(rnd: random.Random, level: int) -> str:
    n = _size((9, 14, 20, 26), min(4, level)) + rnd.randint(0, 2)   # 26 is ~10^5 calls
    k = rnd.choice((2, 3))
    return f"""\
calls = 0

def f(n):
    global calls
    calls += 1
    if n < {k}:
        return n
    return f(n - 1) + f(n - {k})

result = (f({n}), calls)"""


def _josephus(rnd: random.Random, level: int) -> str:
    n = (9, 19, 41, 97, 203, 409, 811)[min(7, max(1, level)) - 1] + rnd.randint(0, 5)
    k = rnd.choice((3, 4, 5, 7))
    return f"""\
people = list(range(1, {n + 1}))
i, out = 0, []
while len(people) > 1:
    i = (i + {k - 1}) % len(people)
    out.append(people.pop(i))
result = (people[0], out[-1], out[len(out) // 2])"""


def _tour(rnd: random.Random, level: int) -> str:
    n = min(10, _size((5, 6, 7, 8, 9, 10), level))        # 9! orderings is the most to build
    pts = [(rnd.randint(0, 9), rnd.randint(0, 9)) for _ in range(n)]
    d = [[abs(a[0] - b[0]) + abs(a[1] - b[1]) + ((i * 7 + j * 3) % 4 if i != j else 0)
          for j, b in enumerate(pts)] for i, a in enumerate(pts)]
    return f"""\
from itertools import permutations
d = {d!r}
best = min(sum(d[a][b] for a, b in zip((0,) + p, p + (0,)))
           for p in permutations(range(1, {n})))
result = best"""


def _subset(rnd: random.Random, level: int) -> str:
    k = min(18, _size((6, 9, 12, 15, 18), level))
    xs = sorted(rnd.sample(range(11, 11 + k * 9), k))
    cap = sum(xs) * rnd.choice((4, 5)) // 10 + rnd.randint(0, 7)
    return f"""\
from itertools import combinations
xs, cap = {xs!r}, {cap}
best = max((sum(c), -len(c)) for r in range(len(xs) + 1)
           for c in combinations(xs, r) if sum(c) <= cap)
result = (best[0], -best[1])"""


def _perms(rnd: random.Random, level: int) -> str:
    """How many orderings obey a made-up rule between neighbours. There is no
    table to fill in: the honest method tracks which items are used and which
    came last, and that grows as 2^n."""
    n = min(9, _size((4, 5, 6, 7, 8, 9), level))
    a, b, m = rnd.choice((2, 3)), rnd.choice((1, 2)), rnd.choice((3, 4, 5))
    return f"""from itertools import permutations
result = sum(1 for p in permutations(range(1, {n + 1}))
             if all(({a} * x + {b} * y) % {m} != 0 for x, y in zip(p, p[1:])))"""


def _colorings(rnd: random.Random, level: int) -> str:
    """How many ways to colour a small graph so no edge joins two of a kind."""
    n = min(12, _size((5, 6, 7, 8, 10, 12), level))
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    ring = [(i, i + 1) for i in range(n - 1)]
    extra = rnd.sample([e for e in pairs if e not in ring], max(2, n * 3 // 4))
    edges = sorted(ring + extra)
    return f"""from itertools import product
edges = {edges!r}
result = sum(1 for c in product(range(3), repeat={n})
             if all(c[a] != c[b] for a, b in edges))"""


# -- no structure to lean on ---------------------------------------------------
# Sonnet 5.5 and Opus 5.5 solved every kind above to level 6 or 7 by hand
# (2026-11-21): a rule like (2x + y) % 3 collapses into residue classes, a
# sparse graph colours by elimination, a near-geometric tour falls to a greedy
# guess. These draw their constraints at random, so there is nothing to
# collapse: the only method is exhaustive, and it doubles with every step up.

def _hampaths(rnd: random.Random, level: int) -> str:
    """Orderings where every neighbouring pair is on a random allowed list."""
    n = min(9, _size((4, 5, 6, 7, 8, 9), level))
    pairs = [(a, b) for a in range(n) for b in range(n) if a != b]
    ok = sorted(rnd.sample(pairs, len(pairs) * 9 // 20))
    return f"""from itertools import permutations
ok = set({ok!r})
result = sum(1 for p in permutations(range({n}))
             if all((x, y) in ok for x, y in zip(p, p[1:])))"""


def _sat(rnd: random.Random, level: int) -> str:
    """How many true/false assignments satisfy a random list of 3-term rules."""
    n = min(16, _size((5, 7, 9, 11, 13, 15), level))
    clauses = []
    for _ in range(n * 3 - 2):
        vs = rnd.sample(range(1, n + 1), 3)
        clauses.append(tuple(v if rnd.random() < 0.5 else -v for v in sorted(vs)))
    return f"""from itertools import product
rules = {clauses!r}
result = sum(1 for bits in product((False, True), repeat={n})
             if all(any(bits[abs(t) - 1] == (t > 0) for t in rule) for rule in rules))"""


def _route(rnd: random.Random, level: int) -> str:
    """The cheapest round trip when the costs are random and one-way."""
    n = min(10, _size((5, 6, 7, 8, 9, 10), level))
    d = [[0 if i == j else rnd.randint(3, 40) for j in range(n)] for i in range(n)]
    return f"""from itertools import permutations
cost = {d!r}
result = min(sum(cost[a][b] for a, b in zip((0,) + p, p + (0,)))
             for p in permutations(range(1, {n})))"""


def _dense(rnd: random.Random, level: int) -> str:
    """Four-colourings of a dense random graph."""
    n = min(10, _size((5, 6, 7, 8, 9, 10), level))
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    edges = sorted(rnd.sample(pairs, min(len(pairs), n * 2)))
    return f"""from itertools import product
edges = {edges!r}
result = sum(1 for c in product(range(4), repeat={n})
             if all(c[a] != c[b] for a, b in edges))"""


SEARCH_KINDS = ("hampaths", "sat", "route", "dense")

_INSIGHT = {"hampaths": _hampaths, "sat": _sat, "route": _route, "dense": _dense,
            "perms": _perms, "colorings": _colorings, "cycle": _cycle, "count": _count, "coins": _coins, "paths": _paths,
            "lcs": _lcs, "calls": _calls, "josephus": _josephus, "tour": _tour,
            "subset": _subset}


def insight_source(kind: str, level: int, seed: int) -> str:
    rnd = random.Random(f"insight-{kind}-{level}-{seed}")
    return _INSIGHT[kind](rnd, int(level))


# --------------------------------------------------------------------------- #
# What goes in the suite, and how hard each rung is                           #
# --------------------------------------------------------------------------- #
# Tiers are measured, not declared. Method (docs/BENCH_DIFFICULTY.md): every
# candidate was sent to the two local models' stronger one (qwen3.8-flash, 3-bit)
# and to Sonnet 5.5 and Opus 5.5, with reasoning allowed and an 8,192-token
# budget, on 2026-11-21.
#
#   easy    the local model passes it                 (flash >= 90%)
#   medium  local models miss, frontier does not      (flash ~50%, frontier 100%)
#   hard    frontier models miss some                 (Sonnet/Opus 50-90%)
#   expert  frontier models fail inside the budget    (Sonnet/Opus <= ~33%)
#
# When a rung stops separating anything, raise `top_level`: the next rung is
# generated, and its tier is the last one in its row until it is measured.

# programs kept from the two hand-written families (the rest stay in the module
# for a later, weaker model): flash 24/24, Sonnet 23/24, Opus 14/14 -> easy
CODE_CORE = ("aug_tuple_mutation", "finally_loop_else", "except_name_unbound",
             "class_scope_comprehension", "generator_binding", "mro_kwargs",
             "context_managers", "slice_assignment",
             "operators_that_lie", "scopes_that_lie", "shadowed_builtins",
             "protocols_that_lie", "search_that_misses", "state_that_lies",
             "collections_that_lie", "cache_that_forgets")

# ladder -> (first level in the suite, the tier of each level from 1)
LADDERS = {
    # traces, steps 8 16 32 64 128 256 512. flash: L1-2 90%, L3-4 55%.
    # Sonnet L1-4 39/40, L5-6 9/10. Opus L5-6 6/10.
    "trace:ledger": (1, ("easy", "easy", "medium", "medium", "hard", "hard", "expert")),
    "trace:grid": (1, ("easy", "easy", "medium", "medium", "hard", "hard", "expert")),
    "trace:queue": (1, ("easy", "easy", "medium", "medium", "hard", "hard", "expert")),
    "trace:mix": (1, ("easy", "medium", "medium", "medium", "hard", "expert", "expert")),
    # the seven below: flash passed level 2 of every one, and level 3 only of
    # count, coins and lcs (and coins level 4).
    # no structure: Sonnet missed hampaths L4; both timed out at L6 (9 items).
    "hampaths": (2, ("easy", "easy", "medium", "hard", "hard", "expert", "expert")),
    # sat L6 (15 variables): Opus in 6.2k tokens, Sonnet over budget at 10.6k.
    "sat": (2, ("easy", "easy", "medium", "medium", "hard", "hard", "expert")),
    # route L5: Sonnet 6.0k, Opus 9.0k tokens. L6 (10 stops): both failed.
    "route": (2, ("easy", "easy", "medium", "medium", "hard", "expert", "expert")),
    # structure, but too big to finish: at L5 Sonnet spent 84k tokens on count
    # and 20k on cycle, wrong both times; coins L5 right in 9.9k (over budget).
    "count": (2, ("easy", "easy", "easy", "medium", "expert", "expert")),
    "coins": (2, ("easy", "easy", "easy", "easy", "hard", "expert", "expert")),
    "cycle": (2, ("easy", "easy", "medium", "medium", "expert", "expert", "expert")),
    # lcs L5: Sonnet right in 6.5k; L6 wrong at 11.8k.
    "lcs": (2, ("easy", "easy", "easy", "medium", "hard", "expert", "expert")),
}
TOP_LEVEL = 6


def ladder_tier(ladder: str, level: int) -> str:
    row = LADDERS[ladder][1]
    return row[min(int(level), len(row)) - 1]


def _trivial(v) -> bool:
    """An answer a model could give without doing the work."""
    return v in (0, 1, None, "", [], ()) or (
        isinstance(v, tuple) and all(x in (0, 1) for x in v))


def code_specs(run, top_level: int = TOP_LEVEL) -> list:
    """Every hard code case as {key, src, value, tier, family, ladder, level}.

    `run(src)` returns the program's `result` (bench._run_snippet): passed in so
    this module runs nothing itself. Ladders come out lowest level first, which
    is what lets a run stop climbing one (bench._run)."""
    out = []
    table = dict(CODE_STACKED + CODE_LYING)
    for key in CODE_CORE:
        fam = "stacked" if any(k == key for k, _s in CODE_STACKED) else "lying"
        out.append({"key": key, "src": table[key], "value": run(table[key]), "tier": "easy",
                    "family": fam, "ladder": "", "level": 0})
    top = max(1, min(9, int(top_level or TOP_LEVEL)))
    for ladder, (first, _tiers) in LADDERS.items():
        for level in range(first, top + 1):
            seed, value, src = 0, None, ""
            for _try in range(20):                 # never ship a guessable answer
                src = (trace_source(ladder.split(":", 1)[1], level, seed)
                       if ladder.startswith("trace:") else insight_source(ladder, level, seed))
                value = run(src)
                if not _trivial(value):
                    break
                seed += 100
            out.append({"key": f"{ladder.replace(':', '-')}-L{level}", "src": src,
                        "value": value, "tier": ladder_tier(ladder, level),
                        "family": "trace" if ladder.startswith("trace:") else "search",
                        "ladder": ladder, "level": level})
    return out
