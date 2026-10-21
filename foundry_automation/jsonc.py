"""A small JSONC (JSON with comments and trailing commas) reader that records
where each value sits in the text, so one value can be replaced while every
comment and the rest of the formatting stay as they were."""

from __future__ import annotations

import json
from typing import Any, Optional


class JsoncError(ValueError):
    pass


class _P:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.spans: dict[tuple, tuple[int, int]] = {}

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n﻿":
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    raise JsoncError(f"unterminated comment at {self.i}")
                self.i = j + 2
            else:
                break

    def err(self, what: str):
        line = self.s.count("\n", 0, self.i) + 1
        raise JsoncError(f"{what} at line {line} (offset {self.i})")

    def value(self, path: tuple) -> Any:
        self.ws()
        if self.i >= len(self.s):
            self.err("unexpected end")
        start = self.i
        c = self.s[self.i]
        if c == "{":
            v = self.obj(path)
        elif c == "[":
            v = self.arr(path)
        elif c == '"':
            v = self.string()
        else:
            v = self.scalar()
        self.spans[path] = (start, self.i)
        return v

    def string(self) -> str:
        s = self.s
        j = self.i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                break
            j += 1
        else:
            self.err("unterminated string")
        raw = s[self.i:j + 1]
        self.i = j + 1
        try:
            return json.loads(raw)
        except ValueError:
            self.err("bad string")

    def scalar(self) -> Any:
        s = self.s
        j = self.i
        while j < len(s) and s[j] not in ",}] \t\r\n/":
            j += 1
        tok = s[self.i:j]
        try:
            v = json.loads(tok)
        except ValueError:
            self.err(f"bad value {tok[:20]!r}")
        self.i = j
        return v

    def obj(self, path: tuple) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "}":
                self.i += 1
                return out
            if self.i >= len(self.s) or self.s[self.i] != '"':
                self.err("expected a key")
            k = self.string()
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != ":":
                self.err("expected ':'")
            self.i += 1
            out[k] = self.value(path + (k,))
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "}":
                continue
            else:
                self.err("expected ',' or '}'")

    def arr(self, path: tuple) -> list:
        self.i += 1
        out: list = []
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value(path + (len(out),)))
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "]":
                continue
            else:
                self.err("expected ',' or ']'")


def parse(text: str) -> Any:
    return parse_with_spans(text)[0]


def parse_with_spans(text: str) -> tuple[Any, dict]:
    p = _P(text)
    v = p.value(())
    p.ws()
    if p.i != len(text):
        p.err("trailing content")
    return v, p.spans


def _indent_at(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    j = line_start
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[line_start:j]


def _dump(value: Any, indent: str) -> str:
    body = json.dumps(value, indent=2, ensure_ascii=False)
    return body.replace("\n", "\n" + indent)


def replace_value(text: str, path: tuple, value: Any) -> str:
    """Replace the value at `path` (a tuple of keys). When the last key is
    missing from its object, it is added as the object's last member."""
    data, spans = parse_with_spans(text)
    if path in spans:
        a, b = spans[path]
        return text[:a] + _dump(value, _indent_at(text, a)) + text[b:]
    parent = path[:-1]
    if parent not in spans:
        raise JsoncError(f"no object at {'.'.join(map(str, parent)) or '<root>'}")
    a, b = spans[parent]
    if text[a] != "{":
        raise JsoncError(f"{'.'.join(map(str, parent))} is not an object")
    close = b - 1                                   # the closing brace
    ind = _indent_at(text, a)
    member_ind = ind + "  "
    member = f'{member_ind}{json.dumps(path[-1])}: {_dump(value, member_ind)}\n{ind}'
    # the closing brace goes on its own line; the new member sits just above it
    k = close
    while k > a + 1 and text[k - 1] in " \t":
        k -= 1
    lead = "" if text[k - 1] == "\n" else "\n"
    out = text[:k] + lead + member + text[close:]
    keys = list(_get(data, parent))
    if keys:
        # a comma right after the last member's value (before any comment)
        _, end = spans[parent + (keys[-1],)]
        p = _P(text)
        p.i = end
        p.ws()                                      # skips comments too
        if text[p.i] != ",":
            out = out[:end] + "," + out[end:]
    return out


def _get(data: Any, path: tuple) -> Any:
    for k in path:
        data = data[k]
    return data


def get(data: Any, path: tuple, default: Optional[Any] = None) -> Any:
    try:
        return _get(data, path)
    except (KeyError, IndexError, TypeError):
        return default
