#!/bin/bash
# The junction container's start (install/junction/Dockerfile, Distributed Foundry phase e).
#
#   install       once (and again to repair / upgrade: no code needed then): install/workstation.sh
#                 in the container (router address + one-time code from Access → Machines → Add a
#                 workstation); keys go to a mode-600 file in the volume. Then the Kilo role rules
#                 (the coordinator's kilo-config) and the platform checks.
#   studio-enrol  the junction's Studio (studio-main): its one-time code from Access → Machines →
#                 Add a Studio slot `studio-main` traded for its key, written into the volume
#                 where the studio-main container reads it (never printed, never copied by hand)
#   run           (the default) the Foundry Client, headless, as the `foundry` user: it starts and
#                 watches the coordinator, the upgrade supervisor, Kilo and OpenCode
#   doctor        the client's checks, then the platform's
#   shell         a shell as `foundry`, for a look around
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
        # service of docker-compose.yml (`studio-enrol`, not installed by workstation.sh here)
        bash /opt/foundry-install/workstation.sh --root "$ROOT" --skip-tools --no-start \
            --no-studio --secret-store file "$@"
        # the client does not run during `install`, so the role rules come from the coordinator's
        # own generator (what `foundry-client apply-kilo-config` runs) for each project here
        cd "$ROOT/app"
        for p in $(as_foundry "$ROOT/venv/bin/python" -c 'from foundry_client import workstation as W; print(" ".join(W.enrolled()["projects"]))'); do
            kj=$(as_foundry "$ROOT/venv/bin/python" -c "from foundry_client import config, components as C; print(C.kilo_config_path(config.load(), '$p'))")
            if as_foundry "$ROOT/venv/bin/python" -m foundry_client coordinator --state "$ROOT/state" \
                    --profiles "$ROOT/app/foundry_coordinator/projects" kilo-config "$p" \
                    --path "$kj" --apply >/dev/null 2>&1; then
                echo "   ✓ Kilo's role rules written into $kj"
            else
                echo "   ! kilo-config for $p failed: run 'apply-kilo-config' once the junction runs" >&2
            fi
        done
        exec runuser -u "$USER_NAME" -- env HOME="$(home_of)" "$ROOT/venv/bin/python" -m foundry_client doctor --platform
        ;;
    studio-enrol)
        # --router URL [--fingerprint SHA256] [--code-stdin]
        router="" ; fp="" ; stdin=0
        while [ $# -gt 0 ]; do
            case "$1" in
                --router) router="$2"; shift 2 ;;
                --fingerprint) fp="$2"; shift 2 ;;
                --code-stdin) stdin=1; shift ;;
                *) echo "studio-enrol: unknown option $1" >&2; exit 2 ;;
            esac
        done
        if [ -z "$router" ] && [ -f "$ROOT/studio-main/router.txt" ]; then router="$(head -1 "$ROOT/studio-main/router.txt")"; fi
        if [ -z "$router" ]; then read -r -p "Foundry router address (e.g. http://192.168.0.10:11435): " router; fi
        [ -n "$router" ] || { echo "no router address (--router http://<router>:11435)" >&2; exit 2; }
        case "$router" in http://*|https://*) ;; *) router="http://$router" ;; esac
        if [ "$stdin" = 1 ]; then read -r code || true
        else read -r -s -p "One-time code for studio-main (Access → Machines → Add a Studio slot): " code; echo; fi
        [ -n "$code" ] || { echo "no code" >&2; exit 2; }
        install -d -m 755 "$ROOT/studio-main"
        # uid 1000 = the slot image's `studio` user, which reads the key; values never printed
        CODE="$code" ROUTER="$router" FP="$fp" OUT="$ROOT/studio-main" python3 - <<'PY'
import hashlib, http.client, json, os, socket, ssl, sys, urllib.parse
router, out, fp = os.environ["ROUTER"].rstrip("/"), os.environ["OUT"], os.environ.get("FP", "")
u = urllib.parse.urlsplit(router)
try:
    if u.scheme == "https":
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30, context=ctx)
        c.connect()
        got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
        if not fp or fp.replace(":", "").upper() != got:
            sys.exit("✗ an https router needs --fingerprint (Access → Secrets → Transport): stopped")
    else:
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    c.request("POST", "/v1/foundry/enroll",
              json.dumps({"code": os.environ["CODE"], "hostname": socket.gethostname()}),
              {"Content-Type": "application/json", "User-Agent": "foundry-junction/1"})
    r = c.getresponse(); data = json.loads(r.read() or b"{}")
except (OSError, ValueError) as e:
    sys.exit(f"✗ cannot reach the router at {router} ({e})")
if r.status != 200:
    e = data.get("error") or {}
    sys.exit("✗ the router refused the code: " + ((e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {r.status}"))
if data.get("kind") != "slot" or data.get("slot") != "studio-main":
    sys.exit(f"✗ this code is for {data.get('kind')} {data.get('slot') or data.get('machine')}, "
             "not the Studio slot studio-main")
def private(name, text):
    p = os.path.join(out, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, (text + "\n").encode()); os.close(fd)
    os.chown(p, 1000, 1000)
private("slot-key.txt", data["key"])
private("router.txt", router)
private("fingerprint.txt", fp)
print(f"   ✓ studio-main enrolled ({data['project']}); key in {out}/slot-key.txt (mode 600)")
PY
        echo "   Start it: docker compose --profile studio up -d (then sign Studio in once over noVNC)"
        ;;
    run)
        if [ ! -x "$ROOT/venv/bin/python" ] || [ ! -d "$ROOT/app/.git" ]; then
            echo "This junction is not installed yet. Run once:" >&2
            echo "   docker compose run --rm junction install --router http://<router>:11435" >&2
            echo "(it asks for a one-time code from Access → Machines → Add a workstation)." >&2
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
        echo "unknown command $cmd (install, studio-enrol, run, doctor, shell)" >&2
        exit 2
        ;;
esac
