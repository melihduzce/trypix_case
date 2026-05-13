"""
TRYPIX Çoklu Sağlayıcılı Üretim Servisi
FastAPI uygulamasının ana giriş noktası.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.providers.fal_provider import FalProvider
from app.providers.openrouter_provider import OpenRouterProvider
from app.router.health_tracker import HealthTracker
from app.router.routing_engine import RoutingEngine
from app.db.event_store import EventStore
from app.api.routes import router

# ── Logging Setup ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Global singletons (injected into routes via Depends) ──────────────────
health_tracker: HealthTracker = None
routing_engine: RoutingEngine = None
event_store: EventStore = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global health_tracker, routing_engine, event_store

    logger.info("[startup] Initializing TRYPIX service...")

    # Load API keys from environment
    fal_api_key = os.environ.get("FAL_API_KEY", "")
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
    db_path = os.environ.get("DB_PATH", "trypix.db")

    if not fal_api_key:
        logger.warning("[startup] FAL_API_KEY not set — FAL provider will fail auth")
    if not openrouter_api_key:
        logger.warning("[startup] OPENROUTER_API_KEY not set — OpenRouter provider will fail auth")

    # Initialize providers
    providers = [
        FalProvider(
            api_key=fal_api_key,
            timeout_seconds=float(os.environ.get("FAL_TIMEOUT_SECONDS", "120")),
        ),
        OpenRouterProvider(
            api_key=openrouter_api_key,
            timeout_seconds=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "90")),
        ),
    ]

    # Initialize core components
    event_store = EventStore(db_path=db_path)
    health_tracker = HealthTracker()
    routing_engine = RoutingEngine(
        providers=providers,
        health_tracker=health_tracker,
        event_store=event_store,
    )

    logger.info(f"[startup] Providers: {[p.name for p in providers]}")
    logger.info(f"[startup] Primary provider: {routing_engine.get_primary_provider()}")
    logger.info("[startup] TRYPIX service ready")

    yield

    # Shutdown: close HTTP clients
    logger.info("[shutdown] Closing provider connections...")
    for provider in providers:
        if hasattr(provider, "close"):
            await provider.close()
    logger.info("[shutdown] Done")


#App

app = FastAPI(
    title="TRYPIX Multi-Provider Generation Service",
    description="AI image generation with automatic failover and health-based routing",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/")
async def root():
    return {
        "service": "TRYPIX Generation Service",
        "version": "1.0.0",
        "docs": "/docs",
    }
