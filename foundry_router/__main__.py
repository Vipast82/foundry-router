"""`python -m foundry_router` — the container entrypoint."""

import asyncio
import contextlib
import logging
import os

import uvicorn

from .main import create_app

log = logging.getLogger("foundry_router")


def run() -> None:
    app = create_app()
    cfg = app.state.services.config_store.config
    host = os.environ.get("FOUNDRY_HOST") or cfg.server.host
    port = int(os.environ.get("FOUNDRY_PORT") or cfg.server.port)
    sec = cfg.secrets
    if not sec.https_enabled:
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    # Access → Secrets: a second, HTTPS-only listener (self-signed, pinned by
    # fingerprint) for secret traffic. Same app; only the plain server runs the
    # lifespan, so background work starts once.
    from .config import data_dir
    from .tls import ensure_cert
    tls_port = int(os.environ.get("FOUNDRY_TLS_PORT") or sec.https_port)
    try:
        cert, key, fp = ensure_cert(data_dir())
    except Exception:                                            # noqa: BLE001
        log.exception("could not make the HTTPS certificate: serving plain HTTP only")
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    app.state.tls = {"port": tls_port, "fingerprint": fp}
    plain = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    tls = uvicorn.Server(uvicorn.Config(app, host=host, port=tls_port, log_level="info",
                                        ssl_certfile=str(cert), ssl_keyfile=str(key),
                                        lifespan="off"))
    tls.capture_signals = contextlib.nullcontext      # the plain server owns the signals

    async def both():
        t = asyncio.create_task(tls.serve())
        try:
            await plain.serve()
        finally:
            tls.should_exit = True
            await t
    asyncio.run(both())


if __name__ == "__main__":
    run()
