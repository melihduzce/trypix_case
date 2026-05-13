"""
TRYPIX Çoklu Sağlayıcılı Üretim Servisi için test takımı.

Gereksinim duyulan tüm senaryoları kapsar:

Birincil sağlayıcının başarılı çağrılması

Birincil sağlayıcı hatasında ikinciye geçiş (fallback)

Sağlık durumu eşik değerine ulaşınca sağlayıcının devreden çıkarılması (demotion)

Operatör müdahalesi ile bir sağlayıcının rotasyondan alınması
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.providers.base import (
    BaseProvider, GenerationRequest, GenerationResult,
    GenerationStatus, ProviderError, ProviderRateLimitError,
    ProviderTimeoutError, ProviderAuthError, ProviderStatus
)
from app.router.health_tracker import HealthTracker, DEGRADED_THRESHOLD, CIRCUIT_OPEN_THRESHOLD, CONSECUTIVE_FAIL_LIMIT
from app.router.routing_engine import RoutingEngine
from app.db.event_store import EventStore


# ── Test Fixtures ─────────────────────────────────────────────────────────

def make_mock_provider(name: str, success: bool = True, latency_ms: float = 500.0) -> MagicMock:
    """Creates a mock provider that succeeds or fails predictably."""
    provider = MagicMock(spec=BaseProvider)
    provider.name = name

    if success:
        async def _generate(request):
            await asyncio.sleep(latency_ms / 1000)
            return GenerationResult(
                job_id=request.job_id,
                provider_name=name,
                status=GenerationStatus.COMPLETED,
                image_urls=[f"https://example.com/{name}/image.png"],
                latency_ms=latency_ms,
            )
        provider.generate = _generate
    else:
        async def _generate_fail(request):
            raise ProviderError(f"{name} failed", is_retryable=True)
        provider.generate = _generate_fail

    return provider


def make_engine(providers, db_path=":memory:") -> tuple[RoutingEngine, HealthTracker, EventStore]:
    store = EventStore(db_path=db_path)
    tracker = HealthTracker()
    engine = RoutingEngine(providers=providers, health_tracker=tracker, event_store=store)
    return engine, tracker, store


# ── Test 1: Successful Primary Call ───────────────────────────────────────

@pytest.mark.asyncio
async def test_successful_primary_call():
    """
    Birincil sağlayıcı başarılı olduğunda, sonuç birincil sağlayıcıdan gelmeli ve
    health_tracker bu çağrıyı başarılı olarak kaydetmeli.
    """
    primary = make_mock_provider("fal", success=True, latency_ms=300.0)
    secondary = make_mock_provider("openrouter", success=True, latency_ms=500.0)

    engine, tracker, store = make_engine([primary, secondary])

    result = await engine.generate(prompt="A beautiful sunset over the ocean")

    assert result.status == GenerationStatus.COMPLETED
    assert result.provider_name == "fal"
    assert len(result.image_urls) == 1
    assert result.image_urls[0] == "https://example.com/fal/image.png"

    # Health tracker should record success for fal
    health = tracker.get_health("fal")
    assert health is not None
    assert health.consecutive_failures == 0
    assert health.success_rate() == 1.0

    print(" Test 1 passed: Successful primary call")


# ── Test 2: Primary Failure with Secondary Fallback ───────────────────────

@pytest.mark.asyncio
async def test_primary_failure_fallback_to_secondary():
    """
    Birincil sağlayıcı tekrar denenebilir bir hatayla başarısız olduğunda, servis
    gözle görülür bir şekilde ikincil sağlayıcıya geçiş yapmalı (failover).
    """
    primary = make_mock_provider("fal", success=False)
    secondary = make_mock_provider("openrouter", success=True, latency_ms=800.0)

    engine, tracker, _ = make_engine([primary, secondary])

    result = await engine.generate(prompt="A futuristic city skyline")

    assert result.status == GenerationStatus.COMPLETED
    assert result.provider_name == "openrouter", (
        f"Expected 'openrouter' but got '{result.provider_name}' — failover did not work"
    )
    assert len(result.image_urls) == 1

    # fal should have a failure recorded
    fal_health = tracker.get_health("fal")
    assert fal_health.consecutive_failures >= 1

    # openrouter should have a success recorded
    openrouter_health = tracker.get_health("openrouter")
    assert openrouter_health.consecutive_failures == 0

    print(" Test 2 passed: Primary failure → fallback to secondary")


# ── Test 3: Health-Triggered Demotion ─────────────────────────────────────

@pytest.mark.asyncio
async def test_health_triggered_demotion():
    """
    Bir sağlayıcı, ardışık hata limitini (CONSECUTIVE_FAIL_LIMIT) aşacak kadar hata topladığında,
    health tracker onu CIRCUIT_OPEN olarak işaretlemeli ve routing engine onu atlamalı.
    """
    tracker = HealthTracker()

    # Simulate consecutive failures on fal
    for i in range(CONSECUTIVE_FAIL_LIMIT):
        tracker.record_failure("fal", f"server error {i}", latency_ms=100.0)

    fal_status = tracker.get_status("fal")
    assert fal_status == ProviderStatus.CIRCUIT_OPEN, (
        f"Expected CIRCUIT_OPEN after {CONSECUTIVE_FAIL_LIMIT} consecutive failures, got {fal_status}"
    )

    # Routing engine should not route to fal
    primary = make_mock_provider("fal", success=True)
    secondary = make_mock_provider("openrouter", success=True)

    store = EventStore(db_path=":memory:")
    engine = RoutingEngine(providers=[primary, secondary], health_tracker=tracker, event_store=store)

    result = await engine.generate(prompt="Abstract art")

    assert result.provider_name == "openrouter", (
        f"Expected 'openrouter' after fal circuit opened, got '{result.provider_name}'"
    )
    assert result.status == GenerationStatus.COMPLETED

    print(f" Test 3 passed: Health-triggered demotion (CIRCUIT_OPEN after {CONSECUTIVE_FAIL_LIMIT} failures)")


# ── Test 4: Operator Override ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_override_takes_provider_out_of_rotation():
    """
    Bir operatör bir sağlayıcıyı devre dışı bıraktığında, sonraki tüm istekler
    devre dışı bırakılan sağlayıcı sağlıklı olsa bile diğer sağlayıcılara yönlenmeli.
    """
    primary = make_mock_provider("fal", success=True)
    secondary = make_mock_provider("openrouter", success=True)

    engine, tracker, _ = make_engine([primary, secondary])

    # Confirm fal is initially primary
    assert engine.get_primary_provider() == "fal"

    # Operator disables fal
    tracker.set_operator_disabled("fal", disabled=True)

    # fal should now be OPERATOR_DISABLED
    assert tracker.get_status("fal") == ProviderStatus.OPERATOR_DISABLED
    assert not tracker.is_available("fal")

    # Routing should now go to openrouter
    result = await engine.generate(prompt="A peaceful mountain landscape")

    assert result.provider_name == "openrouter", (
        f"Expected 'openrouter' after fal was operator-disabled, got '{result.provider_name}'"
    )
    assert result.status == GenerationStatus.COMPLETED

    # Re-enable fal
    tracker.set_operator_disabled("fal", disabled=False)
    assert tracker.is_available("fal")
    # fal is back in rotation (both providers now available)
    available = engine.get_provider_order()
    assert "fal" in available, f"fal should be back in rotation, got: {available}"

    print(" Test 4 passed: Operator override removes provider from rotation")


# ── Test 5: All Providers Unavailable ─────────────────────────────────────

@pytest.mark.asyncio
async def test_all_providers_unavailable():
    """
    Tüm sağlayıcılar devresi açık (circuit-open) veya devre dışı (disabled) olduğunda, servis
    exception fırlatmak yerine FAILED sonucu döndürmeli.
    """
    tracker = HealthTracker()

    # Open circuits for both providers
    for _ in range(CONSECUTIVE_FAIL_LIMIT):
        tracker.record_failure("fal", "error")
        tracker.record_failure("openrouter", "error")

    primary = make_mock_provider("fal", success=True)
    secondary = make_mock_provider("openrouter", success=True)

    store = EventStore(db_path=":memory:")
    engine = RoutingEngine(providers=[primary, secondary], health_tracker=tracker, event_store=store)

    result = await engine.generate(prompt="Test")

    assert result.status == GenerationStatus.FAILED
    assert result.provider_name == "none"
    assert result.error_message is not None

    print(" Test 5 passed: All providers unavailable → graceful failure")


# ── Test 6: Rate Limit Triggers Failover ──────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_triggers_failover():
    """
    Birincil sağlayıcıdan gelen 429 rate-limit hatası, ikincil sağlayıcıya
    failover'ı tetiklemeli.
    """
    primary = MagicMock(spec=BaseProvider)
    primary.name = "fal"

    async def rate_limit_generate(request):
        raise ProviderRateLimitError("Rate limit exceeded")
    primary.generate = rate_limit_generate

    secondary = make_mock_provider("openrouter", success=True)

    engine, tracker, _ = make_engine([primary, secondary])

    result = await engine.generate(prompt="Test rate limit failover")

    assert result.status == GenerationStatus.COMPLETED
    assert result.provider_name == "openrouter"

    print(" Test 6 passed: Rate limit error triggers failover")


# ── Test 7: Auth Error Does NOT Trigger Failover ──────────────────────────

@pytest.mark.asyncio
async def test_auth_error_does_not_failover():
    """
    Yetkilendirme (auth) hataları yanlış yapılandırmayı gösterir, geçici bir başarısızlık değildir.
    Servis, auth hatalarında failover yapmamalı.
    """
    primary = MagicMock(spec=BaseProvider)
    primary.name = "fal"

    async def auth_fail_generate(request):
        raise ProviderAuthError("Invalid API key")
    primary.generate = auth_fail_generate

    secondary = make_mock_provider("openrouter", success=True)

    engine, tracker, _ = make_engine([primary, secondary])

    result = await engine.generate(prompt="Test auth error")

    # Should fail immediately without trying secondary
    assert result.status == GenerationStatus.FAILED
    assert "fal" in result.provider_name or "Auth error" in (result.error_message or "")

    print(" Test 7 passed: Auth error does NOT trigger failover")


# ── Test 8: Health Tracker Sliding Window ─────────────────────────────────

def test_health_tracker_sliding_window():
    """
    Kayan pencere (sliding window), yalnızca WINDOW_SECONDS içindeki sonuçları dikkate almalı.
    Eski hatalar mevcut sağlık durumunu etkilememeli.
    """
    tracker = HealthTracker()

    # Simulate old failures (outside window)
    old_time = time.time() - 120  # 2 minutes ago (outside 60s window)
    from app.router.health_tracker import Outcome
    health = tracker._get_or_create("fal")

    for _ in range(5):
        health.outcomes.append(Outcome(
            timestamp=old_time,
            success=False,
            latency_ms=100.0,
            error_reason="old error",
        ))

    # Recent success
    tracker.record_success("fal", latency_ms=200.0)

    # Success rate should be high (old failures outside window)
    assert tracker.get_status("fal") == ProviderStatus.HEALTHY

    print("Test 8 passed: Sliding window ignores old failures")


# ── Run all tests ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def run_all():
        print("\n=== TRYPIX Test Suite ===\n")
        await test_successful_primary_call()
        await test_primary_failure_fallback_to_secondary()
        await test_health_triggered_demotion()
        await test_operator_override_takes_provider_out_of_rotation()
        await test_all_providers_unavailable()
        await test_rate_limit_triggers_failover()
        await test_auth_error_does_not_failover()
        test_health_tracker_sliding_window()
        print("\n=== All tests passed! ===\n")

    asyncio.run(run_all())
