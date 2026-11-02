"""A new main workstation from one enrollment (docs/plans/LINUX_WORKSTATION_PLAN.md W3), and
moving the work between workstations (W4: export / import).

install/workstation.sh trades the one-time code from Access → Machines for the enrollment answer
(three client keys, the game's clone settings, the router's Kilo provider block, an optional
Studio slot code) and hands it to `foundry_client workstation apply`, which:
  * stores the keys in the client's secret store (never printed);
  * writes client.toml (paths under the workstation root), the coordinator's project.toml,
    kilo.jsonc and the media adapter's config;
  * writes the game's repo key for the clone (an ssh deploy key, or an https token).

Layout under the root (default /srv/foundry):
    app/        the Foundry release (the deploy checkout; profiles in app/foundry_coordinator/projects)
    venv/       Python for the client and coordinator
    game/<id>/  the game checkout
    state/      the coordinator's state (client.state_dir)
    config/     foundry-media.json
"""
from __future__ import annotations

import json
import os
import re
import secrets as _random
import sys
import tomllib
from pathlib import Path
from typing import Optional

import tomli_w

DEFAULT_ROOT = "/srv/foundry"


def layout(root: str, project: str) -> dict:
    r = root.replace("\\", "/").rstrip("/") or DEFAULT_ROOT
    j = lambda *p: "/".join([r, *p])                            # noqa: E731 (POSIX paths)
    return {"root": r, "app": j("app"), "venv": j("venv"), "game": j("game", project),
            "state": j("state"), "config": j("config"),
            "profiles": j("app", "foundry_coordinator", "projects"),
            "python": j("venv", "bin", "python")}


# ----------------------------------------------------------------------------- pure builders

def client_settings(enr: dict, L: dict, secret_store: str = "auto") -> dict:
    """The client.toml values a new workstation gets (merged onto the defaults)."""
    project = enr["project"]
    return {
        "client": {"backend_url": enr.get("router_http") or enr.get("router") or "",
                   "state_dir": L["state"], "secret_store": secret_store,
                   "router_key": "FOUNDRY_CLIENT_KEY"},
        "workspace": {"game": L["game"], "profiles": L["profiles"], "deploy": L["app"],
                      "kilo_config": L["game"] + "/kilo.jsonc", "project": project},
        "components": {
            # Kilo passes FOUNDRY_KEY on to the media adapter it starts (stdio MCP)
            "kilo": {"secrets": ["KILO_SERVER_PASSWORD", "FOUNDRY_KEY_KILO", "FOUNDRY_KEY"]},
            "coordinator": {"secrets": ["KILO_SERVER_PASSWORD", "FOUNDRY_COORDINATOR_KEY"]},
        },
    }


def project_toml(example_text: str, enr: dict, L: dict) -> str:
    """The coordinator's local project.toml from the release's project.example.toml with this
    workstation's paths and addresses (comments are not kept; every value is)."""
    raw = tomllib.loads(example_text)
    raw.setdefault("workspace", {})["path"] = L["game"]
    game = enr.get("game") or {}
    if game.get("branch"):
        raw["workspace"]["branch"] = game["branch"]
    if game.get("remote"):
        raw["workspace"]["remote"] = game["remote"]
    cl = raw.setdefault("client", {})
    cl["url"] = "http://127.0.0.1:4096"
    ext = cl.setdefault("permissions", {}).setdefault("external_directory", {})
    for k in [k for k in ext if re.match(r"^[A-Za-z]:[/\\]", k)]:       # Windows source roots
        ext.pop(k)
    ext[L["root"]] = "allow"
    ext[L["root"] + "/*"] = "allow"
    if enr.get("router_http"):
        raw.setdefault("foundry", {})["url"] = enr["router_http"]
    return ("# written by `foundry_client workstation apply` (install/workstation.sh); the router's\n"
            "# settings (Automation → Settings) overlay it, so change values there\n"
            + tomli_w.dumps(raw))


def media_config(enr: dict, L: dict) -> dict:
    out = L["game"] + "/assets/generated/_unapproved"
    return {"foundry_url": enr.get("router_http") or "", "roots": [out], "default_dir": out,
            "wait_seconds": 50, "sidecar": True, "allow_read_outside": False}


def kilo_jsonc(enr: dict, L: dict) -> str:
    """A first kilo.jsonc: the router's provider block, the implementer as the model, and the
    media adapter. The coordinator adds the role permission blocks (apply-kilo-config) and,
    with studio.main_slot set, the Studio MCP servers on the slot."""
    block = dict(enr.get("kilo_provider") or {})
    model = f"foundry/{enr['implementer']}" if enr.get("implementer") else ""
    doc = {"$schema": "https://app.kilo.ai/config.json", **block}
    if model:
        doc.update({"model": model, "small_model": model})
    doc.update({"default_agent": "code", "snapshot": False, "permission": {},
                "mcp": {"foundry-media": {
                    "type": "local", "enabled": True,
                    "command": [L["python"], L["app"] + "/contrib/media-adapter/foundry_media_mcp.py",
                                "--config", L["config"] + "/foundry-media.json"]}}})
    return json.dumps(doc, indent=2) + "\n"


# ----------------------------------------------------------------------------- applying

def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def repo_access(enr: dict, home: str) -> dict:
    """Write the game's repo key where git finds it: {kind: none|ssh|https, path, git_env}.
    An ssh private key -> ~/.ssh/foundry-<id>-repo (GIT_SSH_COMMAND); anything else is an https
    token -> ~/.git-credentials for the clone URL's host (credential.helper store)."""
    game = enr.get("game") or {}
    key = game.get("repo_key")
    if not key:
        return {"kind": "none", "path": "", "problem": game.get("repo_key_problem") or ""}
    if "PRIVATE KEY-----" in key:
        path = os.path.join(home, ".ssh", f"foundry-{enr['project']}-repo")
        _write_private(path, key.strip() + "\n")
        return {"kind": "ssh", "path": path,
                "ssh_command": f"ssh -i {path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"}
    url = game.get("clone_url") or ""
    host = re.sub(r"^https?://([^/@]*@)?", "", url).split("/")[0]
    if not host:
        return {"kind": "none", "path": "", "problem": "a token needs an https clone URL"}
    path = os.path.join(home, ".git-credentials")
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln and not ln.endswith("@" + host)]
    except OSError:
        pass
    lines.append(f"https://x-access-token:{key.strip()}@{host}")
    _write_private(path, "\n".join(lines) + "\n")
    return {"kind": "https", "path": path}


def apply(enr: dict, root: str = DEFAULT_ROOT, secret_store: str = "auto",
          home: Optional[str] = None, cfg_path: Optional[Path] = None) -> dict:
    """Everything a new workstation needs from its enrollment answer. Returns a summary with no
    secret values (what install/workstation.sh prints)."""
    from . import config, secrets
    if enr.get("kind") != "workstation":
        raise ValueError("this enrollment is not for a workstation (made on Access → Machines → "
                         "Add a workstation)")
    L = layout(root, enr["project"])
    home = home or os.path.expanduser("~")
    cfg = config.load(cfg_path)
    secrets.configure({"client": {**cfg["client"], "secret_store": secret_store}})
    stored = []
    for name, value in sorted((enr.get("keys") or {}).items()):
        secrets.set_secret(name, value)
        stored.append(name)
    if not secrets.get("KILO_SERVER_PASSWORD"):
        secrets.set_secret("KILO_SERVER_PASSWORD", _random.token_urlsafe(24))
        stored.append("KILO_SERVER_PASSWORD (new, random)")
    want = client_settings(enr, L, secret_store)
    merged = config._merge(cfg, want)                                  # noqa: SLF001
    saved = config.save(merged, cfg_path)
    access = repo_access(enr, home)
    os.makedirs(L["config"], exist_ok=True)
    with open(L["config"] + "/foundry-media.json", "w", encoding="utf-8") as f:
        json.dump(media_config(enr, L), f, indent=2)
    return {"client_toml": str(saved), "secret_store": secrets.store_in_use(),
            "secrets_stored": stored, "layout": L, "repo": {k: v for k, v in access.items()},
            "studio_slot": (enr.get("studio") or {}).get("slot") or ""}


def write_profile(enr: dict, root: str = DEFAULT_ROOT) -> dict:
    """project.toml and kilo.jsonc, once the release and the game are cloned (an existing file
    is kept: a re-run never overwrites your edits)."""
    L = layout(root, enr["project"])
    pdir = os.path.join(L["profiles"], enr["project"])
    out = {"project_toml": os.path.join(pdir, "project.toml"),
           "kilo_jsonc": L["game"] + "/kilo.jsonc", "written": []}
    if not os.path.isfile(out["project_toml"]):
        with open(os.path.join(pdir, "project.example.toml"), encoding="utf-8") as f:
            text = project_toml(f.read(), enr, L)
        with open(out["project_toml"], "w", encoding="utf-8") as f:
            f.write(text)
        out["written"].append("project.toml")
    if not os.path.isfile(out["kilo_jsonc"]):
        with open(out["kilo_jsonc"], "w", encoding="utf-8") as f:
            f.write(kilo_jsonc(enr, L))
        out["written"].append("kilo.jsonc")
    return out


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="foundry-client workstation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("apply", help="store the keys and write client.toml (installer step)")
    a1.add_argument("--enrollment", required=True, help="the enrollment answer (JSON, mode 600)")
    a1.add_argument("--root", default=DEFAULT_ROOT)
    a1.add_argument("--secret-store", default="auto", choices=("auto", "keyring", "file"))
    a2 = sub.add_parser("profile", help="write project.toml and kilo.jsonc (after the clones)")
    a2.add_argument("--enrollment", required=True)
    a2.add_argument("--root", default=DEFAULT_ROOT)
    e = sub.add_parser("export", help="pack this workstation's work into one encrypted file")
    e.add_argument("--out", required=True)
    e.add_argument("--passphrase-stdin", action="store_true")
    e.add_argument("--keep-active", action="store_true",
                   help="do not hand the project over (default: this workstation steps down)")
    i = sub.add_parser("import", help="unpack an export onto this (new) workstation")
    i.add_argument("file")
    i.add_argument("--root", default=DEFAULT_ROOT)
    i.add_argument("--passphrase-stdin", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd in ("apply", "profile"):
        with open(a.enrollment, encoding="utf-8") as f:
            enr = json.load(f)
        res = apply(enr, a.root, a.secret_store) if a.cmd == "apply" else write_profile(enr, a.root)
        print(json.dumps(res, indent=2))
        return 0
    from . import move
    return move.main(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
