# Studio slot: Roblox Studio (through Vinegar + Wine) on a headless Linux desktop you
# reach in a browser (noVNC). Proof of concept for Proxmox LXC slots on the mini PCs.
#
# Build:  docker build -t foundry/studio-slot:poc contrib/studio-slot
# Run:    see README.md (docker compose up -d in this folder)

# ---- build Vinegar v1.9.4 from its release tarball (checksum verified) -------------
FROM golang:1.26-trixie AS vinegar
ARG VINEGAR_VERSION=v1.9.4
ARG VINEGAR_SHA256=b71f1868051ec99d05fb0e138b04939e2704f89ecc6f9ad3aa0e031b05bf6568
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils make g++ pkg-config libvulkan-dev \
        libglib2.0-dev-bin gettext \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSLo vinegar.tar.xz \
        "https://github.com/vinegarhq/vinegar/releases/download/${VINEGAR_VERSION}/vinegar-${VINEGAR_VERSION}.tar.xz" \
    && echo "${VINEGAR_SHA256}  vinegar.tar.xz" | sha256sum -c - \
    && tar -xJf vinegar.tar.xz --strip-components=1 \
    && make && make DESTDIR=/out install

# ---- runtime: Debian 13 desktop + Vinegar ------------------------------------------
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 DISPLAY=:1 \
    XDG_RUNTIME_DIR=/tmp/runtime-studio
RUN dpkg --add-architecture i386 && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils unzip procps dbus dbus-x11 \
        tigervnc-standalone-server tigervnc-tools novnc websockify openbox xterm \
        libgtk-4-1 libadwaita-1-0 gir1.2-adw-1 \
        libgl1-mesa-dri libglx-mesa0 libegl1 mesa-vulkan-drivers libvulkan1 vulkan-tools \
        mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 libvulkan1:i386 \
        libfreetype6 libfontconfig1 fonts-dejavu-core fonts-liberation \
        libx11-6 libxcomposite1 libxcursor1 libxi6 libxrandr2 libxinerama1 libxkbcommon0 \
        libgnutls30 libpulse0 libxkbregistry0 \
        firefox-esr xdg-utils desktop-file-utils shared-mime-info \
        pci.ids x11-xserver-utils \
        wine wine64 \
        git xdotool x11-utils imagemagick libicu76 \
        python3 python3-pil python3-numpy ffmpeg \
        libxi6 libxxf86vm1 libxfixes3 libxrender1 libsm6 \
    && rm -rf /var/lib/apt/lists/*
# Blender 4.5 LTS, the same release as the build PC (docs/ENVIRONMENT.md: 4.5.12), for AU's
# asset pipeline (tools/blender/*.py run with `blender -b`); checksum from download.blender.org
ARG BLENDER_VERSION=4.5.12
ARG BLENDER_SHA256=95e3a2dfedba3bd32ca54fc355eac6b15a11986954ccb02815a07535d0120a25
RUN cd /tmp && curl -fsSLo blender.tar.xz \
        "https://download.blender.org/release/Blender${BLENDER_VERSION%.*}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
    && echo "${BLENDER_SHA256}  blender.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/blender && tar -xJf blender.tar.xz -C /opt/blender --strip-components=1 \
    && ln -sf /opt/blender/blender /usr/local/bin/blender && rm -f blender.tar.xz \
    && blender -b --version | head -1
# PowerShell 7 (AU's tools/gate.ps1, the gate's host half) and rokit (AU's toolchain
# manager: lune, selene, stylua, wally from rokit.toml); digests from the GitHub releases
ARG PWSH_VERSION=7.6.9
ARG PWSH_SHA256=1da6d6d6035d651f20e70ddee7c4c15ac6ab8b9fb40ee92240d91263f28a884b
ARG ROKIT_VERSION=1.2.0
ARG ROKIT_SHA256=951a7f3ec3d2a5e021fd1867d32f69f010ee4c2927f644b759578afc59c65fc0
RUN cd /tmp && curl -fsSLo pwsh.tgz \
        "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-x64.tar.gz" \
    && echo "${PWSH_SHA256}  pwsh.tgz" | sha256sum -c - \
    && mkdir -p /opt/microsoft/powershell/7 && tar -xzf pwsh.tgz -C /opt/microsoft/powershell/7 \
    && chmod 755 /opt/microsoft/powershell/7/pwsh && ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh \
    && curl -fsSLo rokit.zip \
        "https://github.com/rojo-rbx/rokit/releases/download/v${ROKIT_VERSION}/rokit-${ROKIT_VERSION}-linux-x86_64.zip" \
    && echo "${ROKIT_SHA256}  rokit.zip" | sha256sum -c - \
    && unzip -o rokit.zip -d /usr/local/bin && chmod 755 /usr/local/bin/rokit \
    && rm -f pwsh.tgz rokit.zip && pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
# Node 22 (Debian's 20 lacks Promise.withResolvers, which robloxstudio-mcp needs),
# checked against nodejs.org's SHASUMS256
ARG NODE_VERSION=22.21.0
RUN cd /tmp && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " node-v${NODE_VERSION}-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
    && rm -f /tmp/node-* /tmp/SHASUMS256.txt && node -v
# the Studio MCP server (same one the Foundry gate uses) and its own Studio plugin
ARG MCP_VERSION=3.4.2
RUN npm install -g "@chrrxs/robloxstudio-mcp@${MCP_VERSION}" \
    && mkdir -p /opt/studio-slot/plugins \
    && cp "$(npm root -g)/@chrrxs/robloxstudio-mcp/studio-plugin/MCPPlugin.rbxmx" /opt/studio-slot/plugins/
COPY --from=vinegar /out/ /
# Studio sign-in: the roblox-studio-auth link from the browser opens Vinegar
RUN update-desktop-database /usr/share/applications \
    && update-mime-database /usr/share/mime >/dev/null 2>&1 || true
# Rojo 7.7.1 + its matching Studio plugin. 7.6.1 can't read AU's newer .rbxm models
# ("MeshPart.Tags should be SharedString"). 7.7.x can miss new files / project-file edits
# while serving (rojo-rbx/rojo#1300): restart `rojo serve` after adding files.
ARG ROJO_VERSION=7.7.1
RUN curl -fsSLo /tmp/rojo.zip \
        "https://github.com/rojo-rbx/rojo/releases/download/v${ROJO_VERSION}/rojo-${ROJO_VERSION}-linux-x86_64.zip" \
    && unzip -o /tmp/rojo.zip -d /usr/local/bin && chmod 755 /usr/local/bin/rojo \
    && rm /tmp/rojo.zip && rojo --version \
    && curl -fsSLo /opt/studio-slot/plugins/Rojo.rbxm \
        "https://github.com/rojo-rbx/rojo/releases/download/v${ROJO_VERSION}/Rojo.rbxm"
RUN useradd -m -s /bin/bash studio && mkdir -p /tmp/runtime-studio \
    && chown studio:studio /tmp/runtime-studio && chmod 700 /tmp/runtime-studio
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY mcp-smoke.js studio-plugin-settings.js /opt/studio-slot/
COPY vinegar-config.toml /etc/studio-slot/vinegar-config.toml
COPY studio-auth-handler.sh /usr/local/bin/studio-auth-handler
COPY studio-mcp-official.sh /usr/local/bin/studio-mcp-official
COPY studio-reopen.sh /usr/local/bin/studio-reopen
COPY slot-checkout.sh /usr/local/bin/slot-checkout
COPY studio-auth.desktop /usr/share/applications/studio-auth.desktop
COPY openbox-studio.xml /etc/studio-slot/openbox-studio.xml
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/studio-auth-handler \
        /usr/local/bin/studio-mcp-official /usr/local/bin/studio-reopen /usr/local/bin/slot-checkout \
    && update-desktop-database /usr/share/applications \
    && sed -i '/<\/applications>/e cat /etc/studio-slot/openbox-studio.xml' /etc/xdg/openbox/rc.xml \
    && grep -q 'title="Place1 - Roblox Studio"' /etc/xdg/openbox/rc.xml
USER studio
# the browser sign-in hand-back: our handler stops a running Studio first (the hand-off
# between two Studio copies hangs under Wine), then starts Studio with the code
RUN xdg-mime default studio-auth.desktop x-scheme-handler/roblox-studio-auth \
    && xdg-mime default org.vinegarhq.Vinegar.desktop x-scheme-handler/roblox-studio
WORKDIR /home/studio
# noVNC (browser desktop). Bind it to localhost when you run the container.
EXPOSE 6080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
