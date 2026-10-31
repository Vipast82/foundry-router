#!/bin/bash
# Prepare a Proxmox LXC (Debian 13, unprivileged, nesting+keyctl; see lxc.conf.example) to run a
# Studio slot that the workstation's Foundry drives over ssh. Run as root INSIDE the LXC (the
# Proxmox console). Fetch it from the release you deploy, e.g.
#   apt-get update && apt-get install -y curl
#   curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/v0.159.1/contrib/studio-slot/proxmox/setup-host.sh
#   bash setup-host.sh "<the workstation's ssh public key, one line: type %USERPROFILE%\.ssh\id_ed25519.pub>"
# Idempotent: re-running keeps what exists. It never handles the Open Cloud key: copy that
# yourself (step 4 below).
set -euo pipefail
PUBKEY="${1:-}"
ROOT=/srv/studio-slot
USER_NAME=studio
SLOT_UID=1000            # the image's `studio` user: the read-only mounts must be readable by it

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssh-server docker.io >/dev/null
if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y -qq docker-compose >/dev/null 2>&1 || apt-get install -y -qq docker-compose-v2 >/dev/null
fi
docker compose version
systemctl enable --now docker ssh >/dev/null 2>&1 || true

echo "== user $USER_NAME (uid $SLOT_UID, docker group)"
if ! id "$USER_NAME" >/dev/null 2>&1; then
    if getent passwd "$SLOT_UID" >/dev/null; then
        useradd -m -s /bin/bash "$USER_NAME"
        echo "note: uid $SLOT_UID is taken; check that the slot can read $ROOT (it is world-readable)"
    else
        useradd -m -s /bin/bash -u "$SLOT_UID" "$USER_NAME"
    fi
fi
usermod -aG docker "$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"
if [ -n "$PUBKEY" ]; then
    AK="/home/$USER_NAME/.ssh/authorized_keys"
    touch "$AK"
    grep -qxF "$PUBKEY" "$AK" || echo "$PUBKEY" >> "$AK"
    chown "$USER_NAME:$USER_NAME" "$AK"; chmod 600 "$AK"
fi

echo "== $ROOT (bare mirrors Foundry pushes to, the Foundry checkout, secrets)"
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$ROOT"
cd "$ROOT"
[ -d AnimalUprising.git ] || sudo -u "$USER_NAME" git init -q --bare -b master AnimalUprising.git
[ -d foundry.git ] || sudo -u "$USER_NAME" git init -q --bare -b deploy foundry.git
[ -d foundry ] || sudo -u "$USER_NAME" git clone -q foundry.git foundry 2>/dev/null || true
install -d -m 700 -o "$SLOT_UID" -g "$SLOT_UID" slot-key
chmod -R a+rX AnimalUprising.git foundry.git foundry

cat <<EOF

== next steps (from the workstation unless noted; <lxc> = this LXC's address)
1. ssh works without a password. The FIRST connection accepts the LXC's host key; Foundry
   itself only ever connects with BatchMode, which refuses unknown hosts:
     ssh -o StrictHostKeyChecking=accept-new $USER_NAME@<lxc> docker ps
2. seed the mirrors (once; afterwards Foundry pushes before every gate run):
     git -C C:\\Roblox\\AnimalUprising push ssh://$USER_NAME@<lxc>$ROOT/AnimalUprising.git master
     git -C C:\\Users\\shock\\foundry-deploy push ssh://$USER_NAME@<lxc>$ROOT/foundry.git HEAD:refs/heads/deploy
     ssh $USER_NAME@<lxc> "git -C $ROOT/foundry fetch -q origin deploy && git -C $ROOT/foundry checkout -q --detach FETCH_HEAD"
3. the slot's own place: a private experience "Animal Uprising [SLOT-n]"; put its link in
     $ROOT/foundry/contrib/studio-slot/proxmox/.env (copy .env.example)
4. the slot's key: on the router, Access → Clients & Keys → preset "slot" (scope secrets, project
   animaluprising, only slots = this slot's id). Copy the key it shows once into a file (you do this;
   nothing else reads or prints it):
     scp <key file> $USER_NAME@<lxc>:$ROOT/slot-key/slot-key.txt
     then on the LXC: chmod 600 $ROOT/slot-key/slot-key.txt; chown $SLOT_UID $ROOT/slot-key/slot-key.txt
   The Open Cloud key itself lives on the router (Setup → Secrets); the slot pulls it at start.
5. build and start (on the LXC, as $USER_NAME):
     cd $ROOT/foundry/contrib/studio-slot/proxmox && docker compose up -d --build
6. sign Studio in once: ssh -L 6080:localhost:6080 $USER_NAME@<lxc>, open http://localhost:6080/vnc.html
7. add the slot on the router: Automation → Settings → Studio slots (slot-n with container, host,
   au_push_url, foundry_push_url, foundry_host_dir, place_id, universe_id), deploy, and start a track
   with --slot slot-n
EOF
