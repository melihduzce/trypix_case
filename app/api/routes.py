"""
API Routes — Async job-based image generation API + operator endpoints.

External interface: job-id polling pattern
  POST /generate      → returns job_id immediately (non-blocking)
  GET  /jobs/{job_id} → poll for result


  API Route'lar — Async job tabanlı görsel üretme API'si + operatör endpoint'leri

Dış arayüz: job-id polling pattern kullanıyor
POST /generate → hemen job_id döner (beklemez, bloke etmez)
GET /jobs/{job_id} → sonucu sorgula (polling yap)
"""

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from app.router.routing_engine import RoutingEngine
from app.router.health_tracker import HealthTracker
from app.db.event_store import EventStore
from app.providers.base import GenerationStatus

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory job store (sufficient for single-instance deployment)
_jobs: dict[str, dict] = {}


#Şemalar(veri yapılar)

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    width: int = Field(default=512, ge=256, le=2048)
    height: int = Field(default=512, ge=256, le=2048)
    num_images: int = Field(default=1, ge=1, le=4)
    extra_params: dict = Field(default_factory=dict)


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    provider_used: Optional[str] = None
    image_urls: list[str] = []
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None


class ProviderOverrideRequest(BaseModel):
    disabled: bool


#Bağımlılıklar

def get_routing_engine() -> RoutingEngine:
    from app.main import routing_engine
    return routing_engine

def get_health_tracker() -> HealthTracker:
    from app.main import health_tracker
    return health_tracker

def get_event_store() -> EventStore:
    from app.main import event_store
    return event_store


# ── Arka planda çalışan üretim görevi ────────────────────────────────────────────

async def _run_generation(job_id: str, request: GenerateRequest, engine: RoutingEngine):
    _jobs[job_id] = {"status": "processing", "job_id": job_id}
    try:
        result = await engine.generate(
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            num_images=request.num_images,
            extra_params=request.extra_params,
        )
        _jobs[job_id] = {
            "job_id": job_id,
            "status": result.status.value,
            "provider_used": result.provider_name,
            "image_urls": result.image_urls,
            "latency_ms": result.latency_ms,
            "error_message": result.error_message,
        }
    except Exception as e:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "failed",
            "error_message": str(e),
            "image_urls": [],
        }


# Endpointler

@router.post("/generate", response_model=GenerateResponse)
async def generate_image(
    req: GenerateRequest,
    background_tasks: BackgroundTasks,
    engine: RoutingEngine = Depends(get_routing_engine),
):
    """
    Görsel üretme isteği gönder. Hemen job_id döner.
    Sonucu almak için GET /jobs/{job_id} adresini poll'la (sorgula).
    """
    import uuid
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending", "job_id": job_id}
    background_tasks.add_task(_run_generation, job_id, req, engine)
    return GenerateResponse(
        job_id=job_id,
        status="pending",
        message=f"Job submitted. Poll GET /api/v1/jobs/{job_id} for result.",
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Poll this endpoint to get generation result."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobStatusResponse(**job)


# ── Dashboard Endpoints ───────────────────────────────────────────────────

@router.get("/dashboard/health")
async def get_health_snapshot(
    tracker: HealthTracker = Depends(get_health_tracker),
    engine: RoutingEngine = Depends(get_routing_engine),
):
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
    return {
        "provider_name": provider_name,
        "success_rate_over_time": store.get_success_rate_over_time(provider_name),
    }


@router.get("/dashboard/recent")
async def get_recent_activity(
    store: EventStore = Depends(get_event_store),
):
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
    if provider_name not in engine.providers:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    tracker.set_operator_disabled(provider_name, body.disabled)
    action = "disabled" if body.disabled else "enabled"
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
    return {"status": "ok"}
