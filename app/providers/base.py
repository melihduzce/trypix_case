"""
Abstract base class for all image generation providers.
Adding a new provider only requires implementing this interface —
no changes needed in routing or health-tracking logic.
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
    Unified interface for all image generation providers.

    Each concrete provider implements this interface, hiding its specific
    async pattern (polling, webhook, synchronous) from the routing layer.
    """

    def __init__(self, name: str, api_key: str, timeout_seconds: float = 120.0):
        self.name = name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """
        Submit a generation request and return a completed result.

        Implementations must handle their own async pattern internally:
        - Polling providers: submit + poll until done
        - Webhook providers: submit + wait for callback
        - Synchronous providers: submit + await response

        Raises:
            ProviderRateLimitError: on 429 responses
            ProviderTimeoutError: when timeout_seconds exceeded
            ProviderAuthError: on 401/403 responses
            ProviderError: on other failures
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Lightweight check to verify the provider is reachable.
        Returns True if healthy, False otherwise.
        """
        ...

    def __repr__(self) -> str:
        return f"<Provider:{self.name}>"
