"""Secret and personal-data redaction for captured records.

Used twice: by Foundry before a capture is written (redact-before-persist)
and by the pipeline again at normalization and at export (export-time
validation). Findings record WHAT was found and WHERE (a JSON path), never
the value.

Detected:
  - values you pass in (resolved config secrets, client keys, secret env vars)
  - authorization / cookie / api-key headers and `Bearer ...` tokens
  - private-key blocks, AWS access keys and secret assignments, GitHub / Slack /
    Google / OpenAI / Anthropic style keys, JWTs, Roblox .ROBLOSECURITY
  - `password=` / `token: ` / `api_key = "..."` style assignments
  - e-mail addresses (personal data), except configured allowed domains
A redactor is a floor, not a guarantee: anything that looks like a secret and
isn't caught is a reason to exclude a record, which validate.py re-checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

TOKEN = "[REDACTED:{kind}]"
DROP_KEYS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
             "x-foundry-key", "x-foundry-internal", "api-key", "x-goog-api-key"}

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE "
                               r"KEY-----|\Z)", re.S)),
    ("roblox-cookie", re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS[^\s\"']*")),
    ("roblox-cookie", re.compile(r"(?i)(\.ROBLOSECURITY\s*[=:]\s*)[^\s;\"']+")),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-=]{12,}")),
    ("auth-header", re.compile(r"(?im)^(\s*(?:authorization|cookie|set-cookie|x-api-key)\s*:\s*).+$")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|live-)?[A-Za-z0-9_\-]{16,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}")),
    ("assignment", re.compile(
        r"(?i)((?:password|passwd|pwd|secret|client_secret|api[_-]?key|apikey|access[_-]?key|"
        r"auth[_-]?token|token|aws_secret_access_key)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})")),
]
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}\b")
# Placeholders that look like assignments but aren't secrets.
_SAFE_VALUES = re.compile(r"^(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|<[^>]+>|\[REDACTED[^\]]*\]|x+|\*+|"
                          r"null|none|true|false|changeme|example|your[-_].*|\{env:[^}]+\})$",
                          re.I)


@dataclass
class Findings:
    items: list[dict] = field(default_factory=list)

    def add(self, kind: str, path: str) -> None:
        self.items.append({"kind": kind, "path": path})

    @property
    def count(self) -> int:
        return len(self.items)

    def kinds(self) -> dict:
        out: dict = {}
        for i in self.items:
            out[i["kind"]] = out.get(i["kind"], 0) + 1
        return out

    def status(self) -> str:
        return "clean" if not self.items else "redacted"


class Redactor:
    def __init__(self, known_secrets: Iterable[str] = (), allowed_email_domains: Iterable[str] = (),
                 redact_emails: bool = True):
        self.known = sorted({s for s in known_secrets if s and len(s) >= 6}, key=len, reverse=True)
        self.allowed = {d.lower().lstrip("@") for d in allowed_email_domains}
        self.redact_emails = redact_emails

    def text(self, s: str, path: str = "", f: Findings | None = None) -> str:
        if not s or not isinstance(s, str):
            return s
        f = f if f is not None else Findings()
        for k in self.known:
            if k in s:
                s = s.replace(k, TOKEN.format(kind="known-secret"))
                f.add("known-secret", path)
        for kind, rx in PATTERNS:
            def sub(m, kind=kind):
                if kind == "assignment":
                    if _SAFE_VALUES.match(m.group(2)):
                        return m.group(0)
                    f.add(kind, path)
                    return m.group(1) + TOKEN.format(kind="secret")
                f.add(kind, path)
                if m.re.groups and kind in ("bearer", "auth-header", "roblox-cookie") \
                        and m.lastindex:
                    return m.group(1) + TOKEN.format(kind=kind)
                return TOKEN.format(kind=kind)
            s = rx.sub(sub, s)
        if self.redact_emails:
            def esub(m):
                dom = m.group(0).split("@", 1)[1].lower()
                if dom in self.allowed or any(dom.endswith("." + a) for a in self.allowed):
                    return m.group(0)
                f.add("email", path)
                return TOKEN.format(kind="email")
            s = EMAIL.sub(esub, s)
        return s

    def obj(self, o: Any, path: str = "$", f: Findings | None = None) -> tuple[Any, Findings]:
        """Redact a JSON-like structure. Header-like keys are dropped outright."""
        f = f if f is not None else Findings()

        def walk(x, p):
            if isinstance(x, dict):
                out = {}
                for k, v in x.items():
                    if str(k).lower() in DROP_KEYS:
                        f.add("dropped-header", f"{p}.{k}")
                        continue
                    out[k] = walk(v, f"{p}.{k}")
                return out
            if isinstance(x, list):
                return [walk(v, f"{p}[{i}]") for i, v in enumerate(x)]
            if isinstance(x, str):
                return self.text(x, p, f)
            return x
        return walk(o, path), f


def scan(o: Any, known_secrets: Iterable[str] = ()) -> Findings:
    """Findings only (export-time validation): nothing should be found in a
    record that was already redacted."""
    _, f = Redactor(known_secrets).obj(o)
    return Findings([i for i in f.items if i["kind"] != "dropped-header"])
