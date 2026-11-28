# The Foundry junction (Distributed Foundry phase e, docs/DISTRIBUTED_FOUNDRY.md §2, §9): the main
# workstation in a container. The coordinator, the Foundry Client, both coding clients (Kilo and
# OpenCode), git, the game's toolchain (rokit: lune, selene, stylua, wally), PowerShell 7 for the
# gate's host half and Blender: the same tools install/workstation.sh puts on a Linux PC, at the
# same pinned versions. The junction's Studio is the slot image beside it (docker-compose.yml,
# `studio-main`, [studio] main_slot).
#
# Build (from the repo root):  docker build -f install/junction/Dockerfile -t foundry/junction .
# First run: see install/junction/README.md (enrolment needs a one-time code from the router).
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client python3 python3-venv python3-pip xz-utils unzip \
        procps docker-cli tini \
    && rm -rf /var/lib/apt/lists/*
COPY install/workstation.sh install/pins.env /opt/foundry-install/
# the packages, the `foundry` user and every tool, at install/pins.env's pins (no router needed)
RUN bash /opt/foundry-install/workstation.sh --tools-only \
    && kilo --version && opencode --version && pwsh -NoLogo -NoProfile -Command 'exit 0'
COPY install/junction/junction-entrypoint.sh /opt/foundry-install/junction-entrypoint.sh
RUN chmod 755 /opt/foundry-install/junction-entrypoint.sh
# everything a junction keeps (the release checkout, the game, the coordinator's state, the
# client's settings and secrets file) lives in this volume
VOLUME ["/srv/foundry"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/foundry-install/junction-entrypoint.sh"]
CMD ["run"]
