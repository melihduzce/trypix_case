"""
API Routes — Internal image generation API + operator endpoints.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.router.routing_engine import RoutingEngine
from app.router.health_tracker import HealthTracker
from app.db.event_store import EventStore
from app.providers.base import GenerationStatus

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request / Response Schemas ─────────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    num_images: int = Field(default=1, ge=1, le=4)
    extra_params: dict = Field(default_factory=dict)


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    provider_used: str
    image_urls: list[str]
    latency_ms: Optional[float]
    error_message: Optional[str] = None


class ProviderOverrideRequest(BaseModel):
    disabled: bool


# ── Dependencies ──────────────────────────────────────────────────────────

def get_routing_engine() -> RoutingEngine:
    from app.main import routing_engine
    return routing_engine

def get_health_tracker() -> HealthTracker:
    from app.main import health_tracker
    return health_tracker

def get_event_store() -> EventStore:
    from app.main import event_store
    return event_store


# ── Generation Endpoint ───────────────────────────────────────────────────

@router.post("/generate", response_model=GenerateResponse)
async def generate_image(
    req: GenerateRequest,
    engine: RoutingEngine = Depends(get_routing_engine),
):
    """
    Submit an image generation request.
    The service selects the best provider and fails over automatically.
    Returns once generation is complete (synchronous from caller's perspective).
    """
    result = await engine.generate(
        prompt=req.prompt,
        width=req.width,
        height=req.height,
        num_images=req.num_images,
        extra_params=req.extra_params,
    )

    if result.status == GenerationStatus.FAILED and not result.image_urls:
        raise HTTPException(
            status_code=503,
            detail=result.error_message or "Generation failed on all providers",
        )

    return GenerateResponse(
        job_id=result.job_id,
        status=result.status.value,
        provider_used=result.provider_name,
        image_urls=result.image_urls,
        latency_ms=result.latency_ms,
        error_message=result.error_message,
    )


# ── Operator Dashboard Endpoints ──────────────────────────────────────────

@router.get("/dashboard/health")
async def get_health_snapshot(
    tracker: HealthTracker = Depends(get_health_tracker),
    engine: RoutingEngine = Depends(get_routing_engine),
):
    """Per-provider health status for the dashboard."""
    return {
        "primary_provider": engine.get_primary_provider(),
        "provider_order": engine.get_provider_order(),
        "providers": tracker.snapshot(),
    }


@router.get("/dashboard/metrics/{provider_name}")
async def get_provider_metrics(
    provider_name: str,
    store: EventStore = Depends(get_event_store),
):
    """Success rate over time + latency for a specific provider."""
    return {
        "provider_name": provider_name,
        "success_rate_over_time": store.get_success_rate_over_time(provider_name),
    }


@router.get("/dashboard/recent")
async def get_recent_activity(
    store: EventStore = Depends(get_event_store),
):
    """Recent generations, failures, and failovers."""
    return {
        "recent_generations": store.get_recent_generations(limit=30),
        "recent_failures": store.get_recent_failures(limit=10),
        "recent_failovers": store.get_recent_failovers(limit=10),
        "summary": store.get_stats_summary(),
    }


@router.post("/operator/providers/{provider_name}/override")
async def override_provider(
    provider_name: str,
    body: ProviderOverrideRequest,
    tracker: HealthTracker = Depends(get_health_tracker),
    engine: RoutingEngine = Depends(get_routing_engine),
):
    """
    Operator override: manually enable or disable a provider without restart.
    """
    if provider_name not in engine.providers:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

    tracker.set_operator_disabled(provider_name, body.disabled)
    action = "disabled" if body.disabled else "enabled"
    logger.info(f"[api] OPERATOR OVERRIDE provider={provider_name} action={action}")

    return {
        "provider_name": provider_name,
        "disabled": body.disabled,
        "message": f"Provider '{provider_name}' has been {action}",
        "current_primary": engine.get_primary_provider(),
    }


@router.get("/operator/providers")
async def list_providers(
    tracker: HealthTracker = Depends(get_health_tracker),
    engine: RoutingEngine = Depends(get_routing_engine),
):
    """List all providers with their current status."""
    snapshot = {p["provider_name"]: p for p in tracker.snapshot()}
    result = []
    for name in engine.provider_order:
        health = snapshot.get(name, {})
        result.append({
            "name": name,
            "status": health.get("status", "unknown"),
            "operator_disabled": health.get("operator_disabled", False),
            "success_rate": health.get("success_rate"),
            "avg_latency_ms": health.get("avg_latency_ms"),
        })
    return {"providers": result, "primary": engine.get_primary_provider()}


@router.get("/health")
async def service_health():
    """Service liveness check."""
    return {"status": "ok"}
