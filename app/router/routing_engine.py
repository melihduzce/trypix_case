"""
Routing Engine — Primary selection and failover.

Picks the best available provider for each request based on observed health,
then falls back to the next available provider on failure.

Routing priority:
  1. Skip OPERATOR_DISABLED providers
  2. Skip CIRCUIT_OPEN providers
  3. Prefer HEALTHY over DEGRADED
  4. Among equal-status providers, prefer lower avg latency

Failover trigger criteria:
  - Any ProviderError with is_retryable=True
  - ProviderRateLimitError (429)
  - ProviderTimeoutError
  - Unexpected exceptions

NOT triggering failover:
  - ProviderAuthError (is_retryable=False) — indicates misconfiguration, not transience
  - ProviderError with is_retryable=False — client errors, won't improve on retry
"""

import logging
import time
import uuid
from typing import Optional

from app.providers.base import (
    BaseProvider, GenerationRequest, GenerationResult,
    GenerationStatus, ProviderError, ProviderAuthError
)
from app.router.health_tracker import HealthTracker
from app.providers.base import ProviderStatus
from app.db.event_store import EventStore

logger = logging.getLogger(__name__)


class RoutingEngine:
    """
    Selects the primary provider and handles failover transparently.

    The caller receives a GenerationResult regardless of which provider
    actually served the request.
    """

    def __init__(self, providers: list[BaseProvider], health_tracker: HealthTracker, event_store: EventStore):
        self.providers = {p.name: p for p in providers}
        self.provider_order = [p.name for p in providers]  # Default priority order
        self.health_tracker = health_tracker
        self.event_store = event_store

        # Register all providers with the health tracker
        for name in self.provider_order:
            self.health_tracker.register(name)

    def _select_providers(self) -> list[str]:
        """
        Returns providers sorted by priority: HEALTHY first, DEGRADED second,
        within each group sorted by avg latency ascending.
        Excludes CIRCUIT_OPEN and OPERATOR_DISABLED providers.
        """
        healthy = []
        degraded = []

        for name in self.provider_order:
            status = self.health_tracker.get_status(name)
            if status == ProviderStatus.HEALTHY:
                healthy.append(name)
            elif status == ProviderStatus.DEGRADED:
                degraded.append(name)
            # CIRCUIT_OPEN and OPERATOR_DISABLED are skipped

        def latency_key(name: str) -> float:
            h = self.health_tracker.get_health(name)
            if h and h.avg_latency_ms > 0:
                return h.avg_latency_ms
            return float("inf")

        healthy.sort(key=latency_key)
        degraded.sort(key=latency_key)

        return healthy + degraded

    async def generate(self, prompt: str, width: int = 1024, height: int = 1024,
                       num_images: int = 1, extra_params: Optional[dict] = None) -> GenerationResult:
        """
        Execute generation with automatic failover.
        Tries providers in priority order until one succeeds or all fail.
        """
        job_id = str(uuid.uuid4())
        request = GenerationRequest(
            job_id=job_id,
            prompt=prompt,
            width=width,
            height=height,
            num_images=num_images,
            extra_params=extra_params or {},
        )

        provider_sequence = self._select_providers()

        if not provider_sequence:
            logger.error(f"[router] No available providers for job_id={job_id}")
            await self.event_store.record_routing_decision(
                job_id=job_id,
                selected_provider=None,
                fallback_sequence=[],
                reason="all_providers_unavailable",
            )
            return GenerationResult(
                job_id=job_id,
                provider_name="none",
                status=GenerationStatus.FAILED,
                error_message="All providers are currently unavailable",
            )

        primary = provider_sequence[0]
        logger.info(
            f"[router] Routing job_id={job_id} "
            f"primary={primary} "
            f"fallbacks={provider_sequence[1:]} "
            f"prompt_preview={prompt[:50]!r}"
        )

        await self.event_store.record_routing_decision(
            job_id=job_id,
            selected_provider=primary,
            fallback_sequence=provider_sequence[1:],
            reason="health_based_selection",
        )

        last_error: Optional[str] = None

        for attempt, provider_name in enumerate(provider_sequence):
            provider = self.providers[provider_name]
            start_time = time.monotonic()

            if attempt > 0:
                logger.warning(
                    f"[router] FAILOVER job_id={job_id} "
                    f"attempt={attempt + 1} "
                    f"trying={provider_name} "
                    f"previous_error={last_error}"
                )
                await self.event_store.record_failover(
                    job_id=job_id,
                    from_provider=provider_sequence[attempt - 1],
                    to_provider=provider_name,
                    reason=last_error or "unknown",
                )

            try:
                result = await provider.generate(request)
                latency_ms = (time.monotonic() - start_time) * 1000

                self.health_tracker.record_success(provider_name, latency_ms)
                await self.event_store.record_generation(
                    job_id=job_id,
                    provider_name=provider_name,
                    success=True,
                    latency_ms=latency_ms,
                    attempt=attempt + 1,
                )

                return result

            except ProviderAuthError as e:
                # Auth errors are not retryable — fail immediately
                latency_ms = (time.monotonic() - start_time) * 1000
                error_msg = str(e)
                self.health_tracker.record_failure(provider_name, error_msg, latency_ms)
                await self.event_store.record_generation(
                    job_id=job_id,
                    provider_name=provider_name,
                    success=False,
                    latency_ms=latency_ms,
                    error_reason=error_msg,
                    attempt=attempt + 1,
                )
                logger.error(f"[router] AUTH ERROR provider={provider_name} job_id={job_id}: {e}")
                # Don't try next provider — auth errors indicate misconfiguration
                return GenerationResult(
                    job_id=job_id,
                    provider_name=provider_name,
                    status=GenerationStatus.FAILED,
                    error_message=f"Auth error on {provider_name}: {e}",
                )

            except ProviderError as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                error_msg = str(e)
                last_error = error_msg
                self.health_tracker.record_failure(provider_name, error_msg, latency_ms)
                await self.event_store.record_generation(
                    job_id=job_id,
                    provider_name=provider_name,
                    success=False,
                    latency_ms=latency_ms,
                    error_reason=error_msg,
                    attempt=attempt + 1,
                )
                logger.warning(
                    f"[router] PROVIDER ERROR provider={provider_name} "
                    f"job_id={job_id} retryable={e.is_retryable}: {e}"
                )

                if not e.is_retryable:
                    # Non-retryable: record but don't try next provider
                    break

                # Retryable: continue to next provider
                continue

            except Exception as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                error_msg = f"Unexpected error: {e}"
                last_error = error_msg
                self.health_tracker.record_failure(provider_name, error_msg, latency_ms)
                await self.event_store.record_generation(
                    job_id=job_id,
                    provider_name=provider_name,
                    success=False,
                    latency_ms=latency_ms,
                    error_reason=error_msg,
                    attempt=attempt + 1,
                )
                logger.exception(f"[router] UNEXPECTED ERROR provider={provider_name} job_id={job_id}")
                continue

        # All providers exhausted
        logger.error(f"[router] ALL PROVIDERS FAILED job_id={job_id} last_error={last_error}")
        return GenerationResult(
            job_id=job_id,
            provider_name="none",
            status=GenerationStatus.FAILED,
            error_message=f"All providers failed. Last error: {last_error}",
        )

    def get_primary_provider(self) -> Optional[str]:
        """Returns the name of the current primary provider."""
        seq = self._select_providers()
        return seq[0] if seq else None

    def get_provider_order(self) -> list[str]:
        """Returns the current provider priority order."""
        return self._select_providers()
