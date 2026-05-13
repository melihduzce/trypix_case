"""
FAL.ai Provider — Polling-based async pattern.

Flow:
  1. POST /fal-run/{model} → returns request_id immediately
  2. GET /requests/{request_id}/status → poll until status == COMPLETED
  3. GET /requests/{request_id} → fetch final result with image URLs

Latency profile: 10–45s typical, up to 120s under load.
Error semantics: 429 on rate limit, 5xx on server errors, timeout on hung jobs.
"""

import asyncio
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

FAL_QUEUE_BASE = "https://queue.fal.run"
FAL_MODEL = "fal-ai/flux/schnell"
POLL_INTERVAL_SECONDS = 2.0


class FalProvider(BaseProvider):
    """
    FAL.ai provider using the queue/polling pattern.

    Submits a job to FAL's queue, then polls the status endpoint
    until the job completes or timeout is reached.
    """

    def __init__(self, api_key: str, timeout_seconds: float = 120.0, model: str = FAL_MODEL):
        super().__init__(name="fal", api_key=api_key, timeout_seconds=timeout_seconds)
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Key {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        start_time = time.monotonic()
        client = self._get_client()

        # Step 1: Submit job to FAL queue
        try:
            submit_url = f"{FAL_QUEUE_BASE}/{self.model}"
            payload = {
                "prompt": request.prompt,
                "image_size": {
                    "width": request.width,
                    "height": request.height,
                },
                "num_images": request.num_images,
                **request.extra_params,
            }

            logger.info(f"[fal] Submitting job for request_id={request.job_id}")
            response = await client.post(submit_url, json=payload)
            self._raise_for_status(response)

            data = response.json()
            fal_request_id = data.get("request_id")
            if not fal_request_id:
                raise ProviderError("FAL did not return a request_id")

            logger.info(f"[fal] Job submitted fal_request_id={fal_request_id}")

        except httpx.TimeoutException:
            raise ProviderTimeoutError("FAL submission timed out")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"FAL submission failed: {e}")

        # Step 2: Poll until complete or timeout
        status_url = f"https://queue.fal.run/requests/{fal_request_id}/status"
        result_url = f"https://queue.fal.run/requests/{fal_request_id}"

        while True:
            elapsed = (time.monotonic() - start_time) * 1000
            if elapsed / 1000 > self.timeout_seconds:
                raise ProviderTimeoutError(
                    f"FAL job {fal_request_id} timed out after {self.timeout_seconds}s"
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            try:
                status_response = await client.get(status_url)
                self._raise_for_status(status_response)
                status_data = status_response.json()
                status = status_data.get("status", "")

                logger.debug(f"[fal] Poll fal_request_id={fal_request_id} status={status}")

                if status == "COMPLETED":
                    break
                elif status in ("FAILED", "CANCELLED"):
                    error_msg = status_data.get("error", {}).get("message", "Unknown FAL error")
                    raise ProviderError(f"FAL job failed: {error_msg}", is_retryable=True)
                # IN_QUEUE or IN_PROGRESS → keep polling

            except httpx.TimeoutException:
                logger.warning(f"[fal] Poll timeout for {fal_request_id}, retrying...")
                continue
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"FAL polling failed: {e}")

        # Step 3: Fetch result
        try:
            result_response = await client.get(result_url)
            self._raise_for_status(result_response)
            result_data = result_response.json()

            images = result_data.get("images", [])
            image_urls = [img.get("url") for img in images if img.get("url")]

            if not image_urls:
                raise ProviderError("FAL returned no image URLs")

            latency_ms = (time.monotonic() - start_time) * 1000
            logger.info(f"[fal] Job completed fal_request_id={fal_request_id} latency={latency_ms:.0f}ms")

            return GenerationResult(
                job_id=request.job_id,
                provider_name=self.name,
                status=GenerationStatus.COMPLETED,
                image_urls=image_urls,
                latency_ms=latency_ms,
                provider_job_id=fal_request_id,
            )

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"FAL result fetch failed: {e}")

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            # Lightweight: just check if queue endpoint responds
            response = await client.get(
                f"{FAL_QUEUE_BASE}/{self.model}",
                timeout=5.0
            )
            # 405 Method Not Allowed means the endpoint exists (we sent GET to a POST endpoint)
            return response.status_code in (200, 405, 422)
        except Exception as e:
            logger.warning(f"[fal] Health check failed: {e}")
            return False

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ProviderRateLimitError("FAL rate limit exceeded")
        elif response.status_code in (401, 403):
            raise ProviderAuthError(f"FAL auth failed: {response.status_code}")
        elif response.status_code >= 500:
            raise ProviderError(
                f"FAL server error: {response.status_code}",
                is_retryable=True,
                status_code=response.status_code,
            )
        elif response.status_code >= 400:
            raise ProviderError(
                f"FAL client error: {response.status_code} — {response.text}",
                is_retryable=True,
                status_code=response.status_code,
            )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()