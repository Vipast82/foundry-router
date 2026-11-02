#!/bin/bash
# Create a Foundry main workstation on a Proxmox host in one go (LINUX_WORKSTATION_PLAN W3, D-L2):
# makes the LXC (Debian 13, unprivileged, Docker-ready for the Studio container) and runs
# install/workstation.sh inside it, so it ends enrolled and running.
#
# Before you run it: Foundry router → Access → Machines → "Add a workstation" (give it this LXC's
# IP if it gets a Studio for main sprints). That shows a one-time enrollment code.
#
# Run as root in the Proxmox HOST shell (Datacenter → <node> → Shell):
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/install/create-workstation-lxc.sh
#   bash create-workstation-lxc.sh
#
# Sizes (D-L4): 8 cores, 32 GB memory (Studio + Wine is the heavy part; 2-player playtests need
# ~14 GB on their own), 120 GB disk. With 16 GB, multiplayer playtest rows stay MANUAL.
set -euo pipefail

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local v; read -r -p "$1 [$2]: " v; printf '%s' "${v:-$2}"; }
[ "$(id -u)" = 0 ] || die "run this as root on the Proxmox host"
command -v pct >/dev/null || die "this is not a Proxmox host (no pct): run it in the Proxmox host shell"

echo
echo "Foundry: create a main workstation (Proxmox LXC)"
echo "-------------------------------------------------"
NEXT=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
CTID=$(ask "Container id" "$NEXT")
pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists"
NAME=$(ask "Name" "foundry-workstation-$CTID")
while :; do
    IP=$(ask "Static IP with prefix (the IP you gave the workstation on Access → Machines)" "192.168.0.120/24")
    [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] && break
    echo "   like 192.168.0.120/24"
done
GW_DEF=$(ip route | awk '/^default/{print $3; exit}')
GW=$(ask "Gateway" "${GW_DEF:-192.168.0.1}")
BR=$(ask "Network bridge" "vmbr0")
ST_DEF=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
STORAGE=$(ask "Storage for the disk" "${ST_DEF:-local-lvm}")
DISK=$(ask "Disk size, GB (game + tracks ~30 GB, Studio image ~10 GB, Blender + library ~10 GB)" "120")
MEM=$(ask "Memory, MB (32 GB: Studio + Wine + a 2-player playtest)" "32768")
CORES=$(ask "CPU cores" "8")
IGPU=n
if [ -e /dev/dri/renderD128 ]; then IGPU=$(ask "Pass the Intel iGPU in (Studio's view and Blender's viewport)? y/n" "n"); fi
ROUTER=$(ask "Foundry router address" "http://192.168.0.112:11435")
ROUTER="${ROUTER%/}"; case "$ROUTER" in http://*|https://*) ;; *) ROUTER="http://$ROUTER" ;; esac
read -r -s -p "Enrollment code from Access → Machines (typing is hidden): " CODE; echo
[ -n "$CODE" ] || die "no enrollment code"

bold "checking the router"
VERSION=$(curl -fsS -m 10 -k "$ROUTER/health" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])") \
    || die "cannot reach the router at $ROUTER from this host"
ok "router v$VERSION"
FP=""
if [[ "$ROUTER" == https://* ]]; then
    hp="${ROUTER#https://}"; hp="${hp%%/*}"
    FP=$(echo | openssl s_client -connect "$hp" 2>/dev/null | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2)
    echo "   router certificate SHA-256: $FP"
    [ "$(ask "Same as Access → Secrets → Transport shows? y/n" "n")" = y ] || die "stopped: certificate not confirmed"
fi
INST=/tmp/foundry-workstation.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/workstation.sh" ]; then cp "$HERE/workstation.sh" "$INST"
else
    curl -fsSLo "$INST" "https://raw.githubusercontent.com/Vipast82/foundry-router/v$VERSION/install/workstation.sh" \
        || die "cannot download workstation.sh for v$VERSION"
fi
ok "workstation installer for v$VERSION"

bold "Debian 13 template"
pveam update >/dev/null 2>&1 || true
TPL=$(pveam available --section system 2>/dev/null | awk '/debian-13-standard/{print $2}' | sort -V | tail -1)
[ -n "$TPL" ] || die "no debian-13-standard template in 'pveam available'"
TPL_ST=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
TPL_ST="${TPL_ST:-local}"
if ! pveam list "$TPL_ST" 2>/dev/null | grep -q "$TPL"; then
    echo "   downloading $TPL to $TPL_ST ..."
    pveam download "$TPL_ST" "$TPL" >/dev/null || die "template download failed"
fi
ok "$TPL_ST:vztmpl/$TPL"

bold "creating container $CTID ($NAME, $IP)"
pct create "$CTID" "$TPL_ST:vztmpl/$TPL" --hostname "$NAME" --ostype debian \
    --unprivileged 1 --features nesting=1,keyctl=1 \
    --cores "$CORES" --memory "$MEM" --swap 8192 \
    --rootfs "$STORAGE:$DISK" \
    --net0 "name=eth0,bridge=$BR,ip=$IP,gw=$GW" --onboot 1 >/dev/null
if [ "$IGPU" = y ]; then
    cat >> "/etc/pve/lxc/$CTID.conf" <<EOF
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
EOF
fi
pct start "$CTID"
for _ in $(seq 1 30); do
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 \
    || die "the container has no network (check IP $IP, gateway $GW, bridge $BR); remove it with: pct destroy $CTID"
ok "container $CTID is up"

bold "running the workstation installer inside it"
pct push "$CTID" "$INST" /root/workstation.sh
args=(--router "$ROUTER" --code-stdin)
[ -n "$FP" ] && args+=(--fingerprint "$FP")
printf '%s\n' "$CODE" | FOUNDRY_SETUP_MATCHED=1 pct exec "$CTID" -- env FOUNDRY_SETUP_MATCHED=1 bash /root/workstation.sh "${args[@]}"
CODE=""
echo
echo "Container $CTID ($NAME) is the workstation. Proxmox: it starts at boot (onboot=1)."
echo "To bring the work over from the old one: export there (foundry-client workstation export),"
echo "copy the file in (pct push $CTID FILE /root/move.fwx), then:"
echo "   pct exec $CTID -- bash /root/workstation.sh --import-only /root/move.fwx"
