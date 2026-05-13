"""
Tüm görsel üretim sağlayıcıları için soyut temel sınıf (abstract base class).
Yeni bir sağlayıcı eklemek için sadece bu arayüzü implemente etmek yeterli —
routing veya sağlık takibi (health tracking) mantığında hiçbir değişiklik yapmaya gerek yok.

"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CIRCUIT_OPEN = "circuit_open"
    OPERATOR_DISABLED = "operator_disabled"


class GenerationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class GenerationRequest:
    job_id: str
    prompt: str
    width: int = 1024
    height: int = 1024
    num_images: int = 1
    extra_params: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    job_id: str
    provider_name: str
    status: GenerationStatus
    image_urls: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    latency_ms: Optional[float] = None
    provider_job_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class ProviderError(Exception):
    """Base error for all provider failures."""
    def __init__(self, message: str, is_retryable: bool = True, status_code: Optional[int] = None):
        super().__init__(message)
        self.is_retryable = is_retryable
        self.status_code = status_code


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, is_retryable=True, status_code=429)


class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str = "Provider timed out"):
        super().__init__(message, is_retryable=True)


class ProviderAuthError(ProviderError):
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, is_retryable=False, status_code=401)


class BaseProvider(ABC):
    """
    Tüm görsel üretim sağlayıcıları için birleşik (tek tip) arayüz.

    Her somut sağlayıcı bu arayüzü implemente eder. Böylece kendine özel async çalışma şeklini (polling, webhook, senkron)
    routing katmanından gizlemiş olur.
    """

    def __init__(self, name: str, api_key: str, timeout_seconds: float = 120.0):
        self.name = name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """
        Bir üretim isteği gönderir ve tamamlanmış sonucu döndürür.

        Her sağlayıcı kendi async çalışma şeklini içeride halletmek zorundadır:

        Polling yapan sağlayıcılar: isteği gönder + bitene kadar durumu sorgula

        Webhook kullanan sağlayıcılar: isteği gönder + geri dönüşü bekle

        Senkron çalışan sağlayıcılar: isteği gönder + cevabı bekle

        Hata durumları (fırlattığı exception'lar):

        ProviderRateLimitError: 429 (rate limit) cevabı geldiğinde

        ProviderTimeoutError: timeout süresi aşıldığında

        ProviderAuthError: 401/403 (yetkilendirme hatası) cevabı geldiğinde

        ProviderError: diğer tüm hatalarda
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Sağlayıcıya erişilip erişilemediğini kontrol eden hafif bir sağlık kontrolü.
        Eğer sağlıklıysa True, değilse False döner.
        """
        ...

    def __repr__(self) -> str:
        return f"<Provider:{self.name}>"
