"""FastAPI application.

Serves the API, the CALL-E webhook receiver, and the built dashboard.
The CALL-E key lives only in this process's environment; the browser
never receives it (``docs/security-privacy.md``).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.middleware import RequestIdMiddleware
from app.api.routes import router
from app.config import get_settings
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info(
        "resolve-e starting",
        extra={
            "app_env": settings.app_env,
            "live_calls": settings.live_calls_enabled,
        },
    )
    if not settings.live_calls_enabled:
        log.warning(
            "LIVE CALLS DISABLED - using the deterministic mock provider. "
            "Set CALLE_LIVE_CALLS=true with a valid CALLE_API_KEY to place real calls."
        )
    yield
    log.info("resolve-e stopping")


app = FastAPI(
    title="Resolve-E",
    description=(
        "Autonomous exception-resolution agent that uses phone calls as its "
        "execution layer, powered by CALL-E."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Outermost, so even a CORS rejection is correlated.
app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    CORSMiddleware,
    # The dashboard is served from this same origin in Docker; these are
    # the Vite dev-server origins for local frontend work.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if FRONTEND_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")
