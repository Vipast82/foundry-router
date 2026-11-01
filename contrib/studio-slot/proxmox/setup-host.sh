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
#   bash setup-host.sh
#
# It asks for two things, the router address and the enrollment code, then:
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
# Re-running is safe: what exists is kept; a new code re-enrolls (a fresh key).
set -euo pipefail

ROOT=/srv/studio-slot
USER_NAME=studio
SLOT_UID=1000
REPO=https://github.com/Vipast82/foundry-router.git
ROUTER="" ; CODE="" ; FINGERPRINT="" ; BUILD=1 ; CODE_STDIN=0
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --router) ROUTER="$2"; shift 2 ;;
        --code-stdin) CODE_STDIN=1; shift ;;
        --fingerprint) FINGERPRINT="$2"; shift 2 ;;
        --no-build) BUILD=0; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run this as root (the Proxmox console of the LXC)"

echo
echo "Foundry Studio slot installer"
echo "-----------------------------"
if [ -z "$ROUTER" ]; then
    read -r -p "Foundry router address [http://192.168.0.112:11435]: " ROUTER
    ROUTER="${ROUTER:-http://192.168.0.112:11435}"
fi
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
if [ "$CODE_STDIN" = 1 ]; then
    read -r CODE
else
    read -r -s -p "Enrollment code from Access → Machines (typing is hidden): " CODE; echo
fi
[ -n "$CODE" ] || die "no enrollment code"

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

# the router calls (enroll, ready) and reading the enrollment answer; values never printed
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$ROOT"
ENROLL="$ROOT/.enroll.json"
API="$ROOT/.installer-api.py"
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

cmd = sys.argv[1]
if cmd == "enroll":
    router, out = os.environ["ROUTER"], os.environ["OUT"]
    try:
        c, fp = conn(router, os.environ.get("FINGERPRINT", ""))
        st, data = call(c, "POST", "/v1/foundry/enroll",
                        {"code": os.environ["CODE"], "hostname": socket.gethostname()})
    except OSError as e:
        sys.exit(f"\n✗ cannot reach the router at {router} ({e}): is the address right, and can "
                 "this machine reach it?")
    if st != 200:
        sys.exit(f"\n✗ the router refused the code: {why(st, data)}")
    data["router"], data["tls_pinned"] = router, fp
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    print(f"   ✓ enrolled as {data['machine']} ({data['project']}/{data['slot']}); "
          f"router v{data['router_version']}")
elif cmd == "get":                       # get <dotted.key>  (from the enrollment answer)
    d = json.load(open(os.environ["OUT"]))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print(" ".join(str(x) for x in d) if isinstance(d, list) else ("" if d is None else d))
elif cmd == "write-key":                 # write-key <path>
    d = json.load(open(os.environ["OUT"]))
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, (d["key"] + "\n").encode())
    os.close(fd)
elif cmd == "ssh-keys":
    print("\n".join(json.load(open(os.environ["OUT"])).get("ssh_authorized_keys") or []))
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
# the enrollment answer holds the slot key until it is saved: never left behind, whatever happens
trap 'rm -f "$ENROLL" "$API"' EXIT
py() { OUT="$ENROLL" python3 "$API" "$@"; }

bold "2/6 enrolling with the router"
umask 077
CODE="$CODE" ROUTER="$ROUTER" FINGERPRINT="$FINGERPRINT" OUT="$ENROLL" python3 "$API" enroll
umask 022
CODE=""
SLOT="$(py get slot)"; PROJECT="$(py get project)"; VERSION="$(py get router_version)"
CONTAINER="$(py get settings.container)"; CONTAINER="${CONTAINER:-studio-slot}"
PLACE="$(py get settings.place_id)"; UNIVERSE="$(py get settings.universe_id)"
NEVER="$(py get settings.rojo_never_place_ids)"
# noVNC: on the LAN unless the slot's "VNC on the LAN" setting is off (Access → Machines → VNC)
VNC_LAN="$(py get vnc.lan)"; VNC_PORT="$(py get vnc.port)"; VNC_PW="$(py get vnc.password)"
case "$VNC_PORT" in ''|*[!0-9]*) VNC_PORT=6080 ;; esac
if [ "$VNC_LAN" = False ]; then VNC_BIND=127.0.0.1; else VNC_BIND=0.0.0.0; fi
install -d -m 700 -o "$SLOT_UID" -g "$SLOT_UID" "$ROOT/slot-key"
py write-key "$ROOT/slot-key/slot-key.txt"
chown "$SLOT_UID:$SLOT_UID" "$ROOT/slot-key/slot-key.txt"
ok "slot key saved in $ROOT/slot-key/slot-key.txt (mode 600, never shown)"

bold "3/6 Foundry v$VERSION and the mirrors"
cd "$ROOT"
[ -d AnimalUprising.git ] || runuser -u "$USER_NAME" -- git init -q --bare -b master AnimalUprising.git
[ -d foundry.git ] || runuser -u "$USER_NAME" -- git init -q --bare -b deploy foundry.git
if [ ! -d foundry/.git ]; then
    rm -rf foundry
    # a full clone (~7 MB): a shallow one cannot be pushed to the foundry.git mirror
    runuser -u "$USER_NAME" -- git -c advice.detachedHead=false clone -q --branch "v$VERSION" "$REPO" foundry \
        || die "cannot download Foundry v$VERSION from GitHub"
else
    runuser -u "$USER_NAME" -- git -C foundry fetch -q "$REPO" "+refs/tags/v$VERSION:refs/tags/v$VERSION" \
        || die "cannot download Foundry v$VERSION from GitHub"
    runuser -u "$USER_NAME" -- git -C foundry -c advice.detachedHead=false checkout -q --force "v$VERSION"
fi
# from now on the workstation pushes its deployed Foundry to foundry.git and this checkout follows
runuser -u "$USER_NAME" -- git -C foundry push -q --force "$ROOT/foundry.git" HEAD:refs/heads/deploy
runuser -u "$USER_NAME" -- git -C foundry remote set-url origin "$ROOT/foundry.git"
chmod -R a+rX AnimalUprising.git foundry.git foundry
ok "Foundry $(runuser -u "$USER_NAME" -- git -C foundry describe --tags --always) in $ROOT/foundry; game mirror $ROOT/AnimalUprising.git"

bold "4/6 ssh access for Foundry"
# The main PC's coordinator registers its own ssh key with the router (v0.162.0), and this host
# reports its ssh host keys there. slot-keys.py does both now and every 5 minutes after (a
# systemd timer), so a new or replaced main PC gets in without reinstalling this slot.
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"
AK="/home/$USER_NAME/.ssh/authorized_keys"
touch "$AK"; chown "$USER_NAME:$USER_NAME" "$AK"; chmod 600 "$AK"
install -m 755 "$ROOT/foundry/contrib/studio-slot/proxmox/slot-keys.py" /usr/local/sbin/foundry-slot-keys
if ! /usr/local/sbin/foundry-slot-keys --router "$(py get router)" --fingerprint "$(py get tls_pinned)"; then
    warn "could not sync the ssh keys with the router now; the timer retries every 5 minutes"
fi
cat > /etc/systemd/system/foundry-slot-keys.service <<'UNIT'
[Unit]
Description=Foundry: sync this Studio slot's ssh keys with the router
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/foundry-slot-keys
UNIT
cat > /etc/systemd/system/foundry-slot-keys.timer <<'UNIT'
[Unit]
Description=Foundry: sync this Studio slot's ssh keys with the router every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
if systemctl daemon-reload >/dev/null 2>&1 && systemctl enable --now foundry-slot-keys.timer >/dev/null 2>&1; then
    ok "key refresh every 5 minutes (foundry-slot-keys.timer)"
else
    warn "could not start foundry-slot-keys.timer: run 'foundry-slot-keys' after adding a main PC"
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
# a trial override from 2026-11-01 (it also added /dev/dri) would add the device twice
OVR="$COMPOSE_DIR/docker-compose.override.yml"
[ -f "$OVR" ] && head -1 "$OVR" | grep -q '^# TRIAL' && rm -f "$OVR"
umask 077
cat > "$ROOT/slot.env" <<EOF
# written by setup-host.sh for $SLOT ($PROJECT) on $(date -u +%FT%TZ); re-run the installer to change it
STUDIO_OPEN=$STUDIO_OPEN
SLOT_CONTAINER=$CONTAINER
SLOT_NOVNC_BIND=$VNC_BIND
SLOT_NOVNC_PORT=$VNC_PORT
SLOT_CPUS=$CPUS
SLOT_RENDER_THREADS=$CPUS
SLOT_GEOMETRY=$SIZE
COMPOSE_FILE=$COMPOSE_FILES
SLOT_MEM=${MEM_G}g
SLOT_MULTIPLAYER=$([ "$MEM_G" -ge 14 ] && echo 1 || echo 0)
FOUNDRY_URL=$(py get router)
FOUNDRY_TLS_FINGERPRINT=$(py get tls_pinned)
FOUNDRY_PROJECT=$PROJECT
FOUNDRY_SLOT=$SLOT
ROJO_NEVER_PLACE_IDS=$NEVER
EOF
umask 022
chown "$USER_NAME:$USER_NAME" "$ROOT/slot.env"; chmod 600 "$ROOT/slot.env"
ln -sf "$ROOT/slot.env" "$COMPOSE_DIR/.env"
ok "$ROOT/slot.env: $CPUS of $NPROC CPUs, ${MEM_G} GB, desktop $SIZE, place ${PLACE:-none}"
ok "Studio draws with $GPU_TXT"
if [ "$BUILD" = 0 ]; then
    rm -f "$ENROLL" "$API"
    echo; echo "--no-build: stopped before the build. Build and start later with:"
    echo "   runuser -l $USER_NAME -c 'cd $COMPOSE_DIR && docker compose up -d --build'"
    exit 0
fi
echo "   building the image: 20-40 minutes the first time (progress: tail -f $ROOT/build.log)"
if ! runuser -l "$USER_NAME" -c "cd '$COMPOSE_DIR' && docker compose up -d --build" >"$ROOT/build.log" 2>&1; then
    tail -20 "$ROOT/build.log" >&2
    die "the build failed (full log: $ROOT/build.log)"
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
rm -f "$ENROLL" "$API"

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

One step left, once: sign Studio in.
  1. $OPEN
  2. Sign in to Roblox in the Studio window; it stays signed in across restarts and rebuilds.

Useful later:
  status      docker exec $CONTAINER slot-secrets --status
  logs        docker logs $CONTAINER
  reinstall   run this installer again with a new code (Access → Machines → New code)
EOF
