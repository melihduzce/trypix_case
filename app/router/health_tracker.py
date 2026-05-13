"""
Health Tracker (Sağlık Takipçisi) — Sliding Window (Kayan Pencere) Algoritması.

Zaman tabanlı kayan pencere kullanarak her sağlayıcının sağlık durumunu takip eder.
Her sağlayıcı, son X saniye içindeki çağrı sonuçlarını (başarılı/başarısız) tutar.
Sağlık skoru = pencere içindeki başarı oranı.

Neden bu algoritma seçildi?

Kayan pencere, basit sayaca göre daha hızlı tepki verir (yeni gelişmelere anında uyum sağlar)

Üstel düzleştirme (exponential smoothing) yöntemine göre daha basit ve hata ayıklaması daha kolaydır

Sayı tabanlı değil, zaman tabanlı olduğu için az trafik alan sağlayıcılar sonsuza kadar sağlıklı görünmez

Eşik Değerleri (ayarlanabilir):

DEGRADED (bozuldu): başarı oranı < 0.7 (son 60 saniyede)

CIRCUIT_OPEN (devre açık): başarı oranı < 0.4 VEYA 3+ ardışık hata

Recovery (iyileşme): 30 saniye boyunca başarı oranı >= 0.8 → sağlıklı duruma döner
"""

import time
import threading
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from app.providers.base import ProviderStatus

logger = logging.getLogger(__name__)

# Tunable thresholds
WINDOW_SECONDS = 60.0          # Sliding window size
DEGRADED_THRESHOLD = 0.70      # Below this → DEGRADED
CIRCUIT_OPEN_THRESHOLD = 0.40  # Below this → CIRCUIT_OPEN
CONSECUTIVE_FAIL_LIMIT = 3     # Immediate circuit open on N consecutive failures
RECOVERY_THRESHOLD = 0.80      # Must reach this to exit CIRCUIT_OPEN
RECOVERY_WINDOW = 30.0         # Must sustain recovery for this many seconds
MIN_OBSERVATIONS = 3           # Don't penalize providers with <3 data points


@dataclass
class Outcome:
    timestamp: float
    success: bool
    latency_ms: float
    error_reason: Optional[str] = None


@dataclass
class ProviderHealth:
    provider_name: str
    status: ProviderStatus = ProviderStatus.HEALTHY
    outcomes: deque = field(default_factory=lambda: deque(maxlen=500))
    consecutive_failures: int = 0
    operator_disabled: bool = False
    circuit_opened_at: Optional[float] = None
    last_status_change: float = field(default_factory=time.time)

    # For latency tracking
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0

    def success_rate(self, window_seconds: float = WINDOW_SECONDS) -> float:
        recent = self._recent_outcomes(window_seconds)
        if len(recent) < MIN_OBSERVATIONS:
            return 1.0  # Benefit of the doubt with few observations
        successes = sum(1 for o in recent if o.success)
        return successes / len(recent)

    def _recent_outcomes(self, window_seconds: float) -> list[Outcome]:
        cutoff = time.time() - window_seconds
        return [o for o in self.outcomes if o.timestamp >= cutoff]

    def recent_failures(self, limit: int = 10) -> list[dict]:
        failures = [o for o in reversed(self.outcomes) if not o.success]
        return [
            {
                "timestamp": o.timestamp,
                "error_reason": o.error_reason,
                "latency_ms": o.latency_ms,
            }
            for o in failures[:limit]
        ]

    def _compute_latency_percentiles(self, window_seconds: float = WINDOW_SECONDS) -> None:
        recent = [o for o in self._recent_outcomes(window_seconds) if o.success and o.latency_ms > 0]
        if not recent:
            return
        latencies = sorted(o.latency_ms for o in recent)
        n = len(latencies)
        self.avg_latency_ms = sum(latencies) / n
        self.p50_latency_ms = latencies[int(n * 0.50)]
        self.p95_latency_ms = latencies[int(n * 0.95)]


class HealthTracker:
    """
    Thread-safe (iş parçacığı güvenli) sağlayıcı bazlı sağlık takipçisi.

    Her çağrı sonucunu kaydeder ve her gözlem sonrasında sağlık durumunu yeniden hesaplar.
    Routing engine tarafından birincil sağlayıcıyı seçmek için kullanılır.
    """

    def __init__(self):
        self._providers: dict[str, ProviderHealth] = {}
        self._lock = threading.Lock()

    def register(self, provider_name: str) -> None:
        with self._lock:
            if provider_name not in self._providers:
                self._providers[provider_name] = ProviderHealth(provider_name=provider_name)
                logger.info(f"[health] Registered provider={provider_name}")

    def record_success(self, provider_name: str, latency_ms: float) -> None:
        with self._lock:
            health = self._get_or_create(provider_name)
            health.outcomes.append(Outcome(
                timestamp=time.time(),
                success=True,
                latency_ms=latency_ms,
            ))
            health.consecutive_failures = 0
            health._compute_latency_percentiles()
            self._recompute_status(health)
            logger.info(
                f"[health] SUCCESS provider={provider_name} "
                f"latency={latency_ms:.0f}ms "
                f"success_rate={health.success_rate():.2f} "
                f"status={health.status}"
            )

    def record_failure(self, provider_name: str, error_reason: str, latency_ms: float = 0.0) -> None:
        with self._lock:
            health = self._get_or_create(provider_name)
            health.outcomes.append(Outcome(
                timestamp=time.time(),
                success=False,
                latency_ms=latency_ms,
                error_reason=error_reason,
            ))
            health.consecutive_failures += 1
            self._recompute_status(health)
            logger.warning(
                f"[health] FAILURE provider={provider_name} "
                f"reason={error_reason} "
                f"consecutive={health.consecutive_failures} "
                f"success_rate={health.success_rate():.2f} "
                f"status={health.status}"
            )

    def _recompute_status(self, health: ProviderHealth) -> None:
        if health.operator_disabled:
            health.status = ProviderStatus.OPERATOR_DISABLED
            return

        prev_status = health.status
        rate = health.success_rate()

        # Immediate circuit open on consecutive failures
        if health.consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
            if health.status != ProviderStatus.CIRCUIT_OPEN:
                health.circuit_opened_at = time.time()
            health.status = ProviderStatus.CIRCUIT_OPEN

        elif rate < CIRCUIT_OPEN_THRESHOLD:
            if health.status != ProviderStatus.CIRCUIT_OPEN:
                health.circuit_opened_at = time.time()
            health.status = ProviderStatus.CIRCUIT_OPEN

        elif rate < DEGRADED_THRESHOLD:
            health.status = ProviderStatus.DEGRADED

        else:
            # If recovering from circuit open, require sustained recovery
            if prev_status == ProviderStatus.CIRCUIT_OPEN:
                recovery_rate = health.success_rate(window_seconds=RECOVERY_WINDOW)
                if recovery_rate >= RECOVERY_THRESHOLD:
                    health.status = ProviderStatus.HEALTHY
                    health.circuit_opened_at = None
                    logger.info(f"[health] RECOVERED provider={health.provider_name}")
                # else: stay in CIRCUIT_OPEN
            else:
                health.status = ProviderStatus.HEALTHY

        if prev_status != health.status:
            health.last_status_change = time.time()
            logger.info(
                f"[health] STATUS CHANGE provider={health.provider_name} "
                f"{prev_status} → {health.status}"
            )

    def get_status(self, provider_name: str) -> ProviderStatus:
        with self._lock:
            health = self._providers.get(provider_name)
            if health is None:
                return ProviderStatus.HEALTHY
            return health.status

    def get_health(self, provider_name: str) -> Optional[ProviderHealth]:
        with self._lock:
            return self._providers.get(provider_name)

    def get_all_health(self) -> dict[str, ProviderHealth]:
        with self._lock:
            return dict(self._providers)

    def set_operator_disabled(self, provider_name: str, disabled: bool) -> None:
        with self._lock:
            health = self._get_or_create(provider_name)
            health.operator_disabled = disabled
            if disabled:
                health.status = ProviderStatus.OPERATOR_DISABLED
            else:
                # Re-evaluate based on current metrics
                health.status = ProviderStatus.HEALTHY
                self._recompute_status(health)
            logger.info(
                f"[health] OPERATOR OVERRIDE provider={provider_name} disabled={disabled}"
            )

    def is_available(self, provider_name: str) -> bool:
        status = self.get_status(provider_name)
        return status in (ProviderStatus.HEALTHY, ProviderStatus.DEGRADED)

    def _get_or_create(self, provider_name: str) -> ProviderHealth:
        if provider_name not in self._providers:
            self._providers[provider_name] = ProviderHealth(provider_name=provider_name)
        return self._providers[provider_name]

    def snapshot(self) -> list[dict]:
        """Returns a serializable snapshot of all provider health for the dashboard."""
        with self._lock:
            result = []
            for name, health in self._providers.items():
                result.append({
                    "provider_name": name,
                    "status": health.status.value,
                    "success_rate": round(health.success_rate(), 4),
                    "consecutive_failures": health.consecutive_failures,
                    "operator_disabled": health.operator_disabled,
                    "avg_latency_ms": round(health.avg_latency_ms, 1),
                    "p50_latency_ms": round(health.p50_latency_ms, 1),
                    "p95_latency_ms": round(health.p95_latency_ms, 1),
                    "recent_failures": health.recent_failures(5),
                    "last_status_change": health.last_status_change,
                    "circuit_opened_at": health.circuit_opened_at,
                    "total_observations": len(health.outcomes),
                })
            return result
