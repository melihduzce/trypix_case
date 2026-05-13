"""
OpenRouter Provider — Synchronous via Chat Completions with image modality.

Flow:
  POST /api/v1/chat/completions with modalities=["image","text"] → returns base64 image
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
OPENROUTER_IMAGE_MODEL = "google/gemini-2.5-flash-image"


class OpenRouterProvider(BaseProvider):
    def __init__(self, api_key: str, timeout_seconds: float = 120.0,
                 model: str = OPENROUTER_IMAGE_MODEL,
                 site_url: str = "https://trypix.ai", site_name: str = "TRYPIX"):
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
            "messages": [{"role": "user", "content": request.prompt}],
            "modalities": ["image", "text"],
        }

        logger.info(f"[openrouter] Submitting job_id={request.job_id} model={self.model}")

        try:
            response = await client.post(f"{OPENROUTER_BASE}/chat/completions", json=payload)
            self._raise_for_status(response)
            data = response.json()

            image_urls = []
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                # images field
                for img in msg.get("images", []):
                    url = img if isinstance(img, str) else img.get("url") or img.get("data")
                    if url:
                        image_urls.append(url)
                # content list parts
                content = msg.get("content", "")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url")
                            if url:
                                image_urls.append(url)

            if not image_urls:
                logger.error(f"[openrouter] No images in response: {str(data)[:500]}")
                raise ProviderError("OpenRouter returned no images", is_retryable=True)

            latency_ms = (time.monotonic() - start_time) * 1000
            logger.info(f"[openrouter] Done job_id={request.job_id} latency={latency_ms:.0f}ms")

            return GenerationResult(
                job_id=request.job_id,
                provider_name=self.name,
                status=GenerationStatus.COMPLETED,
                image_urls=image_urls,
                latency_ms=latency_ms,
                provider_job_id=data.get("id"),
            )

        except httpx.TimeoutException:
            raise ProviderTimeoutError(f"OpenRouter timed out after {self.timeout_seconds}s")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"OpenRouter request failed: {e}")

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            response = await client.get(f"{OPENROUTER_BASE}/models", timeout=5.0)
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"[openrouter] Health check failed: {e}")
            return False

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ProviderRateLimitError("OpenRouter rate limit exceeded")
        elif response.status_code == 402:
            raise ProviderError("OpenRouter credit exhausted", is_retryable=False, status_code=402)
        elif response.status_code in (401, 403):
            raise ProviderAuthError(f"OpenRouter auth failed: {response.status_code}")
        elif response.status_code >= 500:
            raise ProviderError(f"OpenRouter server error: {response.status_code}", is_retryable=True, status_code=response.status_code)
        elif response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:200])
            except Exception:
                detail = response.text[:200]
            raise ProviderError(f"OpenRouter error {response.status_code}: {detail}", is_retryable=True, status_code=response.status_code)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()