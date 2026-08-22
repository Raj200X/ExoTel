"""Main FastAPI entrypoint for Saarthi.

This module initializes the FastAPI application, sets up middleware,
registers the lifecycle events (startup/shutdown), and mounts
the API routers and static files.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from saarthi.api import browser_voice, routes, twilio, voicebot, webhooks
from saarthi.core import state
from saarthi.core.config import AppConfig
from saarthi.core.database import Database
from saarthi.services.call_analyzer import CallAnalyzer

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize all components on startup, clean up on shutdown.

    Sets up the global configuration, logging, database connection pool,
    and external service clients (like CallAnalyzer) in the `state` module.
    """
    # 1. Load configuration
    state.config = AppConfig.from_env()

    # 2. Configure logging
    logging.basicConfig(
        level=getattr(logging, state.config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting Saarthi in %s mode", state.config.env)

    # 3. Initialize database
    state.db = Database(state.config.database_url)
    await state.db.init()
    logger.info("Database initialized")

    # 4. Initialize Call Analyzer (if Gemini API key is present)
    if state.config.has_gemini:
        state.analyzer = CallAnalyzer(
            api_key=state.config.gemini_api_key,
            model_name=state.config.model_name,
        )
        logger.info("Call Analyzer initialized (model: %s)", state.config.model_name)
    else:
        logger.warning("GEMINI_API_KEY not set. Call analysis will not be available.")

    logger.info("Saarthi startup complete")
    
    yield  # Application runs here

    # 5. Shutdown and clean up resources
    if state.db:
        await state.db.close()
    logger.info("Saarthi shutting down")


# ---------------------------------------------------------------------------
# FastAPI Application Initialization
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Saarthi",
    description=(
        "AI-powered phone guidance service. Users call a real phone number, "
        "speak naturally in Hindi, Hinglish, or English, and receive guidance. "
        "This dashboard monitors and manages the service."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Allow CORS for dashboard and potential external integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Mount Routers
# ---------------------------------------------------------------------------

app.include_router(routes.router)
app.include_router(webhooks.router)
app.include_router(voicebot.router)
app.include_router(twilio.router)
app.include_router(browser_voice.router)


# ---------------------------------------------------------------------------
# Serve Dashboard SPA
# ---------------------------------------------------------------------------

static_dir = Path(__file__).parent / "static"


@app.get("/", tags=["Dashboard"], response_model=None)
async def serve_dashboard():
    """Serve the single-page application (SPA) dashboard.

    Returns:
        The index.html file if it exists, otherwise a generic JSON status response.
    """
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse(
        {"message": "Saarthi is running. Dashboard not found."},
        status_code=200,
    )


# Mount the static directory so assets (JS, CSS) can be fetched
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Health Check Endpoint
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, Any]:
    """Health check endpoint to verify service status.

    Returns:
        A dictionary containing system status, version, and component availability.
    """
    return {
        "status": "ok",
        "service": "Saarthi",
        "version": "1.0.0",
        "env": state.config.env if state.config else "unknown",
        "gemini_available": state.config.has_gemini if state.config else False,
        "exotel_configured": state.config.has_exotel if state.config else False,
        "phone_number": state.config.saarthi_phone_number if state.config else "",
    }


def main() -> None:
    """Run the FastAPI server via Uvicorn.

    This function is primarily used when running the script directly.
    """
    import uvicorn

    app_config = AppConfig.from_env()
    uvicorn.run(
        "saarthi.main:app",
        host=app_config.host,
        port=app_config.port,
        reload=app_config.env == "development",
    )


if __name__ == "__main__":
    main()
