from __future__ import annotations

from fastapi import FastAPI

from lmr.config import load_config


def create_app(config_path: str = "/app/config/datasets.yaml") -> FastAPI:
    config = load_config(config_path)
    app = FastAPI(title="LMR Data Platform", version="0.1.0")
    app.state.config = config

    from lmr.serve.routes import router

    app.include_router(router)
    return app
