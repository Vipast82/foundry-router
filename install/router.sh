#!/bin/bash
# Foundry router installer: one run turns a Linux machine with Docker (or a fresh Debian or
# Ubuntu one, where it installs Docker) into a running Foundry router.
#
# On the new router machine, as root:
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/main/install/router.sh
#   bash router.sh
#
# It asks for:
#   - the install folder (default /opt/foundry-router);
#   - the web UI port (default 11435);
#   - whether to turn on the HTTPS listener for secrets (port 11436);
#   - a backup file to restore, if you are moving a router (a .fbak from Logs -> Data);
# then installs Docker if needed, downloads the Foundry release, builds and starts the router,
# and prints the address. Open it: a new router shows its setup wizard; a restored one is
# back as it was (unlock its secret store with its passphrase).
#
# Layout: <dir>/app (the release, a git checkout), <dir>/config (config.yaml,
# provider-keys.env), <dir>/data (databases, secret store, HTTPS certificate).
#
# Options:
#   --dir DIR            install folder (default /opt/foundry-router)
#   --version X.Y.Z      release to install (default: the newest release)
#   --port N             web UI / API port (default 11435)
#   --https | --no-https turn the HTTPS listener for secrets on or off
#   --restore FILE       restore this backup (asks for its passphrase)
#   --upgrade            upgrade an installed router to --version (default: newest)
#   --yes                accept the defaults instead of asking
#   --no-start           stop before starting the router (for testing)
# Re-running is safe: config and data are kept.
set -euo pipefail

REPO="${FOUNDRY_REPO:-https://github.com/Vipast82/foundry-router.git}"   # FOUNDRY_REPO: a fork or a test copy
DIR=/opt/foundry-router ; VERSION="" ; PORT="" ; HTTPS="" ; RESTORE="" ; UPGRADE=0
YES=0 ; START=1 ; DIR_SET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="$2"; DIR_SET=1; shift 2 ;;
        --version) VERSION="${2#v}"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --https) HTTPS=1; shift ;;
        --no-https) HTTPS=0; shift ;;
        --restore) RESTORE="$2"; shift 2 ;;
        --upgrade) UPGRADE=1; shift ;;
        --yes|-y) YES=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
# questions come from the terminal, so `curl ... | bash` works too
TTY=/dev/tty
HAVE_TTY=0; if (exec < "$TTY") 2>/dev/null; then HAVE_TTY=1; fi   # opens, not just exists
ask() {   # ask VAR "question" default
    local var="$1" q="$2" def="$3" ans=""
    if [ "$YES" = 1 ] || [ "$HAVE_TTY" = 0 ]; then printf -v "$var" '%s' "$def"; return; fi
    read -r -p "$q${def:+ [$def]}: " ans < "$TTY" || true
    printf -v "$var" '%s' "${ans:-$def}"
}
yesno() { # yesno "question" default(y|n) -> 0 for yes
    local ans=""
    if [ "$YES" = 1 ] || [ "$HAVE_TTY" = 0 ]; then [ "$2" = y ]; return; fi
    read -r -p "$1 [$([ "$2" = y ] && echo Y/n || echo y/N)]: " ans < "$TTY" || true
    ans="${ans:-$2}"; case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}
[ "$(id -u)" = 0 ] || die "run this as root (sudo bash router.sh)"

echo
echo "Foundry router installer"
echo "------------------------"
[ "$DIR_SET" = 1 ] || ask DIR "Install folder" "$DIR"
APP="$DIR/app" ; CONF="$DIR/config" ; DATA="$DIR/data"
if [ -d "$APP/.git" ] && [ "$UPGRADE" = 0 ]; then
    echo "   A router is already installed in $DIR."
    yesno "   Upgrade it to the newest (or --version) release?" y && UPGRADE=1 || true
fi

# -- 1. Docker ------------------------------------------------------------------------------
bold "1/5 Docker and git"
if ! command -v docker >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || die "install Docker (with compose) and git first, then run this again"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl git docker.io >/dev/null
fi
if ! docker compose version >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || die "Docker compose (v2) is missing: install it, then run this again"
    apt-get install -y -qq docker-compose >/dev/null 2>&1 || apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 \
        || apt-get install -y -qq docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
if docker info >/dev/null 2>&1; then
    ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short 2>/dev/null)"
elif [ "$START" = 0 ]; then
    warn "Docker is not running here (--no-start: carrying on)"
else
    die "Docker does not start (in a Proxmox LXC it needs features: nesting=1,keyctl=1)"
fi

# -- 2. the release -------------------------------------------------------------------------
bold "2/5 Foundry release"
if [ -z "$VERSION" ]; then
    VERSION=$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null | sed 's#.*refs/tags/v##' \
              | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
    [ -n "$VERSION" ] || die "cannot read the releases from $REPO: is there internet access?"
fi
if [ -d "$APP/.git" ]; then
    OLD=$(git -C "$APP" describe --tags --always 2>/dev/null || echo "?")
    git -C "$APP" fetch -q -f --depth 1 origin "refs/tags/v$VERSION:refs/tags/v$VERSION" \
        || die "release v$VERSION not found"
    git -C "$APP" -c advice.detachedHead=false checkout -q "v$VERSION"
    ok "upgraded $OLD -> v$VERSION"
else
    install -d -m 755 "$DIR"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "v$VERSION" "$REPO" "$APP" \
        || die "cannot download release v$VERSION"
    ok "v$VERSION in $APP"
fi
INSTALLED=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP/foundry_router/__init__.py")

# -- 3. settings ------------------------------------------------------------------------------
bold "3/5 settings"
install -d -m 750 -o 1000 -g 1000 "$CONF" "$DATA"
ENVF="$APP/.env"
getenv() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1 || true; }
setenv() {
    touch "$ENVF"; chmod 600 "$ENVF"
    if grep -q "^$1=" "$ENVF"; then sed -i "s#^$1=.*#$1=$2#" "$ENVF"; else echo "$1=$2" >> "$ENVF"; fi
}
[ -n "$PORT" ] || PORT=$(getenv FOUNDRY_PORT)
[ -n "$PORT" ] || ask PORT "Web UI port" 11435
case "$PORT" in ''|*[!0-9]*) die "the port must be a number" ;; esac
TLS_PORT=$(getenv FOUNDRY_TLS_PORT); TLS_PORT="${TLS_PORT:-11436}"
setenv FOUNDRY_PORT "$PORT"
setenv FOUNDRY_TLS_PORT "$TLS_PORT"
setenv FOUNDRY_CONFIG_DIR "$CONF"
setenv FOUNDRY_DATA_DIR_HOST "$DATA"
FRESH=0; [ -f "$CONF/config.yaml" ] || FRESH=1
if [ -z "$HTTPS" ] && [ "$FRESH" = 1 ] && [ -z "$RESTORE" ]; then
    if yesno "Turn on the HTTPS listener for secrets (port $TLS_PORT, a self-signed certificate)?" n; then HTTPS=1; else HTTPS=0; fi
fi
ok "port $PORT, config in $CONF, data in $DATA"

# -- 4. build (and a restore) -------------------------------------------------------------------
bold "4/5 build"
if [ -z "$RESTORE" ] && [ "$FRESH" = 1 ] && [ "$UPGRADE" = 0 ] && [ "$HAVE_TTY" = 1 ] && [ "$YES" = 0 ]; then
    ask RESTORE "Moving a router? Path of its backup (.fbak) to restore, or Enter for a new router" ""
fi
if [ "$START" = 0 ]; then
    warn "--no-start: not building or starting"
    exit 0
fi
cd "$APP"
echo "   building the image (a few minutes the first time)…"
docker compose build -q >"$DIR/build.log" 2>&1 || { tail -20 "$DIR/build.log"; die "the build failed (log: $DIR/build.log)"; }
ok "image built"
if [ -n "$RESTORE" ]; then
    [ -f "$RESTORE" ] || die "no such file: $RESTORE"
    docker compose stop -t 30 >/dev/null 2>&1 || true
    install -d -m 750 -o 1000 -g 1000 "$DATA/restore"
    install -m 600 -o 1000 -g 1000 "$RESTORE" "$DATA/restore/upload.fbak"
    PASS=""
    if [ "$HAVE_TTY" = 1 ]; then read -r -s -p "   Backup passphrase (typing is hidden): " PASS < "$TTY" || true; echo
    else read -r PASS || true; fi   # no terminal: the first line on stdin
    [ -n "$PASS" ] || die "no backup passphrase"
    printf '%s\n' "$PASS" | docker compose run --rm -T --no-deps foundry-router \
        python -m foundry_router.backup restore /data/restore/upload.fbak \
        || { rm -f "$DATA/restore/upload.fbak"; die "the restore was refused (above)"; }
    PASS=""
    rm -f "$DATA/restore/upload.fbak"
    ok "restore staged: it is put in place as the router starts"
fi

# -- 5. start -------------------------------------------------------------------------------------
bold "5/5 start"
docker compose up -d >/dev/null 2>&1 || die "docker compose up failed: cd $APP && docker compose up"
URL="http://127.0.0.1:$PORT"
wait_up() {
    for _ in $(seq 1 90); do
        V=$(curl -fsS --max-time 3 "$URL/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
        [ -n "$V" ] && return 0
        sleep 2
    done
    return 1
}
wait_up || die "the router did not answer within 3 minutes: docker logs foundry-router"
ok "router v$V answers"
if [ "$HTTPS" = 1 ]; then
    if curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
            -d '{"https_enabled": true}' "$URL/admin/api/secrets/settings" >/dev/null 2>&1; then
        docker compose restart >/dev/null 2>&1
        wait_up || die "the router did not come back after turning HTTPS on"
        ok "HTTPS listener on port $TLS_PORT"
    else
        warn "could not turn HTTPS on here (the web UI may already need an admin key): Access -> Secrets -> Transport"
    fi
fi
FP=$(curl -fsS --max-time 5 "$URL/admin/api/secrets" 2>/dev/null \
     | sed -n 's/.*"fingerprint": *"\([0-9A-F:]*\)".*/\1/p' || true)

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
IP="${IP:-$(hostname)}"
echo
printf '\033[1m\033[32mThe Foundry router is running.\033[0m\n'
echo
echo "   Open:    http://$IP:$PORT/ui"
if [ -n "$RESTORE" ]; then
    echo "            It is restored. Unlock its secret store on Access -> Secrets with the old router's passphrase."
elif [ "$FRESH" = 1 ]; then
    echo "            The setup wizard opens: admin key, secret store, backends, personas, game, machines."
fi
[ -n "$FP" ] && echo "   HTTPS:   https://$IP:$TLS_PORT  certificate SHA-256 $FP"
echo "   Version: v${V:-$INSTALLED}   Folder: $DIR"
echo "   Upgrade: bash $APP/install/router.sh --upgrade    Logs: docker logs -f foundry-router"
echo
echo "   Only expose this on your LAN: the API has no internet-grade protection."
