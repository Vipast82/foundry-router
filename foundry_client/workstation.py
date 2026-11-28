"""A new main workstation from one enrollment (docs/plans/LINUX_WORKSTATION_PLAN.md W3), and
moving the work between workstations (W4: export / import).

install/workstation.sh (install/workstation.ps1 on Windows) trades the one-time code from
Access → Machines for the enrollment answer (the client keys, the game's clone settings, the
router's Kilo provider block, the project's coding client, an optional Studio slot code) and
hands it to `foundry_client workstation apply`, which:
  * stores the keys in the client's secret store (never printed);
  * writes client.toml (paths under the workstation root), the coordinator's project.toml,
    kilo.jsonc (and opencode.jsonc for an OpenCode project) and the media adapter's config;
  * writes the game's repo key for the clone (an ssh deploy key, or an https token).

Run again it is idempotent: what is set is kept (client.toml values you changed, the Kilo and
OpenCode server passwords, project.toml, kilo.jsonc). Without a code the installer repairs or
upgrades with the enrolment it has: `workstation fetch` reads the answer again with this
machine's own coordinator key (no new keys). A second project's code ADDS that project: its
game and kilo.jsonc go into [workspace.projects.<id>] beside the first, which stays the default
(workspace.game / workspace.project), and the keys are kept (the router extends them to it).

Layout under the root (default /srv/foundry; C:/Foundry on Windows):
    app/        the Foundry release (the deploy checkout; profiles in app/foundry_coordinator/projects)
    venv/       Python for the client and coordinator (bin/python; Scripts/python.exe on Windows)
    game/<id>/  the game checkout (one per project)
    state/      the coordinator's state (client.state_dir)
    config/     foundry-media.json (the default project) and foundry-media-<id>.json
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
# the generic profile a project without its own project.example.toml starts from
TEMPLATE = "_template"
ENROLLED_KEY = "FOUNDRY_COORDINATOR_KEY"        # machines.py WS_KEYS: <name> (orchestration)
PASSWORDS = ("KILO_SERVER_PASSWORD", "OPENCODE_SERVER_PASSWORD")
# the components that hold keys in their environment (restarted after a re-enrolment)
KEY_COMPONENTS = ("coordinator", "supervisor", "kilo", "opencode")


def is_windows_root(root: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:([/\\]|$)", str(root or "")))


def layout(root: str, project: str, windows: Optional[bool] = None) -> dict:
    """The workstation's paths. Windows (a root like C:/Foundry, or windows=True): the venv's
    python is Scripts/python.exe, which is what kilo.jsonc's foundry-media command runs."""
    r = root.replace("\\", "/").rstrip("/") or DEFAULT_ROOT
    win = is_windows_root(r) if windows is None else bool(windows)
    j = lambda *p: "/".join([r, *p])                            # noqa: E731 (forward slashes)
    return {"root": r, "app": j("app"), "venv": j("venv"), "game": j("game", project),
            "state": j("state"), "config": j("config"),
            "media_config": j("config", f"foundry-media-{project}.json"),
            "profiles": j("app", "foundry_coordinator", "projects"),
            "python": j("venv", "Scripts", "python.exe") if win else j("venv", "bin", "python")}


def coding_clients(enr: dict) -> list[str]:
    """The coding clients the project uses (the router's answer; kilo when it does not say)."""
    kinds = {str(k) for k in (enr.get("coding_clients") or []) if k in ("kilo", "opencode")}
    if enr.get("client_kind") in ("kilo", "opencode"):
        kinds.add(enr["client_kind"])
    return sorted(kinds) or ["kilo"]


def uses_opencode(enr: dict) -> bool:
    return "opencode" in coding_clients(enr)


# ----------------------------------------------------------------------------- pure builders

def project_entry(L: dict) -> dict:
    return {"game": L["game"], "kilo_config": L["game"] + "/kilo.jsonc"}


def client_settings(enr: dict, L: dict, secret_store: str = "auto") -> dict:
    """The client.toml values a new workstation gets (merged onto the defaults)."""
    project = enr["project"]
    coord = ["KILO_SERVER_PASSWORD", "FOUNDRY_COORDINATOR_KEY"]
    comps = {
        # Kilo passes FOUNDRY_KEY on to the media adapter it starts (stdio MCP)
        "kilo": {"secrets": ["KILO_SERVER_PASSWORD", "FOUNDRY_KEY_KILO", "FOUNDRY_KEY"]},
        "coordinator": {"secrets": coord},
    }
    if uses_opencode(enr):
        # the project's coding client is OpenCode ([client] kind = "opencode"): its server
        # runs beside Kilo and the coordinator holds its password
        comps["opencode"] = {"enabled": True,
                             "secrets": ["OPENCODE_SERVER_PASSWORD", "FOUNDRY_KEY_OPENCODE"]}
        coord.append("OPENCODE_SERVER_PASSWORD")
    return {
        "client": {"backend_url": enr.get("router_http") or enr.get("router") or "",
                   "state_dir": L["state"], "secret_store": secret_store,
                   "router_key": "FOUNDRY_CLIENT_KEY"},
        "workspace": {"game": L["game"], "profiles": L["profiles"], "deploy": L["app"],
                      "kilo_config": L["game"] + "/kilo.jsonc", "project": project,
                      "projects": {project: project_entry(L)}},
        "components": comps,
    }


def _fill(cur: dict, want: dict, defaults: dict) -> dict:
    """`want` onto `cur` without undoing an edit: a value is set where `cur` has none, the
    default or an empty one; lists gain the entries they lack (order kept); a value someone
    changed stays."""
    out = dict(cur)
    for k, v in want.items():
        d = defaults.get(k) if isinstance(defaults, dict) else None
        c = out.get(k)
        if isinstance(v, dict):
            out[k] = _fill(c if isinstance(c, dict) else {}, v, d if isinstance(d, dict) else {})
        elif isinstance(v, list) and isinstance(c, list):
            out[k] = c + [x for x in v if x not in c]
        elif c is None or c == "" or c == d or c == []:
            out[k] = v
    return out


def merged_settings(cfg: dict, want: dict, fresh: bool) -> dict:
    """client.toml after an install run. A fresh file takes every installer value; a re-run
    keeps what is there (_fill). Another project than the configured one is ADDED under
    workspace.projects; the first stays the default (workspace.game / workspace.project)."""
    from . import config
    ws_old = dict(cfg.get("workspace") or {})
    ws_new = want["workspace"]
    first = str(ws_old.get("project") or "")
    projects = {k: dict(v) for k, v in (ws_old.get("projects") or {}).items()
                if isinstance(v, dict)}
    if first and ws_old.get("game") and first not in projects:
        projects[first] = {"game": ws_old["game"],
                           "kilo_config": ws_old.get("kilo_config") or ws_old["game"] + "/kilo.jsonc"}
    for k, v in ws_new["projects"].items():
        projects[k] = {**v, **projects.get(k, {})} if not fresh else v
    if first and first != ws_new["project"]:
        want = {**want, "workspace": {"projects": projects}}
    else:
        want = {**want, "workspace": {**ws_new, "projects": projects}}
    if fresh:
        return config._merge(cfg, want)                               # noqa: SLF001
    out = _fill(cfg, want, config.DEFAULTS)
    out["workspace"]["projects"] = projects
    return out


def project_toml(example_text: str, enr: dict, L: dict) -> str:
    """The coordinator's local project.toml from the release's project.example.toml (or the
    generic template's) with this workstation's paths and addresses (comments are not kept;
    every value is)."""
    raw = tomllib.loads(example_text)
    project = enr["project"]
    if raw.get("id") != project:           # the generic template: this project's own id
        raw["id"] = project
        raw["name"] = project
    raw.setdefault("workspace", {})["path"] = L["game"]
    game = enr.get("game") or {}
    if game.get("branch"):
        raw["workspace"]["branch"] = game["branch"]
    if game.get("remote"):
        raw["workspace"]["remote"] = game["remote"]
    cl = raw.setdefault("client", {})
    kind = enr.get("client_kind") if enr.get("client_kind") in ("kilo", "opencode") \
        else cl.get("kind", "kilo")
    cl["kind"] = kind
    if kind == "opencode":
        cl["url"] = "http://127.0.0.1:4097"
        cl["password_env"] = "OPENCODE_SERVER_PASSWORD"
    else:
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
    media adapter (its config is this project's own file). The coordinator adds the role
    permission blocks (apply-kilo-config) and, with studio.main_slot set, the Studio MCP
    servers on the slot."""
    block = dict(enr.get("kilo_provider") or {})
    model = f"foundry/{enr['implementer']}" if enr.get("implementer") else ""
    doc = {"$schema": "https://app.kilo.ai/config.json", **block}
    if model:
        doc.update({"model": model, "small_model": model})
    doc.update({"default_agent": "code", "snapshot": False, "permission": {},
                "mcp": {"foundry-media": {
                    "type": "local", "enabled": True,
                    "command": [L["python"], L["app"] + "/contrib/media-adapter/foundry_media_mcp.py",
                                "--config", L.get("media_config")
                                or L["config"] + "/foundry-media.json"]}}})
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
    token -> ~/.git-credentials for the clone URL's host (credential.helper store). No key in
    the answer (a repair run) and an ssh key written before: that one is used."""
    game = enr.get("game") or {}
    key = game.get("repo_key")
    ssh_path = os.path.join(home, ".ssh", f"foundry-{enr['project']}-repo")
    ssh = {"kind": "ssh", "path": ssh_path,
           "ssh_command": f"ssh -i {ssh_path} -o IdentitiesOnly=yes "
                          "-o StrictHostKeyChecking=accept-new"}
    if not key:
        if os.path.isfile(ssh_path):
            return ssh
        return {"kind": "none", "path": "", "problem": game.get("repo_key_problem") or ""}
    if "PRIVATE KEY-----" in key:
        _write_private(ssh_path, key.strip() + "\n")
        return ssh
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


class NotEnrolled(ValueError):
    pass


def apply(enr: dict, root: str = DEFAULT_ROOT, secret_store: str = "auto",
          home: Optional[str] = None, cfg_path: Optional[Path] = None) -> dict:
    """Everything a workstation needs from its enrollment (or repair) answer. Returns a
    summary with no secret values (what install/workstation.sh prints); `keys_changed` says
    whether new keys were stored, so the installer restarts a running client."""
    from . import config, secrets
    if enr.get("kind") != "workstation":
        raise ValueError("this enrollment is not for a workstation (made on Access → Machines → "
                         "Add a workstation)")
    L = layout(root, enr["project"])
    home = home or os.path.expanduser("~")
    p = Path(cfg_path or config.config_path())
    fresh = not p.is_file()
    cfg = config.load(cfg_path)
    if not fresh and secret_store == "auto":     # a re-run keeps the store it has
        secret_store = str(cfg["client"].get("secret_store") or "auto")
    secrets.configure({"client": {**cfg["client"], "secret_store": secret_store}})
    keys = dict(enr.get("keys") or {})
    if not keys and not secrets.get(ENROLLED_KEY):
        raise NotEnrolled("this answer has no keys and this machine has none: a code that adds a "
                          "project works on the workstation already enrolled; here, use a code "
                          "from Add a workstation (or New code to re-enrol)")
    stored = []
    for name, value in sorted(keys.items()):
        secrets.set_secret(name, value)
        stored.append(name)
    for pw in PASSWORDS:                # kept on every re-run: Kilo / OpenCode keep working
        if pw == "OPENCODE_SERVER_PASSWORD" and not uses_opencode(enr):
            continue
        if not secrets.get(pw):
            secrets.set_secret(pw, _random.token_urlsafe(24))
            stored.append(f"{pw} (new, random)")
    want = client_settings(enr, L, secret_store)
    merged = merged_settings(cfg, want, fresh)
    saved = config.save(merged, cfg_path)
    access = repo_access(enr, home)
    os.makedirs(L["config"], exist_ok=True)
    default = merged["workspace"].get("project") or enr["project"]
    media = [L["media_config"]] + ([L["config"] + "/foundry-media.json"]
                                   if default == enr["project"] else [])
    for path in media:                  # foundry-media.json: what older kilo.jsonc files name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(media_config(enr, L), f, indent=2)
    return {"client_toml": str(saved), "secret_store": secrets.store_in_use(),
            "secrets_stored": stored, "keys_changed": bool(keys),
            "keys_kept": list(enr.get("keys_kept") or []), "layout": L,
            "project": enr["project"], "default_project": default,
            "projects": sorted((merged["workspace"].get("projects") or {}).keys()),
            "coding_clients": coding_clients(enr),
            "repo": {k: v for k, v in access.items()},
            "studio_slot": (enr.get("studio") or {}).get("slot") or ""}


def example_for(profiles: str, project: str) -> str:
    """The project's own project.example.toml, else the generic template's."""
    own = os.path.join(profiles, project, "project.example.toml")
    return own if os.path.isfile(own) else os.path.join(profiles, TEMPLATE, "project.example.toml")


def write_profile(enr: dict, root: str = DEFAULT_ROOT) -> dict:
    """project.toml, kilo.jsonc and (an OpenCode project) opencode.jsonc, once the release and
    the game are cloned. An existing file is kept: a re-run never overwrites your edits. A
    project without its own profile folder starts from projects/_template."""
    L = layout(root, enr["project"])
    pdir = os.path.join(L["profiles"], enr["project"])
    out = {"project_toml": os.path.join(pdir, "project.toml"),
           "kilo_jsonc": L["game"] + "/kilo.jsonc", "written": [], "problems": []}
    if not os.path.isfile(out["project_toml"]):
        src = example_for(L["profiles"], enr["project"])
        with open(src, encoding="utf-8") as f:
            text = project_toml(f.read(), enr, L)
        os.makedirs(pdir, exist_ok=True)
        with open(out["project_toml"], "w", encoding="utf-8") as f:
            f.write(text)
        out["written"].append("project.toml" + (" (from the generic template)"
                                                if TEMPLATE in src else ""))
    if not os.path.isfile(out["kilo_jsonc"]):
        with open(out["kilo_jsonc"], "w", encoding="utf-8") as f:
            f.write(kilo_jsonc(enr, L))
        out["written"].append("kilo.jsonc")
    if uses_opencode(enr):
        oc = opencode_jsonc(out["project_toml"], out["kilo_jsonc"])
        out["problems"] += oc.pop("problems", [])
        out.update(oc)
        if oc.get("opencode_written"):
            out["written"].append("opencode.jsonc")
    return out


def opencode_jsonc(project_toml_path: str, kilo_jsonc_path: str) -> dict:
    """The coordinator's own `agent-config --apply` (agent_config.write): the first
    opencode.jsonc of an OpenCode project, its provider block from kilo.jsonc. Never
    overwrites; a problem is reported, not raised (the installer warns)."""
    try:
        from foundry_coordinator import profile as P
        from foundry_coordinator.agent_config import AgentConfigError, target_path, write
    except ImportError as e:                       # the coordinator is not importable here
        return {"opencode_written": False, "problems": [f"opencode.jsonc: {e}"]}
    try:
        prof = P.load(project_toml_path)
        if prof.client.get("kind") != "opencode":
            return {"opencode_written": False, "problems": []}
        if os.path.isfile(target_path(prof)):
            return {"opencode_jsonc": target_path(prof), "opencode_written": False,
                    "problems": []}
        write(prof, provider_from=kilo_jsonc_path, apply=True)
        return {"opencode_jsonc": target_path(prof), "opencode_written": True, "problems": []}
    except (AgentConfigError, P.ProfileError, OSError, ValueError) as e:
        return {"opencode_written": False, "problems": [f"opencode.jsonc: {e}"]}


def merge_settings(want: dict, cfg_path: Optional[Path] = None) -> dict:
    """client.toml with an installer's values: all of them on a new file; on an existing one
    only where nothing (or the default) is set, so an edit survives a re-run (install/slot.ps1;
    the same rule as apply)."""
    from . import config
    p = Path(cfg_path or config.config_path())
    fresh = not p.is_file()
    cfg = config.load(cfg_path)
    merged = config._merge(cfg, want) if fresh else _fill(cfg, want, config.DEFAULTS)  # noqa: SLF001
    return {"client_toml": str(config.save(merged, cfg_path)), "fresh": fresh}


# ----------------------------------------------------------------------------- repair / upgrade

def enrolled(cfg_path: Optional[Path] = None) -> dict:
    """{enrolled, project, projects, backend_url}: does this machine hold a workstation
    enrolment (its client.toml and its coordinator key)? Reads no value out."""
    from . import config, secrets
    p = Path(cfg_path or config.config_path())
    if not p.is_file():
        return {"enrolled": False, "project": "", "projects": [], "backend_url": ""}
    cfg = config.load(cfg_path)
    secrets.configure(cfg)
    ws = cfg.get("workspace") or {}
    return {"enrolled": bool(secrets.get(ENROLLED_KEY)), "project": ws.get("project") or "",
            "projects": sorted((ws.get("projects") or {}).keys())
            or ([ws["project"]] if ws.get("project") else []),
            "backend_url": cfg["client"].get("backend_url") or ""}


def _connect(router: str, fingerprint: str = "", timeout: float = 30):
    """An http(s) connection to the router; https is pinned to the certificate fingerprint
    (a self-signed router certificate), as the installers' own enrolment does."""
    import hashlib
    import http.client
    import ssl
    import urllib.parse
    u = urllib.parse.urlsplit(router)
    if u.scheme != "https":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                 # pinned below instead
    c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    c.connect()
    got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
    if not fingerprint or fingerprint.replace(":", "").upper() != got:
        c.close()
        raise OSError("the router's certificate does not match --fingerprint "
                      "(Access → Secrets → Transport shows it)")
    return c


def fetch(out: str, router: str = "", fingerprint: str = "", project: str = "",
          repo_key: bool = False, cfg_path: Optional[Path] = None) -> dict:
    """The enrollment answer again, without keys, read with this machine's coordinator key
    (GET /v1/foundry/control/workstation-enrollment), written to `out` (mode 600). Raises
    NotEnrolled when there is no enrolment here; OSError / RuntimeError when the router cannot
    be reached or refuses."""
    import urllib.parse
    from . import secrets
    st = enrolled(cfg_path)
    key = secrets.get(ENROLLED_KEY) if st["enrolled"] else None
    if not key:
        raise NotEnrolled("this machine is not enrolled yet: run the installer with a code")
    router = (router or st["backend_url"]).rstrip("/")
    project = project or st["project"]
    q = urllib.parse.urlencode([(k, v) for k, v in (("project", project),
                                                    ("repo_key", "true" if repo_key else ""))
                                if v])
    c = _connect(router, fingerprint)
    try:
        c.request("GET", "/v1/foundry/control/workstation-enrollment" + (f"?{q}" if q else ""),
                  headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                           "User-Agent": "foundry-workstation-installer/1"})
        r = c.getresponse()
        raw = r.read()
    finally:
        c.close()
        key = None
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    if r.status != 200:
        e = data.get("error") or {}
        raise RuntimeError("the router refused: " + ((e.get("message") if isinstance(e, dict)
                                                       else str(e)) or f"HTTP {r.status}"))
    u = urllib.parse.urlsplit(router)
    data["router"] = router
    if not data.get("router_http") and u.scheme == "http":
        data["router_http"] = router
    data["keys"] = {}
    _write_private(out, json.dumps(data))
    return {"machine": data.get("machine"), "project": data.get("project"),
            "router_version": data.get("router_version"), "projects": data.get("projects") or [],
            "coding_clients": coding_clients(data)}


def _client_api(path: str, method: str = "GET", body=None, timeout: float = 30):
    from . import config
    from .__main__ import _api
    return _api(config.load(), path, method, body, timeout=timeout)


def update(tag: str) -> dict:
    """Move the deploy checkout to `tag` the client's own way (selfupdate.py: at a gap, the
    coordinator and then the client restarted, rolled back on a failure) instead of a checkout
    under a running coordinator."""
    import urllib.error
    try:
        return _client_api("/api/update/apply", "POST", {"tag": tag}, timeout=60)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") or {"error": f"HTTP {e.code}"}
        except ValueError:
            return {"error": f"HTTP {e.code}"}


def restart_with_new_keys() -> dict:
    """After a (re-)enrolment: every running component that holds keys starts again now (it
    reads its secrets at start, so no revoked key is kept), then the client itself."""
    st = _client_api("/api/status", timeout=10)
    done = []
    for c in st.get("components") or []:
        if c.get("name") in KEY_COMPONENTS and c.get("state") in ("running", "starting",
                                                                  "unhealthy"):
            _client_api(f"/api/components/{c['name']}/restart?force=true", "POST", {},
                        timeout=120)
            done.append(c["name"])
    try:
        _client_api("/api/client/restart", "POST", {}, timeout=30)
        done.append("client")
    except OSError:
        pass
    return {"restarted": done}


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
    a3 = sub.add_parser("fetch", help="read the enrollment answer again with this machine's key "
                                      "(repair / upgrade without a code); --check: only say "
                                      "whether this machine is enrolled")
    a3.add_argument("--out", default="")
    a3.add_argument("--router", default="")
    a3.add_argument("--fingerprint", default="")
    a3.add_argument("--project", default="")
    a3.add_argument("--repo-key", action="store_true")
    a3.add_argument("--check", action="store_true")
    a4 = sub.add_parser("update", help="move the deploy checkout to a release the client's own "
                                       "way (Update now)")
    a4.add_argument("--tag", required=True)
    sub.add_parser("restart", help="restart the running client and its key-holding components "
                                   "(after a re-enrolment)")
    a5 = sub.add_parser("settings", help="merge an installer's client.toml values (a JSON file, "
                                         "no secrets) without undoing edits")
    a5.add_argument("--settings", required=True)
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
        try:
            res = apply(enr, a.root, a.secret_store) if a.cmd == "apply" \
                else write_profile(enr, a.root)
        except NotEnrolled as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 3
        print(json.dumps(res, indent=2))
        return 0
    if a.cmd == "fetch":
        if a.check:                      # no value: enrolled?, its projects, the router address
            st = enrolled()
            print(json.dumps(st))
            return 0 if st["enrolled"] else 3
        if not a.out:
            print("error: --out FILE", file=sys.stderr)
            return 2
        try:
            print(json.dumps(fetch(a.out, a.router, a.fingerprint, a.project, a.repo_key)))
        except NotEnrolled as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 3
        except (OSError, RuntimeError) as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 4
        return 0
    if a.cmd == "settings":
        with open(a.settings, encoding="utf-8-sig") as f:      # PowerShell may write a BOM
            want = json.load(f)
        print(json.dumps(merge_settings(want)))
        return 0
    if a.cmd in ("update", "restart"):
        try:
            res = update(a.tag) if a.cmd == "update" else restart_with_new_keys()
        except OSError:
            print(json.dumps({"error": "the Foundry Client is not running"}))
            return 3
        print(json.dumps(res))
        return 1 if res.get("error") else 0
    from . import move
    return move.main(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
