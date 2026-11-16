# The code-checks service (v0.181.165): the Luau checkers in a container of their
# own, so the router's image is unchanged and the whole thing stays optional.
#
#   docker compose --profile code-checks up -d --build
#
# Every download is pinned to a version AND its checksum (the digests GitHub
# publishes with each release). A changed file fails the build rather than
# landing in the image.
FROM debian:bookworm-slim AS tools

ARG LUAU_LSP_VERSION=1.70.1
ARG LUAU_LSP_SHA256=1a2ea1ae4f98f8946cefd970a4b54853e11ab4775e6932faa7f60cf920346567
ARG SELENE_VERSION=0.32.0
ARG SELENE_SHA256=d3773393578580074386e69337d35b7acddfbaa9fc38964c07fa3e28436ade9b
ARG STYLUA_VERSION=2.6.0
ARG STYLUA_SHA256=a485f72bbfa22e4642e77a84b5edc2611a5cab7477b34e2e709dd8f78e919895
# Roblox's own type definitions, as luau-lsp ships them for this release. Without
# them `game`, `Player` and every service are unknown globals and good Roblox code
# is reported as broken.
ARG GLOBAL_TYPES_SHA256=2857efa8245485f8c25c19c018ee1ef9b46afab4efd2ea70fc3257cd3faf7080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /dl
RUN set -eux; \
    curl -fsSLO "https://github.com/JohnnyMorganz/luau-lsp/releases/download/${LUAU_LSP_VERSION}/luau-lsp-linux-x86_64.zip"; \
    echo "${LUAU_LSP_SHA256}  luau-lsp-linux-x86_64.zip" | sha256sum -c -; \
    curl -fsSLO "https://github.com/Kampfkarren/selene/releases/download/${SELENE_VERSION}/selene-${SELENE_VERSION}-linux.zip"; \
    echo "${SELENE_SHA256}  selene-${SELENE_VERSION}-linux.zip" | sha256sum -c -; \
    curl -fsSLO "https://github.com/JohnnyMorganz/StyLua/releases/download/v${STYLUA_VERSION}/stylua-linux-x86_64.zip"; \
    echo "${STYLUA_SHA256}  stylua-linux-x86_64.zip" | sha256sum -c -; \
    for z in *.zip; do unzip -o -j "$z" -d /out; done; \
    chmod +x /out/*; \
    curl -fsSL -o /out/globalTypes.d.luau \
        "https://raw.githubusercontent.com/JohnnyMorganz/luau-lsp/${LUAU_LSP_VERSION}/scripts/globalTypes.d.luau"; \
    echo "${GLOBAL_TYPES_SHA256}  /out/globalTypes.d.luau" | sha256sum -c -; \
    chmod 0644 /out/globalTypes.d.luau; \
    ls -l /out

FROM python:3.12-slim

LABEL org.opencontainers.image.title="foundry-code-checks" \
      org.opencontainers.image.description="Runs Luau checkers on snippets for Foundry Router's code checks. Internal network only." \
      org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --uid 1000 checker
COPY --from=tools /out/luau-lsp /out/selene /out/stylua /usr/local/bin/
COPY --from=tools /out/globalTypes.d.luau /usr/local/share/luau/globalTypes.d.luau
COPY server.py /app/server.py

# No volumes, no credentials, nothing to write but /tmp: what this looks at was
# written by a model.
USER checker
WORKDIR /tmp
EXPOSE 8099
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=5).status == 200 else 1)"
CMD ["python", "/app/server.py"]
