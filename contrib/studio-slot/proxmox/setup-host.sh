#!/bin/bash
# Foundry Studio slot installer: one run turns a fresh Debian 13 machine (a Proxmox LXC; see
# create-slot-lxc.sh, which makes one and runs this inside it) into a connected, running slot.
#
# Before you run it: Foundry router → Access → Machines → "Add a Studio slot". That gives you a
# one-time ENROLLMENT CODE (FNDR-XXXX-XXXX-XXXX).
#
# Run as root on the slot machine:
#   apt-get update -qq && apt-get install -y -qq curl
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/contrib/studio-slot/proxmox/setup-host.sh
#   bash setup-host.sh --router http://<router>:11435
#
# It asks for the router address (unless --router) and the enrollment code, then:
#   1. installs Docker, git and ssh, and creates the `studio` user (uid 1000, the image's);
#   2. trades the code for this slot's key and settings (a code works once);
#   3. lets the workstation in over ssh (its public key comes from the router);
#   4. fetches Foundry at the router's version and makes the mirrors Foundry pushes to;
#   5. writes the slot's .env, builds the image and starts the container (20-40 min the first time);
#   6. waits until the container has pulled its secrets, then tells the router the slot is ready.
# Left for you: signing Studio in once (it prints how).
#
# Non-interactive: setup-host.sh --router <url> --code-stdin < file-with-the-code
# Also: --no-build (stop after step 4, for testing), --fingerprint <sha256> (https router).
# Re-running is safe and needs NO code on an enrolled slot: it repairs / upgrades with the key it
# has (press Enter at the code question, or --repair; the settings are read again from the
# router). A code re-enrolls (a fresh key): --reenrol asks for one even on an enrolled slot.
# Several slots on one host: the router gives the second slot on a host its own folder
# (/srv/studio-slot/<slot>), container and ports; --slot <id> picks the slot to repair. A host
# with one slot keeps /srv/studio-slot as before.
set -euo pipefail

BASE=/srv/studio-slot
USER_NAME=studio
SLOT_UID=1000
REPO=https://github.com/Vipast82/foundry-router.git
ROUTER="" ; CODE="" ; FINGERPRINT="" ; BUILD=1 ; CODE_STDIN=0 ; MODE=auto ; SLOT_ARG=""
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --router) ROUTER="$2"; shift 2 ;;
        --code-stdin) CODE_STDIN=1; shift ;;
        --fingerprint) FINGERPRINT="$2"; shift 2 ;;
        --no-build) BUILD=0; shift ;;
        --repair) MODE=repair; shift ;;
        --reenrol|--reenroll) MODE=enrol; shift ;;
        --slot) SLOT_ARG="$2"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run this as root (the Proxmox console of the LXC)"

# the game mirror's name: AnimalUprising for that project (every slot so far), else the
# project id (foundry_coordinator/slots.py game_repo_name)
game_name() { case "${1,,}" in ''|animaluprising) echo AnimalUprising ;; *) echo "$1" ;; esac; }
envget() { sed -n "s/^$2=//p" "$1/slot.env" 2>/dev/null | tail -1; }

# ---- an enrolment already on this host? (repair / upgrade without a code)
ENROLLED=()
if [ -f "$BASE/slot-key/slot-key.txt" ] && [ -f "$BASE/slot.env" ]; then ENROLLED+=("$BASE"); fi
for d in "$BASE"/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"
    if [ -f "$d/slot-key/slot-key.txt" ] && [ -f "$d/slot.env" ]; then ENROLLED+=("$d"); fi
done
ENR_ROOT=""
if [ -n "$SLOT_ARG" ]; then
    for d in "${ENROLLED[@]}"; do [ "$(envget "$d" FOUNDRY_SLOT)" = "$SLOT_ARG" ] && ENR_ROOT="$d"; done
    [ -n "$ENR_ROOT" ] || [ "$MODE" = enrol ] || [ "$MODE" = auto ] || die "no enrolled slot $SLOT_ARG on this host"
elif [ "${#ENROLLED[@]}" = 1 ]; then
    ENR_ROOT="${ENROLLED[0]}"
elif [ "${#ENROLLED[@]}" -gt 1 ] && [ "$MODE" = repair ]; then
    die "several slots on this host ($(for d in "${ENROLLED[@]}"; do printf '%s ' "$(envget "$d" FOUNDRY_SLOT)"; done)): say which with --slot <id>"
fi
[ "$MODE" != repair ] || [ -n "$ENR_ROOT" ] || die "--repair: this host has no enrolled slot (run it with a code)"

echo
echo "Foundry Studio slot installer"
echo "-----------------------------"
if [ -z "$ROUTER" ] && [ -n "$ENR_ROOT" ]; then ROUTER="$(envget "$ENR_ROOT" FOUNDRY_URL)"; fi
if [ -z "$FINGERPRINT" ] && [ -n "$ENR_ROOT" ]; then FINGERPRINT="$(envget "$ENR_ROOT" FOUNDRY_TLS_FINGERPRINT)"; fi
if [ -z "$ROUTER" ]; then
    read -r -p "Foundry router address (e.g. http://192.168.0.10:11435): " ROUTER
fi
[ -n "$ROUTER" ] || die "no router address (--router http://<router>:11435)"
ROUTER="${ROUTER%/}"
case "$ROUTER" in http://*|https://*) ;; *) ROUTER="http://$ROUTER" ;; esac
# This installer must match the router's release: an older copy left from an earlier install
# writes an old slot.env (2026-11-01: slot-1's VNC stayed on localhost). Before the code is
# asked for (a code works once), fetch the router's own installer and run that one instead.
if [ -z "${FOUNDRY_SETUP_MATCHED:-}" ] && command -v curl >/dev/null 2>&1; then
    RV=$(curl -fsS -m 10 -k "$ROUTER/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' || true)
    NEW=$(mktemp /tmp/setup-host.XXXXXX.sh)
    if [ -n "$RV" ] && curl -fsSLo "$NEW" \
            "https://raw.githubusercontent.com/Vipast82/foundry-router/v$RV/contrib/studio-slot/proxmox/setup-host.sh" \
            && ! cmp -s "$NEW" "$0"; then
        echo "   this installer is older or newer than the router (v$RV): running v$RV's installer"
        cp "$NEW" "$0" 2>/dev/null || true
        FOUNDRY_SETUP_MATCHED=1 exec bash "$NEW" "${ORIG_ARGS[@]}" --router "$ROUTER"
    fi
    rm -f "$NEW"
fi
REPAIR=0
if [ "$MODE" = repair ]; then
    REPAIR=1
elif [ "$CODE_STDIN" = 1 ]; then
    read -r CODE || true
elif [ -n "$ENR_ROOT" ] && [ "$MODE" = auto ]; then
    echo "   this host is enrolled as $(envget "$ENR_ROOT" FOUNDRY_SLOT) ($(envget "$ENR_ROOT" FOUNDRY_PROJECT))"
    read -r -s -p "Enrollment code to re-enrol, or Enter to repair / upgrade with this enrolment: " CODE; echo
else
    read -r -s -p "Enrollment code from Access → Machines (typing is hidden): " CODE; echo
fi
if [ "$REPAIR" = 0 ] && [ -z "$CODE" ]; then
    { [ -n "$ENR_ROOT" ] && [ "$MODE" = auto ]; } || die "no enrollment code"
    REPAIR=1
fi

bold "1/6 packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssh-server python3 docker.io >/dev/null
if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y -qq docker-compose >/dev/null 2>&1 || apt-get install -y -qq docker-compose-v2 >/dev/null
fi
systemctl enable --now docker ssh >/dev/null 2>&1 || true
if docker info >/dev/null 2>&1; then
    ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short 2>/dev/null)"
elif [ "$BUILD" = 0 ]; then
    warn "Docker is not running here (--no-build: carrying on)"
else
    die "Docker does not start. A Proxmox LXC needs features: nesting=1,keyctl=1 (lxc.conf.example)"
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
    if getent passwd "$SLOT_UID" >/dev/null; then
        die "uid $SLOT_UID is taken by $(getent passwd "$SLOT_UID" | cut -d: -f1); the slot image runs as uid $SLOT_UID"
    fi
    useradd -m -s /bin/bash -u "$SLOT_UID" "$USER_NAME"
fi
usermod -aG docker "$USER_NAME"
ok "user $USER_NAME (uid $(id -u "$USER_NAME"), docker group)"

# the router calls (enroll, repair, ready) and reading the answer; values never printed. The
# answer holds the slot key until it is saved: a private folder, removed whatever happens.
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$BASE"
WORK=$(mktemp -d /tmp/foundry-slot.XXXXXX); chmod 700 "$WORK"
ENROLL="$WORK/enroll.json"
API="$WORK/installer-api.py"
trap 'rm -rf "$WORK"' EXIT
cat > "$API" <<'PY'
import hashlib, http.client, json, os, socket, ssl, sys, urllib.parse

def conn(router, fp):
    u = urllib.parse.urlsplit(router)
    if u.scheme != "https":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30), ""
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
    return c, got

def call(c, method, path, body=None, key=""):
    h = {"Content-Type": "application/json", "User-Agent": "foundry-slot-installer/1"}
    if key:
        h["Authorization"] = "Bearer " + key
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return r.status, data

def why(status, data):
    e = data.get("error") or {}
    return (e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {status}"

def save(out, data):
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)

def check(data):
    if data.get("kind") == "workstation":
        sys.exit("\n✗ this code is for a workstation: run install/workstation.sh there")
    if data.get("os") == "windows":
        sys.exit("\n✗ this slot is a Windows slot on the router: run install/slot.ps1 on it")

cmd = sys.argv[1]
if cmd in ("enroll", "repair"):
    router, out = os.environ["ROUTER"], os.environ["OUT"]
    try:
        c, fp = conn(router, os.environ.get("FINGERPRINT", ""))
        if cmd == "enroll":
            st, data = call(c, "POST", "/v1/foundry/enroll",
                            {"code": os.environ["CODE"], "hostname": socket.gethostname()})
        else:                            # the key this slot has: the settings again, no new key
            key = open(os.environ["KEYFILE"]).read().strip()
            st, data = call(c, "GET", "/v1/foundry/secrets/enrollment", key=key)
            key = None
    except OSError as e:
        sys.exit(f"\n✗ cannot reach the router at {router} ({e}): is the address right, and can "
                 "this machine reach it?")
    if st != 200:
        sys.exit(f"\n✗ the router refused the code: {why(st, data)}" if cmd == "enroll" else
                 f"\n✗ the router refused this slot's key: {why(st, data)} (revoked? run this "
                 "again with a New code from Access → Machines)")
    check(data)
    data["router"], data["tls_pinned"] = router, fp
    save(out, data)
    print(f"   ✓ {'enrolled' if cmd == 'enroll' else 'enrolment read again'} as {data['machine']} "
          f"({data['project']}/{data['slot']}); router v{data['router_version']}")
elif cmd == "get":                       # get <dotted.key>  (from the answer)
    d = json.load(open(os.environ["OUT"]))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print(" ".join(str(x) for x in d) if isinstance(d, list) else ("" if d is None else d))
elif cmd == "write-key":                 # write-key <path>
    d = json.load(open(os.environ["OUT"]))
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, (d["key"] + "\n").encode())
    os.close(fd)
elif cmd == "ready":                     # ready <key file>
    d = json.load(open(os.environ["OUT"]))
    key = open(sys.argv[2]).read().strip()
    try:
        c, _ = conn(d["router"], d.get("tls_pinned") or "x")
        st, data = call(c, "POST", "/v1/foundry/secrets/ready", key=key)
    except OSError as e:
        sys.exit(f"cannot reach the router ({e})")
    if st != 200:
        sys.exit(why(st, data))
PY
chmod 600 "$API"
py() { OUT="$ENROLL" python3 "$API" "$@"; }

KEYS_CHANGED=0
if [ "$REPAIR" = 1 ]; then
    bold "2/6 repairing / upgrading with this slot's enrolment (no code)"
    KEYFILE="$ENR_ROOT/slot-key/slot-key.txt" ROUTER="$ROUTER" FINGERPRINT="$FINGERPRINT" \
        OUT="$ENROLL" python3 "$API" repair
else
    bold "2/6 enrolling with the router"
    umask 077
    CODE="$CODE" ROUTER="$ROUTER" FINGERPRINT="$FINGERPRINT" OUT="$ENROLL" python3 "$API" enroll
    umask 022
    KEYS_CHANGED=1
fi
CODE=""
SLOT="$(py get slot)"; PROJECT="$(py get project)"; VERSION="$(py get router_version)"
case "$SLOT" in ''|*[!A-Za-z0-9_-]*) die "the router's slot id is not usable: '$SLOT'" ;; esac
GAME="$(game_name "$PROJECT")"
# this slot's folder: /srv/studio-slot, or /srv/studio-slot/<slot> when the router gave it its
# own (several slots on one host: foundry_host_dir = /srv/studio-slot/<slot>/foundry)
HOST_DIR="$(py get settings.foundry_host_dir)"
case "$HOST_DIR" in
    "$BASE"/*/foundry) ROOT="${HOST_DIR%/foundry}" ;;
    *) ROOT="$BASE" ;;
esac
if [ -n "$ENR_ROOT" ] && [ "$REPAIR" = 1 ] && [ "$ENR_ROOT" != "$ROOT" ]; then
    warn "this slot lives in $ENR_ROOT here (the router says $ROOT): kept as it is"
    ROOT="$ENR_ROOT"
fi
SHARED=0; [ "$ROOT" = "$BASE" ] || SHARED=1
CONTAINER="$(py get settings.container)"; CONTAINER="${CONTAINER:-studio-slot}"
PLACE="$(py get settings.place_id)"; UNIVERSE="$(py get settings.universe_id)"
NEVER="$(py get settings.rojo_never_place_ids)"
# noVNC: on the LAN unless the slot's "VNC on the LAN" setting is off (Access → Machines → VNC)
VNC_LAN="$(py get vnc.lan)"; VNC_PORT="$(py get vnc.port)"; VNC_PW="$(py get vnc.password)"
case "$VNC_PORT" in ''|*[!0-9]*) VNC_PORT=6080 ;; esac
if [ "$VNC_LAN" = False ]; then VNC_BIND=127.0.0.1; else VNC_BIND=0.0.0.0; fi
port_or() { case "$1" in ''|*[!0-9]*) echo "$2" ;; *) echo "$1" ;; esac; }
AGENT_PORT=$(port_or "$(py get settings.agent_port)" 47811)
KILO_PORT=$(port_or "$(py get settings.kilo_port)" 4096)
OPENCODE_PORT=$(port_or "$(py get settings.opencode_port)" 4097)
LANES_ROOT="$(py get settings.lanes_root)"; LANES_ROOT="${LANES_ROOT:-/srv/foundry-lanes}"
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$ROOT"
install -d -m 700 -o "$SLOT_UID" -g "$SLOT_UID" "$ROOT/slot-key"
if [ "$REPAIR" = 0 ]; then
    py write-key "$ROOT/slot-key/slot-key.txt"
    chown "$SLOT_UID:$SLOT_UID" "$ROOT/slot-key/slot-key.txt"
    ok "slot key saved in $ROOT/slot-key/slot-key.txt (mode 600, never shown)"
else
    ok "slot key kept ($ROOT/slot-key/slot-key.txt)"
fi
WAS_RUNNING=0
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" = true ]; then WAS_RUNNING=1; fi

bold "3/6 Foundry v$VERSION and the mirrors"
cd "$ROOT"
[ -d "$GAME.git" ] || runuser -u "$USER_NAME" -- git init -q --bare -b master "$GAME.git"
[ -d foundry.git ] || runuser -u "$USER_NAME" -- git init -q --bare -b deploy foundry.git
if [ ! -d foundry/.git ]; then
    rm -rf foundry
    # a full clone (~7 MB): a shallow one cannot be pushed to the foundry.git mirror
    runuser -u "$USER_NAME" -- git -c advice.detachedHead=false clone -q --branch "v$VERSION" "$REPO" foundry \
        || die "cannot download Foundry v$VERSION from GitHub"
    # from now on the workstation pushes its deployed Foundry to foundry.git and this checkout follows
    runuser -u "$USER_NAME" -- git -C foundry push -q --force "$ROOT/foundry.git" HEAD:refs/heads/deploy
    runuser -u "$USER_NAME" -- git -C foundry remote set-url origin "$ROOT/foundry.git"
elif [ "$WAS_RUNNING" = 1 ]; then
    # the coordinator moves this checkout to its deployed Foundry before every gate: moving it
    # here under a running slot could change the files of a gate in progress
    ok "Foundry checkout kept at $(runuser -u "$USER_NAME" -- git -C foundry describe --tags --always) (the slot is running; the coordinator moves it to its deployed release before each gate)"
else
    runuser -u "$USER_NAME" -- git -C foundry fetch -q "$REPO" "+refs/tags/v$VERSION:refs/tags/v$VERSION" \
        || die "cannot download Foundry v$VERSION from GitHub"
    runuser -u "$USER_NAME" -- git -C foundry -c advice.detachedHead=false checkout -q --force "v$VERSION"
    runuser -u "$USER_NAME" -- git -C foundry push -q --force "$ROOT/foundry.git" HEAD:refs/heads/deploy
    runuser -u "$USER_NAME" -- git -C foundry remote set-url origin "$ROOT/foundry.git"
fi
chmod -R a+rX "$GAME.git" foundry.git foundry
ok "Foundry $(runuser -u "$USER_NAME" -- git -C foundry describe --tags --always) in $ROOT/foundry; game mirror $ROOT/$GAME.git"

bold "4/6 ssh access for Foundry"
# The main PC's coordinator registers its own ssh key with the router (v0.162.0), and this host
# reports its ssh host keys there. slot-keys.py does both now and every 5 minutes after (a
# systemd timer), so a new or replaced main PC gets in without reinstalling this slot.
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"
AK="/home/$USER_NAME/.ssh/authorized_keys"
touch "$AK"; chown "$USER_NAME:$USER_NAME" "$AK"; chmod 600 "$AK"
install -m 755 "$ROOT/foundry/contrib/studio-slot/proxmox/slot-keys.py" /usr/local/sbin/foundry-slot-keys
if ! /usr/local/sbin/foundry-slot-keys --root "$ROOT" --router "$(py get router)" --fingerprint "$(py get tls_pinned)"; then
    warn "could not sync the ssh keys with the router now; the timer retries every 5 minutes"
fi
# one timer per slot folder: foundry-slot-keys (the host's /srv/studio-slot slot) and
# foundry-slot-keys-<slot> for a slot in its own folder
UNIT=foundry-slot-keys; [ "$SHARED" = 0 ] || UNIT="foundry-slot-keys-$SLOT"
cat > "/etc/systemd/system/$UNIT.service" <<EOU
[Unit]
Description=Foundry: sync Studio slot $SLOT's ssh keys with the router
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/foundry-slot-keys --root $ROOT
EOU
cat > "/etc/systemd/system/$UNIT.timer" <<EOU
[Unit]
Description=Foundry: sync Studio slot $SLOT's ssh keys with the router every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOU
if systemctl daemon-reload >/dev/null 2>&1 && systemctl enable --now "$UNIT.timer" >/dev/null 2>&1; then
    ok "key refresh every 5 minutes ($UNIT.timer)"
else
    warn "could not start $UNIT.timer: run 'foundry-slot-keys --root $ROOT' after adding a main PC"
fi
NKEYS=$(sed -n '/^# >>> foundry-managed keys/,/^# <<< foundry-managed keys/p' "$AK" | grep -c '^ssh-\|^ecdsa-\|^sk-' || true)
if [ "${NKEYS:-0}" = 0 ] && [ "$(grep -c '^ssh-\|^ecdsa-\|^sk-' "$AK" || true)" = 0 ]; then
    warn "no main PC has registered its ssh key yet: start the coordinator on your main PC (it"
    warn "registers itself), or add a key on Access → Machines; this slot allows it within 5 minutes."
else
    ok "${NKEYS:-0} workstation key(s) allowed from the router"
fi

bold "5/6 the slot container ($CONTAINER)"
COMPOSE_DIR="$ROOT/foundry/contrib/studio-slot/proxmox"
STUDIO_OPEN=""
if [ -n "$PLACE" ] && [ "$PLACE" != 0 ]; then
    STUDIO_OPEN="roblox-studio:1+launchmode:edit+task:EditPlace+placeId:$PLACE"
    if [ -n "$UNIVERSE" ] && [ "$UNIVERSE" != 0 ]; then STUDIO_OPEN="$STUDIO_OPEN+universeId:$UNIVERSE"; fi
else
    warn "no place set for $SLOT on the router: Studio opens its start page (set place_id on Access → Machines)"
fi
MEM_G=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1048576 - 3 )); [ "$MEM_G" -ge 6 ] || MEM_G=6
# CPU: the slot's cpu_limit (Access → Machines → Settings), 0 = a third of the cores (at least 4).
# Without a GPU, Studio redraws its view in software on every core it gets, even when idle; the
# renderer's threads (LP_NUM_THREADS) follow the limit. foundry-slot-keys keeps both in step later.
NPROC=$(nproc); CPU_SET="$(py get settings.cpu_limit)"
case "$CPU_SET" in ''|*[!0-9]*) CPU_SET=0 ;; esac
if [ "$CPU_SET" -gt 0 ]; then CPUS=$CPU_SET; else CPUS=$(( NPROC / 3 )); [ "$CPUS" -ge 4 ] || CPUS=4; fi
[ "$CPUS" -le "$NPROC" ] || CPUS=$NPROC
SIZE="$(py get settings.desktop_size)"
case "$SIZE" in [0-9]*x[0-9]*) ;; *) SIZE=2560x1440 ;; esac
# GPU (v0.162.4): the host's iGPU draws Studio's view when the LXC has one (Proxmox → the LXC →
# Resources → Device Passthrough /dev/dri/renderD128, mode 0666) and "Use the GPU" is on.
COMPOSE_FILES=docker-compose.yml; GPU_TXT="software drawing (no GPU in this machine)"
if [ "$(py get settings.gpu)" = False ]; then
    GPU_TXT="software drawing (Use the GPU is off on Access → Machines)"
elif [ -e /dev/dri/renderD128 ]; then
    if [ "$(stat -c %a /dev/dri/renderD128 | cut -c3)" -ge 6 ] 2>/dev/null; then
        COMPOSE_FILES=docker-compose.yml:docker-compose.gpu.yml; GPU_TXT="the GPU (/dev/dri)"
    else
        warn "/dev/dri/renderD128 is here, but the slot (uid $SLOT_UID) cannot open it: set mode 0666 in"
        warn "Proxmox → the LXC → Resources → Device Passthrough, restart the LXC, run this again"
        GPU_TXT="software drawing (the GPU's mode is not 0666)"
    fi
fi
# B3 (SLOT_BLENDER_PLAN): with the GPU drawing, Studio needs ~1.5 cores, so the automatic CPU
# limit is all cores but 2 (at least 4) and Blender gets the rest; foundry-slot-keys keeps it so
if [ "$CPU_SET" -eq 0 ] && [ "$COMPOSE_FILES" != docker-compose.yml ]; then
    CPUS=$(( NPROC - 2 )); [ "$CPUS" -ge 4 ] || CPUS=4; [ "$CPUS" -le "$NPROC" ] || CPUS=$NPROC
fi
# Lanes on this slot (Distributed Foundry phase b, docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B2):
# the slot's `lanes` setting (Automation → Settings → Studio slots). On: the lane store
# <lanes_root>/<project>.git (bare, borrowing the game copy's objects) and the compose
# override that starts the lane agent, Kilo and OpenCode (ports on this host's 127.0.0.1 only).
LANES_TXT="off (Studio only)"
if [ "$(py get settings.lanes)" = True ]; then
    install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$LANES_ROOT"
    LSTORE="$LANES_ROOT/$PROJECT.git"
    if [ ! -d "$LSTORE" ]; then
        if runuser -u "$USER_NAME" -- git clone -q --bare --shared "$ROOT/$GAME.git" "$LSTORE"; then
            ok "lane store $LSTORE (shares $ROOT/$GAME.git's objects)"
        else
            warn "could not make the lane store $LSTORE; lanes stay off until this runs again"
        fi
    fi
    if [ -d "$LSTORE" ]; then
        COMPOSE_FILES="$COMPOSE_FILES:docker-compose.lanes.yml"; LANES_TXT="on ($LSTORE)"
    fi
fi
# a trial override from 2026-11-01 (it also added /dev/dri) would add the device twice
OVR="$COMPOSE_DIR/docker-compose.override.yml"
[ -f "$OVR" ] && head -1 "$OVR" | grep -q '^# TRIAL' && rm -f "$OVR"
# the AU LIVE place stays on Rojo's never-list when the router names none (as the compose
# default always did); another project names its own on Access → Machines
if [ -z "$NEVER" ] && [ "$GAME" = AnimalUprising ]; then NEVER=138024886323256; fi
umask 077
{
    echo "# written by setup-host.sh for $SLOT ($PROJECT) on $(date -u +%FT%TZ); re-run the installer to change it"
    echo "STUDIO_OPEN=$STUDIO_OPEN"
    echo "SLOT_CONTAINER=$CONTAINER"
    echo "SLOT_NOVNC_BIND=$VNC_BIND"
    echo "SLOT_NOVNC_PORT=$VNC_PORT"
    echo "SLOT_CPUS=$CPUS"
    echo "SLOT_RENDER_THREADS=$CPUS"
    echo "SLOT_GEOMETRY=$SIZE"
    echo "COMPOSE_FILE=$COMPOSE_FILES"
    echo "SLOT_MEM=${MEM_G}g"
    echo "SLOT_MULTIPLAYER=$([ "$MEM_G" -ge 14 ] && echo 1 || echo 0)"
    echo "FOUNDRY_URL=$(py get router)"
    echo "FOUNDRY_TLS_FINGERPRINT=$(py get tls_pinned)"
    echo "FOUNDRY_PROJECT=$PROJECT"
    echo "FOUNDRY_SLOT=$SLOT"
    echo "ROJO_NEVER_PLACE_IDS=$NEVER"
    # this slot's folder and game mirror name (docker-compose*.yml), its lane ports and folder
    echo "SLOT_ROOT=$ROOT"
    echo "SLOT_GAME=$GAME"
    echo "SLOT_AGENT_PORT=$AGENT_PORT"
    echo "SLOT_KILO_PORT=$KILO_PORT"
    echo "SLOT_OPENCODE_PORT=$OPENCODE_PORT"
    echo "SLOT_LANES_ROOT=$LANES_ROOT"
    # a slot in its own folder is its own compose project (its own studio-home volume); the
    # host's first slot keeps compose's default name, so its volume stays the same
    if [ "$SHARED" = 1 ]; then echo "COMPOSE_PROJECT_NAME=studio-$(echo "$SLOT" | tr '[:upper:]' '[:lower:]')"; fi
} > "$ROOT/slot.env"
umask 022
chown "$USER_NAME:$USER_NAME" "$ROOT/slot.env"; chmod 600 "$ROOT/slot.env"
ln -sf "$ROOT/slot.env" "$COMPOSE_DIR/.env"
ok "$ROOT/slot.env: $CPUS of $NPROC CPUs, ${MEM_G} GB, desktop $SIZE, place ${PLACE:-none}"
ok "Studio draws with $GPU_TXT"
ok "lanes: $LANES_TXT"
if [ "$BUILD" = 0 ]; then
    echo; echo "--no-build: stopped before the build. Build and start later with:"
    echo "   runuser -l $USER_NAME -c 'cd $COMPOSE_DIR && docker compose up -d --build'"
    exit 0
fi
echo "   building the image: 20-40 minutes the first time (progress: tail -f $ROOT/build.log)"
if ! runuser -l "$USER_NAME" -c "cd '$COMPOSE_DIR' && docker compose up -d --build" >"$ROOT/build.log" 2>&1; then
    tail -20 "$ROOT/build.log" >&2
    die "the build failed (full log: $ROOT/build.log)"
fi
if [ "$KEYS_CHANGED" = 1 ] && [ "$WAS_RUNNING" = 1 ]; then
    # re-enrolled: the old key is revoked; the running slot starts again with the new one
    docker restart "$CONTAINER" >/dev/null && ok "container $CONTAINER restarted with the new key" \
        || warn "could not restart $CONTAINER: docker restart $CONTAINER"
fi
ok "container $CONTAINER is running"

bold "6/6 checking the slot"
pulled=0
for _ in $(seq 1 45); do
    if docker exec "$CONTAINER" slot-secrets --status 2>/dev/null | grep -q "^pulled"; then pulled=1; break; fi
    sleep 4
done
if [ "$pulled" = 1 ]; then
    ok "$(docker exec "$CONTAINER" slot-secrets --status 2>/dev/null)"
else
    warn "no secrets pull yet: docker exec $CONTAINER cat /home/studio/slot-secrets.log"
fi
if why=$(py ready "$ROOT/slot-key/slot-key.txt" 2>&1); then
    ok "the router switched $SLOT on: Foundry sends its gates here from now on"
else
    warn "could not tell the router the slot is ready ($why): switch $SLOT on under Automation → Settings → Studio slots"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [ "$VNC_BIND" = 0.0.0.0 ]; then
    OPEN="Open from any PC on your LAN:  http://$IP:$VNC_PORT/"
    if [ "$VNC_PW" = True ]; then OPEN="$OPEN   (it asks for the VNC password)"
    else OPEN="$OPEN   (no password; optional: the SLOT_VNC_PASSWORD secret on Access → Secrets)"; fi
else
    OPEN="On your PC:  ssh -L $VNC_PORT:localhost:$VNC_PORT $USER_NAME@$IP
     then open:  http://localhost:$VNC_PORT/"
fi
cat <<EOF

$(printf '\033[1m')Done: $SLOT is installed and connected.$(printf '\033[0m')

One step left, once: sign Studio in (a repaired slot stays signed in).
  1. $OPEN
  2. Sign in to Roblox in the Studio window; it stays signed in across restarts and rebuilds.

Useful later:
  status      docker exec $CONTAINER slot-secrets --status
  logs        docker logs $CONTAINER
  repair      run this installer again and press Enter at the code (no code needed)
  reinstall   run it with a new code (Access → Machines → New code): a fresh key
EOF
