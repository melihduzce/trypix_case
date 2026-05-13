"""
OpenRouter Provider — Synchronous pattern.

OpenRouter routes to various image generation models (Stable Diffusion, DALL-E, etc.)
via a single unified endpoint. The call is synchronous: POST → wait → get result.

Flow:
  1. POST /api/v1/chat/completions (with image generation model) → waits and returns result
     OR
  POST /api/v1/generation → direct image generation endpoint

We use the images generation endpoint for models that support it.

Latency profile: 5–30s typical depending on model and load.
Error semantics: 429 on rate limit, 402 on credit exhaustion, 5xx on server errors.
"""

import time
import httpx
import logging
from typing import Optional

from app.providers.base import (
    BaseProvider, GenerationRequest, GenerationResult,
    GenerationStatus, ProviderError, ProviderRateLimitError,
    ProviderTimeoutError, ProviderAuthError
)

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Use a fast, affordable image model available on OpenRouter
OPENROUTER_IMAGE_MODEL = "black-forest-labs/flux-1.1-pro"


class OpenRouterProvider(BaseProvider):
    """
    OpenRouter provider using synchronous request pattern.

    Sends a single POST request and awaits the response directly.
    No polling or webhook needed — OpenRouter blocks until generation completes.
    """

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 120.0,
        model: str = OPENROUTER_IMAGE_MODEL,
        site_url: str = "https://trypix.ai",
        site_name: str = "TRYPIX",
    ):
        super().__init__(name="openrouter", api_key=api_key, timeout_seconds=timeout_seconds)
        self.model = model
        self.site_url = site_url
        self.site_name = site_name
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self.site_url,
                    "X-Title": self.site_name,
                },
                timeout=self.timeout_seconds,
            )
        return self._client

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        start_time = time.monotonic()
        client = self._get_client()

        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "n": request.num_images,
            "size": f"{request.width}x{request.height}",
            **request.extra_params,
        }

        logger.info(f"[openrouter] Submitting synchronous request job_id={request.job_id} model={self.model}")

        try:
            response = await client.post(
                f"{OPENROUTER_BASE}/images/generations",
                json=payload,
            )
            self._raise_for_status(response)

            data = response.json()
            images = data.get("data", [])
            image_urls = []

            for img in images:
                url = img.get("url") or img.get("b64_json")
                if url:
                    image_urls.append(url)

            if not image_urls:
                raise ProviderError("OpenRouter returned no image URLs")

            latency_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                f"[openrouter] Request completed job_id={request.job_id} "
                f"latency={latency_ms:.0f}ms"
            )

            return GenerationResult(
                job_id=request.job_id,
                provider_name=self.name,
                status=GenerationStatus.COMPLETED,
                image_urls=image_urls,
                latency_ms=latency_ms,
                provider_job_id=data.get("id"),
            )

        except httpx.TimeoutException:
            raise ProviderTimeoutError(
                f"OpenRouter timed out after {self.timeout_seconds}s"
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"OpenRouter request failed: {e}")

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            response = await client.get(
                f"{OPENROUTER_BASE}/models",
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"[openrouter] Health check failed: {e}")
            return False

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ProviderRateLimitError("OpenRouter rate limit exceeded")
        elif response.status_code == 402:
            raise ProviderError(
                "OpenRouter credit exhausted",
                is_retryable=False,
                status_code=402,
            )
        elif response.status_code in (401, 403):
            raise ProviderAuthError(f"OpenRouter auth failed: {response.status_code}")
        elif response.status_code >= 500:
            raise ProviderError(
                f"OpenRouter server error: {response.status_code}",
                is_retryable=True,
                status_code=response.status_code,
            )
        elif response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise ProviderError(
                f"OpenRouter client error {response.status_code}: {detail}",
                is_retryable=False,
                status_code=response.status_code,
            )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
