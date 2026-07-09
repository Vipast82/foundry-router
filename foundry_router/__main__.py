"""`python -m foundry_router` — the container entrypoint."""

import os

import uvicorn

from .main import create_app


def run() -> None:
    app = create_app()
    host = os.environ.get("FOUNDRY_HOST") or app.state.services.config_store.config.server.host
    port = int(os.environ.get("FOUNDRY_PORT")
               or app.state.services.config_store.config.server.port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
