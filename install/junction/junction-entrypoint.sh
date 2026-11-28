#!/bin/bash
# The junction container's start (install/junction/Dockerfile, Distributed Foundry phase e).
#
#   install   once: install/workstation.sh in the container (router address + one-time code from
#             Access → Machines → Add a workstation); keys go to a mode-600 file in the volume
#   run       (the default) the Foundry Client, headless, as the `foundry` user: it starts and
#             watches the coordinator, the upgrade supervisor, Kilo and OpenCode
#   doctor    the client's checks, then the platform's
#   shell     a shell as `foundry`, for a look around
set -euo pipefail
ROOT=/srv/foundry
USER_NAME=foundry
cmd="${1:-run}"
shift || true

home_of() { getent passwd "$USER_NAME" | cut -d: -f6; }
as_foundry() { runuser -u "$USER_NAME" -- env HOME="$(home_of)" "$@"; }

# The foundry user's settings (~/.config: client.toml, the secrets file) and ssh keys (~/.ssh: the
# game's repo key) live in the volume, so a new container keeps them: the home folders are links.
persist_home() {
    local home; home="$(home_of)"
    for d in .config .ssh; do
        install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$ROOT/home/$d"
        if [ -d "$home/$d" ] && [ ! -L "$home/$d" ]; then
            cp -a "$home/$d/." "$ROOT/home/$d/" 2>/dev/null || true
            rm -rf "${home:?}/$d"
        fi
        [ -L "$home/$d" ] || ln -s "$ROOT/home/$d" "$home/$d"
    done
    chown -h "$USER_NAME:$USER_NAME" "$home/.config" "$home/.ssh"
}
install -d -m 755 "$ROOT"
persist_home

case "$cmd" in
    install)
        # in a container: no systemd service (the container itself is the service), the secret
        # store is a mode-600 file in the volume, and the junction's Studio is the studio-main
        # service of docker-compose.yml (not installed by workstation.sh here)
        exec bash /opt/foundry-install/workstation.sh --root "$ROOT" --skip-tools --no-start \
            --no-studio --secret-store file "$@"
        ;;
    run)
        if [ ! -x "$ROOT/venv/bin/python" ] || [ ! -d "$ROOT/app/.git" ]; then
            echo "This junction is not installed yet. Run once:" >&2
            echo "   docker compose run --rm junction install" >&2
            echo "(it asks for the router address and a one-time code from Access → Machines →" >&2
            echo " Add a workstation)." >&2
            exit 2
        fi
        cd "$ROOT/app"
        exec runuser -u "$USER_NAME" -- env HOME="$(home_of)" \
            "$ROOT/venv/bin/python" -m foundry_client run --headless
        ;;
    doctor)
        cd "$ROOT/app"
        as_foundry "$ROOT/venv/bin/python" -m foundry_client doctor || true
        exec runuser -u "$USER_NAME" -- "$ROOT/venv/bin/python" -m foundry_client doctor --platform
        ;;
    shell)
        exec runuser -u "$USER_NAME" -- env HOME="$(home_of)" bash -l
        ;;
    *)
        echo "unknown command $cmd (install, run, doctor, shell)" >&2
        exit 2
        ;;
esac
