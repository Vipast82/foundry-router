# Foundry Router — single-service image.
# Multi-stage: builder stage compiles wheels so the final image carries no
# compilers or pip caches; final stage is slim Python + the app, non-root.

FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim
LABEL org.opencontainers.image.title="foundry-router" \
      org.opencontainers.image.description="Agentic LLM routing middleware with an Ollama-compatible API. Internal/private-network use only." \
      org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --uid 1000 foundry

WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY foundry_router/ ./foundry_router/
COPY config.example.yaml .

# /config holds config.yaml (copied from config.example.yaml on first start if
# absent); /data holds the SQLite DB. Both MUST be volume mounts so nothing a
# user configures or accumulates lives in the image layer (survives rebuilds).
RUN mkdir -p /config /data && chown -R foundry:foundry /config /data /app
VOLUME ["/config", "/data"]

USER foundry
ENV FOUNDRY_CONFIG=/config/config.yaml \
    FOUNDRY_DATA_DIR=/data \
    FOUNDRY_HOST=0.0.0.0 \
    FOUNDRY_PORT=11435 \
    PYTHONUNBUFFERED=1

# Default port 11435: deliberately Ollama-adjacent (11434 + 1) so clients feel
# familiar but a real Ollama can coexist on the same host.
EXPOSE 11435

CMD ["python", "-m", "foundry_router"]
