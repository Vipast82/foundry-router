#!/bin/bash
# Foundry main workstation installer (docs/plans/LINUX_WORKSTATION_PLAN.md W3): one run turns a
# Debian 13 / Ubuntu 24.04 machine (a Linux PC, or a Proxmox LXC made by
# create-workstation-lxc.sh) into a Foundry main workstation: the coordinator, the Kilo server
# (and OpenCode's, for a project that uses it), the game checkout, Blender for asset sprints,
# and optionally a Studio for main sprints in the Studio slot container on this same machine.
#
# Before you run it: Foundry router → Access → Machines → "Add a workstation". That gives you a
# one-time ENROLLMENT CODE (FNDR-XXXX-XXXX-XXXX).
#
# As root on the new workstation:
#   apt-get update -qq && apt-get install -y -qq curl
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/install/workstation.sh
#   bash workstation.sh --router http://<router>:11435
#
# It asks for the router address (unless --router) and the code, then:
#   1. installs the packages (git, Python, Node 22, Kilo, OpenCode, rokit, Blender 4.5 LTS;
#      Docker for a Studio here) and creates the `foundry` user;
#   2. trades the code for this workstation's keys, stored in the client's secret store
#      (never shown), and the game's clone settings;
#   3. fetches Foundry at the router's version into <root>/app, clones the game into
#      <root>/game/<id>, and writes client.toml, project.toml and kilo.jsonc (opencode.jsonc
#      too when the project's coding client is OpenCode);
#   4. with --import FILE, brings over the work exported from the old workstation (W4);
#   5. with a Studio here, runs the Studio slot installer for it (20-40 min the first time);
#   6. installs the Foundry Client as a systemd service (starts at boot, no sign-in needed),
#      starts it, applies the Kilo rules and runs `doctor`.
# A new workstation starts on STANDBY when another one already runs the project: it starts no
# sprints until you press Make active on Access → Machines.
#
# Options:
#   --router URL          the router (default: asks; an enrolled machine: its client.toml's)
#   --code-stdin          read the enrollment code from stdin (non-interactive; an empty line
#                         on an enrolled machine = repair)
#   --fingerprint SHA256  the router's HTTPS certificate (an https router)
#   --root DIR            where everything lives (default /srv/foundry)
#   --repair              no code: repair / upgrade with this machine's enrolment
#   --reenrol             ask for a code even on an enrolled machine (fresh keys)
#   --project ID          with --repair: which of this machine's projects (default: its first)
#   --import FILE         an export from the old workstation (foundry-client workstation export)
#   --import-only FILE    on an installed workstation: stop the client, import, start it again
#                         (no enrollment code needed)
#   --secret-store MODE   file (default: a mode-600 file, D-L5) | auto | keyring
#   --no-studio           skip the Studio here even if the router added one
#   --skip-tools          skip Node/Kilo/rokit/Blender (testing)
#   --no-start            stop before starting the service (testing)
# Re-running is safe: what exists is kept (client.toml edits, the Kilo / OpenCode passwords,
# project.toml, kilo.jsonc). An enrolled machine needs NO code: press Enter at the code
# question to repair / upgrade with its keys. A code from "Add a workstation" with another
# project ADDS that project (its keys stay); New code re-enrolls (fresh keys, the running
# client is restarted so it keeps none of the old ones).
set -euo pipefail

REPO="${FOUNDRY_REPO:-https://github.com/Vipast82/foundry-router.git}"   # a fork or a test copy
ROOT=/srv/foundry ; ROUTER="" ; CODE="" ; FINGERPRINT="" ; CODE_STDIN=0 ; IMPORT=""
STORE=file ; STUDIO=1 ; TOOLS=1 ; START=1 ; IMPORT_ONLY=0 ; MODE=auto ; WANT_PROJECT=""
USER_NAME=foundry
# The pins: install/pins.env is the one list (tests/test_junction_install.py keeps these
# copies equal to it); a pins.env next to this script is read over them.
NODE_VERSION=22.21.0
# the coding clients at the slot image's pins (contrib/studio-slot/Dockerfile): both in every
# Foundry machine, the project picks one ([client] kind; Distributed Foundry phase e)
KILO_VERSION=7.8.32
KILO_INTEGRITY=sha512-lbW+z4wMioOFejeJy0eSEdaYj9BdOt/QUbQMVLFmuaqJSluBSxlmJPSEaHCKrwU/7NVEH1wnA/K1o1Ol9nWz2A==
OPENCODE_VERSION=2.3.2
OPENCODE_INTEGRITY=sha512-U/DD6X8kgnEFM3VYT7+QN/UJotW++qOxTJtPEUiNzKwu9tNHtAC1jOtup1gpk773j0AFNmMBbLNou4YkdvlRKA==
PWSH_VERSION=7.6.9                     # the game's gate host half (tools/gate.ps1), as in the slot image
PWSH_SHA256=1da6d6d6035d651f20e70ddee7c4c15ac6ab8b9fb40ee92240d91263f28a884b
TOOLS_ONLY=0
ROKIT_VERSION=1.2.0
ROKIT_SHA256=951a7f3ec3d2a5e021fd1867d32f69f010ee4c2927f644b759578afc59c65fc0
BLENDER_VERSION=4.5.12                 # the same build as the slots (contrib/studio-slot/Dockerfile)
BLENDER_SHA256=95e3a2dfedba3bd32ca54fc355eac6b15a11986954ccb02815a07535d0120a25
PINS="$(dirname "$0")/pins.env"
# shellcheck source=pins.env
if [ -f "$PINS" ]; then . "$PINS"; fi
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --router) ROUTER="$2"; shift 2 ;;
        --code-stdin) CODE_STDIN=1; shift ;;
        --fingerprint) FINGERPRINT="$2"; shift 2 ;;
        --root) ROOT="${2%/}"; shift 2 ;;
        --repair) MODE=repair; shift ;;
        --reenrol|--reenroll) MODE=enrol; shift ;;
        --project) WANT_PROJECT="$2"; shift 2 ;;
        --import) IMPORT="$2"; shift 2 ;;
        --import-only) IMPORT="$2"; IMPORT_ONLY=1; shift 2 ;;
        --secret-store) STORE="$2"; shift 2 ;;
        --no-studio) STUDIO=0; shift ;;
        --skip-tools) TOOLS=0; shift ;;
        --tools-only) TOOLS_ONLY=1; shift ;;   # install the packages and tools, then stop (image builds)
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,52p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done
case "$STORE" in file|auto|keyring) ;; *) echo "--secret-store: file, auto or keyring" >&2; exit 2 ;; esac

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
TTY=/dev/tty
HAVE_TTY=0; if (exec < "$TTY") 2>/dev/null; then HAVE_TTY=1; fi
ask() {   # ask VAR "question" default
    local var="$1" q="$2" def="$3" ans=""
    if [ "$HAVE_TTY" = 0 ]; then printf -v "$var" '%s' "$def"; return; fi
    read -r -p "$q${def:+ [$def]}: " ans < "$TTY" || true
    printf -v "$var" '%s' "${ans:-$def}"
}
[ "$(id -u)" = 0 ] || die "run this as root (sudo bash workstation.sh)"
[ -z "$IMPORT" ] || [ -f "$IMPORT" ] || die "--import: no file $IMPORT"
[ -z "$IMPORT" ] || IMPORT="$(cd "$(dirname "$IMPORT")" && pwd)/$(basename "$IMPORT")"
FPY="$ROOT/venv/bin/python"
as_user() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" "$@"; }
uctl() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$UID_N" \
         DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" "$@"; }
client_active() { uctl systemctl --user is-active --quiet foundry-client.service 2>/dev/null; }
npm_pinned() {   # npm_pinned <package> <version> <integrity>: the registry's integrity first
    local got
    got=$(npm view "$1@$2" dist.integrity 2>/dev/null || true)
    [ "$got" = "$3" ] || die "npm's $1@$2 does not have the pinned integrity (install/pins.env)"
    npm install -g --silent "$1@$2" >/dev/null 2>&1 || die "npm could not install $1@$2"
}

if [ "$IMPORT_ONLY" = 1 ]; then
    id "$USER_NAME" >/dev/null 2>&1 && [ -x "$ROOT/venv/bin/python" ] \
        || die "--import-only needs an installed workstation (run this installer first)"
    HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6); UID_N=$(id -u "$USER_NAME")
    WORK=$(mktemp -d /tmp/foundry-ws.XXXXXX); chown "$USER_NAME" "$WORK"; chmod 700 "$WORK"
    trap 'rm -rf "$WORK"' EXIT
    cp "$IMPORT" "$WORK/import.fwx"; chown "$USER_NAME" "$WORK/import.fwx"
    if [ "$HAVE_TTY" = 1 ]; then read -r -s -p "The export's passphrase (typing is hidden): " PASS < "$TTY"; echo
    else read -r PASS || true; fi
    bold "stopping the Foundry Client"
    uctl systemctl --user stop foundry-client.service 2>/dev/null || true
    bold "importing"
    printf '%s\n' "$PASS" | runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" "$ROOT/venv/bin/python" \
        -m foundry_client workstation import "$WORK/import.fwx" --root "$ROOT" --passphrase-stdin \
        || { uctl systemctl --user start foundry-client.service || true; die "the import failed (the client was started again as it was)"; }
    PASS=""
    bold "starting the Foundry Client"
    uctl systemctl --user start foundry-client.service && ok "started" || warn "start it: systemctl --user start foundry-client (as $USER_NAME)"
    echo; echo "Done. On Access → Machines → Workstations, press Make active for this workstation"
    echo "if the old one did not hand the project over (export --keep-active)."
    exit 0
fi

echo
echo "Foundry workstation installer"
echo "-----------------------------"
REPAIR=0 ; ENROLLED_TXT=""
if [ "$TOOLS_ONLY" = 0 ]; then          # --tools-only (an image build) needs no router or code
# an enrolment already here (client.toml and its coordinator key): repair / upgrade needs no code
if id "$USER_NAME" >/dev/null 2>&1 && [ -x "$FPY" ] && [ "$MODE" != enrol ]; then
    HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
    if ENROLLED_TXT=$(cd "$ROOT" && as_user "$FPY" -m foundry_client workstation fetch --check 2>/dev/null); then
        [ -n "$ROUTER" ] || ROUTER=$(echo "$ENROLLED_TXT" | "$FPY" -c 'import json,sys; print(json.load(sys.stdin).get("backend_url",""))')
    else
        ENROLLED_TXT=""
    fi
fi
[ "$MODE" != repair ] || [ -n "$ENROLLED_TXT" ] || die "--repair: this machine is not enrolled yet (run it with a code)"
if [ -z "$ROUTER" ]; then ask ROUTER "Foundry router address (e.g. http://192.168.0.10:11435)" ""; fi
[ -n "$ROUTER" ] || die "no router address (--router http://<router>:11435)"
ROUTER="${ROUTER%/}"
case "$ROUTER" in http://*|https://*) ;; *) ROUTER="http://$ROUTER" ;; esac
# this installer must match the router's release (as setup-host.sh): fetch the router's own copy
# and run that one, before the code is asked for (a code works once)
if [ -z "${FOUNDRY_SETUP_MATCHED:-}" ] && [ -z "${FOUNDRY_REPO:-}" ] && command -v curl >/dev/null 2>&1; then
    RV=$(curl -fsS -m 10 -k "$ROUTER/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' || true)
    NEWDIR=$(mktemp -d /tmp/workstation.XXXXXX); NEW="$NEWDIR/workstation.sh"   # private (mode 700)
    if [ -n "$RV" ] && curl -fsSLo "$NEW" \
            "https://raw.githubusercontent.com/Vipast82/foundry-router/v$RV/install/workstation.sh" \
            && ! cmp -s "$NEW" "$0"; then
        echo "   this installer is not the router's (v$RV): running v$RV's installer"
        # its pins come with it (an older release has none: its own lines hold them)
        curl -fsSLo "$NEWDIR/pins.env" \
            "https://raw.githubusercontent.com/Vipast82/foundry-router/v$RV/install/pins.env" 2>/dev/null \
            || rm -f "$NEWDIR/pins.env"
        FOUNDRY_SETUP_MATCHED=1 exec bash "$NEW" "${ORIG_ARGS[@]}" --router "$ROUTER"
    fi
    rm -rf "$NEWDIR"
fi
if [ "$MODE" = repair ]; then
    REPAIR=1
elif [ "$CODE_STDIN" = 1 ]; then
    read -r CODE || true                   # a last line without a newline still counts
elif [ "$HAVE_TTY" = 1 ]; then
    if [ -n "$ENROLLED_TXT" ]; then
        echo "   this machine is enrolled ($(echo "$ENROLLED_TXT" | "$FPY" -c 'import json,sys; print(", ".join(json.load(sys.stdin)["projects"]) or "?")'))"
        read -r -s -p "Enrollment code (another project, or New code to re-enrol), or Enter to repair / upgrade: " CODE < "$TTY"; echo
    else
        read -r -s -p "Enrollment code from Access → Machines (typing is hidden): " CODE < "$TTY"; echo
    fi
fi
if [ "$REPAIR" = 0 ] && [ -z "$CODE" ]; then
    [ -n "$ENROLLED_TXT" ] || die "no enrollment code"
    REPAIR=1
fi
fi

bold "1/6 packages and the $USER_NAME user"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssh-client python3 python3-venv python3-pip \
    xz-utils unzip procps >/dev/null
if ! id "$USER_NAME" >/dev/null 2>&1; then
    # uid 1000 stays free for the Studio slot image's `studio` user
    useradd -m -s /bin/bash -K UID_MIN=1001 "$USER_NAME"
fi
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
UID_N=$(id -u "$USER_NAME")
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$ROOT" "$ROOT/game" "$ROOT/state" "$ROOT/config"
ok "user $USER_NAME (uid $UID_N); everything under $ROOT"
if [ "$TOOLS" = 1 ]; then
    if ! node --version 2>/dev/null | grep -q "^v$NODE_VERSION\$"; then
        cd /tmp
        curl -fsSLO "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz"
        curl -fsSLo node.sums "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt"
        grep " node-v$NODE_VERSION-linux-x64.tar.xz\$" node.sums | sha256sum -c - >/dev/null \
            || die "the Node download does not match nodejs.org's checksum"
        tar -xJf "node-v$NODE_VERSION-linux-x64.tar.xz" -C /usr/local --strip-components=1
        rm -f "node-v$NODE_VERSION-linux-x64.tar.xz" node.sums
    fi
    ok "Node $(node --version)"
    if ! kilo --version 2>/dev/null | grep -q "$KILO_VERSION"; then
        npm_pinned "@kilocode/cli" "$KILO_VERSION" "$KILO_INTEGRITY"
    fi
    ok "Kilo $(kilo --version 2>/dev/null | head -1)"
    if ! opencode --version 2>/dev/null | grep -q "$OPENCODE_VERSION"; then
        npm_pinned "@opencode/cli" "$OPENCODE_VERSION" "$OPENCODE_INTEGRITY"
    fi
    ok "OpenCode $(opencode --version 2>/dev/null | head -1)"
    if ! pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>/dev/null | grep -q "$PWSH_VERSION"; then
        cd /tmp
        curl -fsSLo pwsh.tgz "https://github.com/PowerShell/PowerShell/releases/download/v$PWSH_VERSION/powershell-$PWSH_VERSION-linux-x64.tar.gz"
        echo "$PWSH_SHA256  pwsh.tgz" | sha256sum -c - >/dev/null || die "PowerShell's checksum does not match"
        mkdir -p /opt/microsoft/powershell/7 && tar -xzf pwsh.tgz -C /opt/microsoft/powershell/7
        chmod 755 /opt/microsoft/powershell/7/pwsh && ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh
        rm -f pwsh.tgz
    fi
    ok "PowerShell $(pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>/dev/null)"
    if ! rokit --version 2>/dev/null | grep -q "$ROKIT_VERSION"; then
        cd /tmp
        curl -fsSLo rokit.zip "https://github.com/rojo-rbx/rokit/releases/download/v$ROKIT_VERSION/rokit-$ROKIT_VERSION-linux-x86_64.zip"
        echo "$ROKIT_SHA256  rokit.zip" | sha256sum -c - >/dev/null || die "rokit's checksum does not match"
        unzip -o -q rokit.zip -d /usr/local/bin && chmod 755 /usr/local/bin/rokit && rm -f rokit.zip
    fi
    ok "rokit $(rokit --version 2>/dev/null | awk '{print $NF}')"
    if [ ! -x /opt/blender/blender ] || ! /opt/blender/blender -b --version 2>/dev/null | grep -q "Blender $BLENDER_VERSION"; then
        apt-get install -y -qq libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libxkbcommon0 \
            libsm6 libice6 libegl1 >/dev/null
        cd /tmp
        curl -fsSLo blender.tar.xz "https://download.blender.org/release/Blender${BLENDER_VERSION%.*}/blender-$BLENDER_VERSION-linux-x64.tar.xz"
        echo "$BLENDER_SHA256  blender.tar.xz" | sha256sum -c - >/dev/null || die "Blender's checksum does not match"
        rm -rf /opt/blender && mkdir -p /opt/blender && tar -xJf blender.tar.xz -C /opt/blender --strip-components=1
        ln -sf /opt/blender/blender /usr/local/bin/blender && rm -f blender.tar.xz
    fi
    ok "$(blender -b --version 2>/dev/null | head -1 || echo 'Blender: not runnable here')"
else
    warn "--skip-tools: Node, Kilo, OpenCode, PowerShell, rokit and Blender not installed"
fi
if [ "$TOOLS_ONLY" = 1 ]; then
    ok "--tools-only: packages and tools are in; enrol later by running this again without it"
    exit 0
fi

WORK=$(mktemp -d /tmp/foundry-ws.XXXXXX); chown "$USER_NAME" "$WORK"; chmod 700 "$WORK"
ENROLL="$WORK/enroll.json"
# the enrollment answer holds the keys until they are stored: never left behind
trap 'rm -rf "$WORK"' EXIT
API="$WORK/api.py"
cat > "$API" <<'PY'
import hashlib, http.client, json, os, socket, ssl, sys, urllib.parse
cmd = sys.argv[1]
if cmd == "enroll":
    router, out, fp = os.environ["ROUTER"], os.environ["OUT"], os.environ.get("FINGERPRINT", "")
    u = urllib.parse.urlsplit(router)
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30, context=ctx)
            c.connect()
            got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
            got = ":".join(got[i:i + 2] for i in range(0, len(got), 2))
            if fp and fp.replace(":", "").upper() != got.replace(":", ""):
                sys.exit(f"\n✗ the router's certificate is {got}, not the one expected: stopped")
            if not fp:
                print(f"   router certificate SHA-256: {got}")
                if input("   Same as Access → Secrets → Transport shows? [y/N] ").strip().lower() != "y":
                    sys.exit("\n✗ stopped: certificate not confirmed")
        else:
            c, got = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30), ""
        c.request("POST", "/v1/foundry/enroll",
                  json.dumps({"code": os.environ["CODE"], "hostname": socket.gethostname()}),
                  {"Content-Type": "application/json", "User-Agent": "foundry-workstation-installer/1"})
        r = c.getresponse(); raw = r.read()
    except OSError as e:
        sys.exit(f"\n✗ cannot reach the router at {router} ({e})")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    if r.status != 200:
        e = data.get("error") or {}
        sys.exit("\n✗ the router refused the code: "
                 + ((e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {r.status}"))
    if data.get("kind") != "workstation":
        sys.exit("\n✗ this code is for a Studio slot, not a workstation (use setup-host.sh)")
    data["router"] = router
    data.setdefault("router_http", router if u.scheme == "http" else "")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    kept = data.get("keys_kept") or []
    print(f"   ✓ {'project added' if kept else 'enrolled'}: {data['machine']} for {data['project']}; "
          f"router v{data['router_version']}" + (f" (keys kept: {len(kept)})" if kept else ""))
elif cmd == "get":
    d = json.load(open(os.environ["OUT"]))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print(" ".join(str(x) for x in d) if isinstance(d, list) else ("" if d is None else d))
PY
py() { OUT="$ENROLL" python3 "$API" "$@"; }
if [ "$REPAIR" = 1 ]; then
    bold "2/6 repairing / upgrading with this machine's enrolment (no code)"
    chown "$USER_NAME" "$WORK"
    (cd "$ROOT" && as_user "$FPY" -m foundry_client workstation fetch --out "$ENROLL" --router "$ROUTER" \
        ${FINGERPRINT:+--fingerprint "$FINGERPRINT"} ${WANT_PROJECT:+--project "$WANT_PROJECT"} >/dev/null) \
        || die "could not read this machine's enrolment from the router (revoked? run this again with a New code)"
    ok "enrolment read again: $(py get machine) for $(py get project); router v$(py get router_version)"
else
    bold "2/6 enrolling with the router"
    umask 077
    CODE="$CODE" ROUTER="$ROUTER" FINGERPRINT="$FINGERPRINT" OUT="$ENROLL" python3 "$API" enroll
    umask 022
fi
CODE=""
chown "$USER_NAME" "$ENROLL"
NAME="$(py get machine)"; PROJECT="$(py get project)"; VERSION="$(py get router_version)"
CLONE="$(py get game.clone_url)"; BRANCH="$(py get game.branch)"; STUDIO_SLOT="$(py get studio.slot)"
CLIENTS="$(py get coding_clients)"
GAME="$ROOT/game/$PROJECT"

bold "3/6 Foundry v$VERSION, the game, the settings"
UPDATING=""
if [ ! -d "$ROOT/app/.git" ]; then
    as_user git -c advice.detachedHead=false clone -q --branch "v$VERSION" "$REPO" "$ROOT/app" \
        || die "cannot download Foundry v$VERSION"
elif [ "$(as_user git -C "$ROOT/app" describe --tags --exact-match 2>/dev/null || true)" = "v$VERSION" ]; then
    ok "Foundry is at v$VERSION already"
elif client_active; then
    # the coordinator runs from this checkout: the client's own update moves it at a gap in
    # the work (restarting the coordinator and the client, rolling back on a failure)
    if UP=$(cd "$ROOT/app" && as_user "$FPY" -m foundry_client workstation update --tag "v$VERSION" 2>&1); then
        UPDATING="v$VERSION"
        ok "the running client moves Foundry to v$VERSION at the next gap (Client page → Update)"
    else
        warn "the client's update to v$VERSION did not start ($UP): the checkout stays where it is"
        warn "while the coordinator runs; stop the client (systemctl --user stop foundry-client as $USER_NAME) and run this again"
    fi
else
    as_user git -C "$ROOT/app" fetch -q -f "$REPO" "+refs/tags/v$VERSION:refs/tags/v$VERSION" \
        || die "cannot download Foundry v$VERSION"
    as_user git -C "$ROOT/app" -c advice.detachedHead=false checkout -q --force "v$VERSION"
fi
if [ ! -x "$ROOT/venv/bin/python" ]; then as_user python3 -m venv "$ROOT/venv"; fi
if [ -z "$UPDATING" ]; then
    as_user "$ROOT/venv/bin/pip" install -q --upgrade pip >/dev/null
    as_user "$ROOT/venv/bin/pip" install -q -e "$ROOT/app[client]" >/dev/null || die "pip install of Foundry failed"
fi
ok "Foundry $(as_user git -C "$ROOT/app" describe --tags) in $ROOT/app (Python in $ROOT/venv)"
# keys -> the secret store, client.toml, the repo key (values never printed)
SUMMARY=$(cd "$ROOT" && as_user "$FPY" -m foundry_client workstation apply --enrollment "$ENROLL" --root "$ROOT" \
          --secret-store "$STORE") || die "could not store the keys and settings"
sj() { echo "$SUMMARY" | "$FPY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
ok "keys $(sj '"stored" if d["keys_changed"] else "kept"') ($(sj 'd["secret_store"] + " store: " + (", ".join(d["secrets_stored"]) or "nothing new")'))"
ok "projects here: $(sj '", ".join(d["projects"])') (default: $(sj 'd["default_project"]'))"
KEYS_CHANGED=$(sj '1 if d["keys_changed"] else 0')
REPO_KIND=$(sj 'd["repo"]["kind"]')
REPO_PATH=$(sj 'd["repo"]["path"]')
REPO_WHY=$(sj 'd["repo"].get("problem","")')
[ -z "$REPO_WHY" ] || warn "repo key: $REPO_WHY"
if [ ! -d "$GAME/.git" ]; then
    if [ "$REPAIR" = 1 ] && [ "$REPO_KIND" = none ]; then
        # the game folder is gone: its repo key once more (the router hands it only on request)
        (cd "$ROOT" && as_user "$FPY" -m foundry_client workstation fetch --out "$ENROLL" --router "$ROUTER" \
            ${FINGERPRINT:+--fingerprint "$FINGERPRINT"} --project "$PROJECT" --repo-key >/dev/null) \
            && SUMMARY=$(cd "$ROOT" && as_user "$FPY" -m foundry_client workstation apply --enrollment "$ENROLL" \
                         --root "$ROOT" --secret-store "$STORE") \
            && REPO_KIND=$(sj 'd["repo"]["kind"]') && REPO_PATH=$(sj 'd["repo"]["path"]') || true
    fi
    if [ -z "$CLONE" ]; then
        ask CLONE "The game's clone URL (set workspace.clone_url on the router to skip this)" ""
        [ -n "$CLONE" ] || die "no clone URL for $PROJECT"
    fi
    GITENV=()
    if [ "$REPO_KIND" = ssh ]; then
        GITENV=(GIT_SSH_COMMAND="ssh -i $REPO_PATH -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    fi
    GITC=(); [ "$REPO_KIND" = https ] && GITC=(-c credential.helper=store)
    as_user env "${GITENV[@]}" git "${GITC[@]}" clone -q --branch "${BRANCH:-main}" "$CLONE" "$GAME" \
        || die "cannot clone $CLONE (a private repo needs workspace.repo_key_secret on the router)"
    if [ "$REPO_KIND" = ssh ]; then as_user git -C "$GAME" config core.sshCommand "ssh -i $REPO_PATH -o IdentitiesOnly=yes"; fi
    if [ "$REPO_KIND" = https ]; then as_user git -C "$GAME" config credential.helper store; fi
fi
ok "game $PROJECT in $GAME ($(as_user git -C "$GAME" rev-parse --abbrev-ref HEAD))"
PROFILE=$(cd "$ROOT" && as_user "$FPY" -m foundry_client workstation profile --enrollment "$ENROLL" --root "$ROOT") \
    || die "could not write project.toml / kilo.jsonc"
ok "project.toml and kilo.jsonc ($(echo "$PROFILE" | "$FPY" -c 'import json,sys; w=json.load(sys.stdin)["written"]; print("written: " + ", ".join(w) if w else "kept as they were")'))"
echo "$PROFILE" | "$FPY" -c 'import json,sys; [print("   ! " + p) for p in json.load(sys.stdin).get("problems") or []]'
case " $CLIENTS " in *" opencode "*) ok "coding client: OpenCode (components.opencode on; opencode.jsonc)" ;; esac
if [ "$TOOLS" = 1 ] && [ -f "$GAME/rokit.toml" ]; then
    (cd "$GAME" && as_user rokit install --no-trust-check >/dev/null 2>&1) && ok "the game's tools (rokit)" \
        || warn "rokit install failed in $GAME: run it there as $USER_NAME"
fi

bold "4/6 the work from the old workstation"
if [ -n "$IMPORT" ]; then
    cp "$IMPORT" "$WORK/import.fwx"; chown "$USER_NAME" "$WORK/import.fwx"
    if [ "$HAVE_TTY" = 1 ]; then
        read -r -s -p "The export's passphrase (typing is hidden): " PASS < "$TTY"; echo
    else
        read -r PASS || true
    fi
    printf '%s\n' "$PASS" | as_user "$FPY" -m foundry_client workstation import "$WORK/import.fwx" \
        --root "$ROOT" --passphrase-stdin > "$WORK/import.json" || die "the import failed"
    PASS=""
    "$FPY" - "$WORK/import.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"   ✓ {r['state_files']} state files, {len(r['branches'])} branches, "
      f"{sum(1 for w in r['worktrees'] if w['ok'])} track worktrees, {r['path_rewrites']} paths rewritten")
for b in r.get("branches_missing") or []:
    print(f"   ! branch {b}: its commit was not in the export")
for p, names in (r.get("kilo_servers_off") or {}).items():
    print(f"   ! {p}/kilo.jsonc: turned off (Windows only): {', '.join(names)}")
PY
else
    ok "nothing to import"
fi

bold "5/6 a Studio for main sprints"
if [ -n "$STUDIO_SLOT" ] && [ "$STUDIO" = 1 ]; then
    SCODE="$(py get studio.code)"
    printf '%s\n' "$SCODE" | FOUNDRY_SETUP_MATCHED=1 bash "$ROOT/app/contrib/studio-slot/proxmox/setup-host.sh" \
        --router "$ROUTER" --code-stdin ${FINGERPRINT:+--fingerprint "$FINGERPRINT"} \
        || warn "the Studio slot installer stopped: run it again with a New code for $STUDIO_SLOT"
    SCODE=""
    echo "   For main sprints to use it: Automation → Settings → studio.main_slot = $STUDIO_SLOT"
elif [ -n "$STUDIO_SLOT" ]; then
    warn "--no-studio: $STUDIO_SLOT is on the router but not installed here"
elif [ "$REPAIR" = 1 ]; then
    ok "a Studio slot here stays as installed (repair it with setup-host.sh, no code needed)"
else
    ok "none (main sprints use a Studio slot or another PC's Studio)"
fi

bold "6/6 the Foundry Client service"
rm -f "$ENROLL"
if [ "$START" = 0 ]; then
    echo "--no-start: stopped before the service. Start it with:"
    echo "   loginctl enable-linger $USER_NAME; runuser -l $USER_NAME -c '$FPY -m foundry_client install-service'"
    exit 0
fi
loginctl enable-linger "$USER_NAME" 2>/dev/null || warn "loginctl enable-linger failed: the service starts only while $USER_NAME is signed in"
for _ in $(seq 1 20); do [ -S "/run/user/$UID_N/bus" ] && break; sleep 1; done
WAS_ACTIVE=0; if client_active; then WAS_ACTIVE=1; fi
uctl "$FPY" -m foundry_client install-service >/dev/null || die "could not install the systemd service"
if [ "$WAS_ACTIVE" = 1 ] && [ "$KEYS_CHANGED" = 1 ]; then
    # (re-)enrolled: the old keys are revoked; the client and everything it started (the
    # coordinator, Kilo, OpenCode) start again with the new ones
    uctl systemctl --user restart foundry-client.service && ok "the running client restarted with the new keys" \
        || warn "could not restart the client: systemctl --user restart foundry-client (as $USER_NAME)"
fi
up=0
for _ in $(seq 1 45); do
    if uctl "$FPY" -m foundry_client status >/dev/null 2>&1 || uctl "$FPY" -m foundry_client status 2>&1 | grep -q Coordinator; then up=1; break; fi
    sleep 2
done
[ "$up" = 1 ] && ok "foundry-client.service is running" || warn "the client did not answer yet: journalctl --user -u foundry-client (as $USER_NAME)"
DEFAULT_PROJECT=$(sj 'd["default_project"]')
if [ "$PROJECT" = "$DEFAULT_PROJECT" ]; then
    uctl "$FPY" -m foundry_client apply-kilo-config --no-reload >/dev/null 2>&1 && ok "Kilo's role rules written into kilo.jsonc" \
        || warn "apply-kilo-config failed: run it as $USER_NAME once the client is up"
else
    # another project of this install: the coordinator's own generator on that project's file
    (cd "$ROOT/app" && as_user "$FPY" -m foundry_client coordinator --state "$ROOT/state" \
        --profiles "$ROOT/app/foundry_coordinator/projects" kilo-config "$PROJECT" \
        --path "$GAME/kilo.jsonc" --apply >/dev/null 2>&1) && ok "Kilo's role rules written into $GAME/kilo.jsonc" \
        || warn "kilo-config for $PROJECT failed: run it as $USER_NAME once the client is up"
fi
echo
uctl "$FPY" -m foundry_client doctor || true
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
cat <<EOF

$(printf '\033[1m')Done: $NAME is installed for $PROJECT.$(printf '\033[0m')

  Client page   ssh -L 47811:127.0.0.1:47811 $USER_NAME@${IP:-this-machine}, then http://localhost:47811/
  Status        runuser -l $USER_NAME -c '$FPY -m foundry_client status'
  Active?       Access → Machines → Workstations: if another workstation still runs $PROJECT,
                this one is on standby until you press Make active there.
  Repair        run this installer again and press Enter at the code (no code needed)
  Reinstall     run it with a new code (Access → Machines → New code): fresh keys
EOF
