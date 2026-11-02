#!/bin/bash
# Foundry main workstation installer (docs/plans/LINUX_WORKSTATION_PLAN.md W3): one run turns a
# Debian 13 / Ubuntu 24.04 machine (a Linux PC, or a Proxmox LXC made by
# create-workstation-lxc.sh) into a Foundry main workstation: the coordinator, the Kilo server,
# the game checkout, Blender for asset sprints, and optionally a Studio for main sprints in the
# Studio slot container on this same machine.
#
# Before you run it: Foundry router → Access → Machines → "Add a workstation". That gives you a
# one-time ENROLLMENT CODE (FNDR-XXXX-XXXX-XXXX).
#
# As root on the new workstation:
#   apt-get update -qq && apt-get install -y -qq curl
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/install/workstation.sh
#   bash workstation.sh
#
# It asks for the router address and the code, then:
#   1. installs the packages (git, Python, Node 22, Kilo, rokit, Blender 4.5 LTS; Docker for a
#      Studio here) and creates the `foundry` user;
#   2. trades the code for this workstation's three keys, stored in the client's secret store
#      (never shown), and the game's clone settings;
#   3. fetches Foundry at the router's version into <root>/app, clones the game into
#      <root>/game/<id>, and writes client.toml, project.toml and kilo.jsonc;
#   4. with --import FILE, brings over the work exported from the old workstation (W4);
#   5. with a Studio here, runs the Studio slot installer for it (20-40 min the first time);
#   6. installs the Foundry Client as a systemd service (starts at boot, no sign-in needed),
#      starts it, applies the Kilo rules and runs `doctor`.
# A new workstation starts on STANDBY when another one already runs the project: it starts no
# sprints until you press Make active on Access → Machines.
#
# Options:
#   --router URL          the router (default: asks; http://192.168.0.112:11435)
#   --code-stdin          read the enrollment code from stdin (non-interactive)
#   --fingerprint SHA256  the router's HTTPS certificate (an https router)
#   --root DIR            where everything lives (default /srv/foundry)
#   --import FILE         an export from the old workstation (foundry-client workstation export)
#   --import-only FILE    on an installed workstation: stop the client, import, start it again
#                         (no enrollment code needed)
#   --secret-store MODE   file (default: a mode-600 file, D-L5) | auto | keyring
#   --no-studio           skip the Studio here even if the router added one
#   --skip-tools          skip Node/Kilo/rokit/Blender (testing)
#   --no-start            stop before starting the service (testing)
# Re-running is safe: what exists is kept; a new code re-enrolls (fresh keys).
set -euo pipefail

REPO="${FOUNDRY_REPO:-https://github.com/Vipast82/foundry-router.git}"   # a fork or a test copy
ROOT=/srv/foundry ; ROUTER="" ; CODE="" ; FINGERPRINT="" ; CODE_STDIN=0 ; IMPORT=""
STORE=file ; STUDIO=1 ; TOOLS=1 ; START=1 ; IMPORT_ONLY=0
USER_NAME=foundry
NODE_VERSION=22.21.0
KILO_VERSION=7.8.8
ROKIT_VERSION=1.2.0
ROKIT_SHA256=951a7f3ec3d2a5e021fd1867d32f69f010ee4c2927f644b759578afc59c65fc0
BLENDER_VERSION=4.5.12                 # the same build as the slots (contrib/studio-slot/Dockerfile)
BLENDER_SHA256=95e3a2dfedba3bd32ca54fc355eac6b15a11986954ccb02815a07535d0120a25
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --router) ROUTER="$2"; shift 2 ;;
        --code-stdin) CODE_STDIN=1; shift ;;
        --fingerprint) FINGERPRINT="$2"; shift 2 ;;
        --root) ROOT="${2%/}"; shift 2 ;;
        --import) IMPORT="$2"; shift 2 ;;
        --import-only) IMPORT="$2"; IMPORT_ONLY=1; shift 2 ;;
        --secret-store) STORE="$2"; shift 2 ;;
        --no-studio) STUDIO=0; shift ;;
        --skip-tools) TOOLS=0; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
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

if [ "$IMPORT_ONLY" = 1 ]; then
    id "$USER_NAME" >/dev/null 2>&1 && [ -x "$ROOT/venv/bin/python" ]         || die "--import-only needs an installed workstation (run this installer first)"
    HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6); UID_N=$(id -u "$USER_NAME")
    uctl() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$UID_N"              DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" "$@"; }
    WORK=$(mktemp -d /tmp/foundry-ws.XXXXXX); chown "$USER_NAME" "$WORK"; chmod 700 "$WORK"
    trap 'rm -rf "$WORK"' EXIT
    cp "$IMPORT" "$WORK/import.fwx"; chown "$USER_NAME" "$WORK/import.fwx"
    if [ "$HAVE_TTY" = 1 ]; then read -r -s -p "The export's passphrase (typing is hidden): " PASS < "$TTY"; echo
    else read -r PASS || true; fi
    bold "stopping the Foundry Client"
    uctl systemctl --user stop foundry-client.service 2>/dev/null || true
    bold "importing"
    printf '%s
' "$PASS" | runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" "$ROOT/venv/bin/python"         -m foundry_client workstation import "$WORK/import.fwx" --root "$ROOT" --passphrase-stdin         || { uctl systemctl --user start foundry-client.service || true; die "the import failed (the client was started again as it was)"; }
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
if [ -z "$ROUTER" ]; then ask ROUTER "Foundry router address" "http://192.168.0.112:11435"; fi
ROUTER="${ROUTER%/}"
case "$ROUTER" in http://*|https://*) ;; *) ROUTER="http://$ROUTER" ;; esac
# this installer must match the router's release (as setup-host.sh): fetch the router's own copy
# and run that one, before the code is asked for (a code works once)
if [ -z "${FOUNDRY_SETUP_MATCHED:-}" ] && [ -z "${FOUNDRY_REPO:-}" ] && command -v curl >/dev/null 2>&1; then
    RV=$(curl -fsS -m 10 -k "$ROUTER/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' || true)
    NEW=$(mktemp /tmp/workstation.XXXXXX.sh)
    if [ -n "$RV" ] && curl -fsSLo "$NEW" \
            "https://raw.githubusercontent.com/Vipast82/foundry-router/v$RV/install/workstation.sh" \
            && ! cmp -s "$NEW" "$0"; then
        echo "   this installer is not the router's (v$RV): running v$RV's installer"
        FOUNDRY_SETUP_MATCHED=1 exec bash "$NEW" "${ORIG_ARGS[@]}" --router "$ROUTER"
    fi
    rm -f "$NEW"
fi
if [ "$CODE_STDIN" = 1 ]; then
    read -r CODE || true                   # a last line without a newline still counts
elif [ "$HAVE_TTY" = 1 ]; then
    read -r -s -p "Enrollment code from Access → Machines (typing is hidden): " CODE < "$TTY"; echo
fi
[ -n "$CODE" ] || die "no enrollment code"

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
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$ROOT" "$ROOT/game" "$ROOT/state" "$ROOT/config"
ok "user $USER_NAME (uid $(id -u "$USER_NAME")); everything under $ROOT"
if [ "$TOOLS" = 1 ]; then
    if ! node --version 2>/dev/null | grep -q '^v2[2-9]'; then
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
        npm install -g --silent "@kilocode/cli@$KILO_VERSION" >/dev/null 2>&1 || die "npm could not install Kilo"
    fi
    ok "Kilo $(kilo --version 2>/dev/null | head -1)"
    if ! command -v rokit >/dev/null; then
        cd /tmp
        curl -fsSLo rokit.zip "https://github.com/rojo-rbx/rokit/releases/download/v$ROKIT_VERSION/rokit-$ROKIT_VERSION-linux-x86_64.zip"
        echo "$ROKIT_SHA256  rokit.zip" | sha256sum -c - >/dev/null || die "rokit's checksum does not match"
        unzip -o -q rokit.zip -d /usr/local/bin && chmod 755 /usr/local/bin/rokit && rm -f rokit.zip
    fi
    ok "rokit $(rokit --version 2>/dev/null | awk '{print $NF}')"
    if [ ! -x /opt/blender/blender ]; then
        apt-get install -y -qq libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libxkbcommon0 \
            libsm6 libice6 libegl1 >/dev/null
        cd /tmp
        curl -fsSLo blender.tar.xz "https://download.blender.org/release/Blender${BLENDER_VERSION%.*}/blender-$BLENDER_VERSION-linux-x64.tar.xz"
        echo "$BLENDER_SHA256  blender.tar.xz" | sha256sum -c - >/dev/null || die "Blender's checksum does not match"
        mkdir -p /opt/blender && tar -xJf blender.tar.xz -C /opt/blender --strip-components=1
        ln -sf /opt/blender/blender /usr/local/bin/blender && rm -f blender.tar.xz
    fi
    ok "$(blender -b --version 2>/dev/null | head -1 || echo 'Blender: not runnable here')"
else
    warn "--skip-tools: Node, Kilo, rokit and Blender not installed"
fi

bold "2/6 enrolling with the router"
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
    print(f"   ✓ enrolled as {data['machine']} for {data['project']}; router v{data['router_version']}")
elif cmd == "get":
    d = json.load(open(os.environ["OUT"]))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print("" if d is None else d)
elif cmd == "set":                       # set <dotted.key> <value>
    p = os.environ["OUT"]; d = json.load(open(p)); cur = d
    ks = sys.argv[2].split(".")
    for k in ks[:-1]:
        cur = cur.setdefault(k, {})
    cur[ks[-1]] = sys.argv[3]
    fd = os.open(p, os.O_WRONLY | os.O_TRUNC); os.write(fd, json.dumps(d).encode()); os.close(fd)
PY
umask 077
CODE="$CODE" ROUTER="$ROUTER" FINGERPRINT="$FINGERPRINT" OUT="$ENROLL" python3 "$API" enroll
umask 022
CODE=""
chown "$USER_NAME" "$ENROLL"
py() { OUT="$ENROLL" python3 "$API" "$@"; }
NAME="$(py get machine)"; PROJECT="$(py get project)"; VERSION="$(py get router_version)"
CLONE="$(py get game.clone_url)"; BRANCH="$(py get game.branch)"; STUDIO_SLOT="$(py get studio.slot)"
GAME="$ROOT/game/$PROJECT"

bold "3/6 Foundry v$VERSION, the game, the settings"
as_user() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" "$@"; }
if [ ! -d "$ROOT/app/.git" ]; then
    as_user git -c advice.detachedHead=false clone -q --branch "v$VERSION" "$REPO" "$ROOT/app" \
        || die "cannot download Foundry v$VERSION"
else
    as_user git -C "$ROOT/app" fetch -q -f "$REPO" "+refs/tags/v$VERSION:refs/tags/v$VERSION" \
        || die "cannot download Foundry v$VERSION"
    as_user git -C "$ROOT/app" -c advice.detachedHead=false checkout -q --force "v$VERSION"
fi
if [ ! -x "$ROOT/venv/bin/python" ]; then as_user python3 -m venv "$ROOT/venv"; fi
as_user "$ROOT/venv/bin/pip" install -q --upgrade pip >/dev/null
as_user "$ROOT/venv/bin/pip" install -q -e "$ROOT/app[client]" >/dev/null || die "pip install of Foundry failed"
ok "Foundry $(as_user git -C "$ROOT/app" describe --tags) in $ROOT/app (Python in $ROOT/venv)"
FPY="$ROOT/venv/bin/python"
# keys -> the secret store, client.toml, the repo key (values never printed)
SUMMARY=$(as_user "$FPY" -m foundry_client workstation apply --enrollment "$ENROLL" --root "$ROOT" \
          --secret-store "$STORE") || die "could not store the keys and settings"
ok "keys stored ($(echo "$SUMMARY" | "$FPY" -c 'import json,sys; d=json.load(sys.stdin); print(d["secret_store"], "store:", ", ".join(d["secrets_stored"]))'))"
REPO_KIND=$(echo "$SUMMARY" | "$FPY" -c 'import json,sys; print(json.load(sys.stdin)["repo"]["kind"])')
REPO_PATH=$(echo "$SUMMARY" | "$FPY" -c 'import json,sys; print(json.load(sys.stdin)["repo"]["path"])')
REPO_WHY=$(echo "$SUMMARY" | "$FPY" -c 'import json,sys; print(json.load(sys.stdin)["repo"].get("problem",""))')
[ -z "$REPO_WHY" ] || warn "repo key: $REPO_WHY"
if [ ! -d "$GAME/.git" ]; then
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
PROFILE=$(as_user "$FPY" -m foundry_client workstation profile --enrollment "$ENROLL" --root "$ROOT") \
    || die "could not write project.toml / kilo.jsonc"
ok "project.toml and kilo.jsonc ($(echo "$PROFILE" | "$FPY" -c 'import json,sys; w=json.load(sys.stdin)["written"]; print("written: " + ", ".join(w) if w else "kept as they were")'))"
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
    ok "nothing to import (a fresh workstation)"
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
UID_N=$(id -u "$USER_NAME")
for _ in $(seq 1 20); do [ -S "/run/user/$UID_N/bus" ] && break; sleep 1; done
uctl() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$UID_N" \
         DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" "$@"; }
uctl "$FPY" -m foundry_client install-service >/dev/null || die "could not install the systemd service"
up=0
for _ in $(seq 1 45); do
    if uctl "$FPY" -m foundry_client status >/dev/null 2>&1 || uctl "$FPY" -m foundry_client status 2>&1 | grep -q Coordinator; then up=1; break; fi
    sleep 2
done
[ "$up" = 1 ] && ok "foundry-client.service is running" || warn "the client did not answer yet: journalctl --user -u foundry-client (as $USER_NAME)"
uctl "$FPY" -m foundry_client apply-kilo-config --no-reload >/dev/null 2>&1 && ok "Kilo's role rules written into kilo.jsonc" \
    || warn "apply-kilo-config failed: run it as $USER_NAME once the client is up"
echo
uctl "$FPY" -m foundry_client doctor || true
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
cat <<EOF

$(printf '\033[1m')Done: $NAME is installed for $PROJECT.$(printf '\033[0m')

  Client page   ssh -L 47811:127.0.0.1:47811 $USER_NAME@${IP:-this-machine}, then http://localhost:47811/
  Status        runuser -l $USER_NAME -c '$FPY -m foundry_client status'
  Active?       Access → Machines → Workstations: if another workstation still runs $PROJECT,
                this one is on standby until you press Make active there.
  Reinstall     run this installer again with a new code (Access → Machines → New code)
EOF
