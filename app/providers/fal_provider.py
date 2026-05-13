"""
FAL.ai Provider — Polling-based async pattern.

Flow:
  1. POST https://queue.fal.run/fal-ai/flux/schnell  → returns request_id + status_url + response_url
  2. GET {status_url}   → poll until status == COMPLETED
  3. GET {response_url} → fetch final result with image URLs
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
POLL_INTERVAL_SECONDS = 3.0


class FalProvider(BaseProvider):
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

        # Step 1: Submit job
        try:
            submit_url = f"{FAL_QUEUE_BASE}/{self.model}"
            payload = {
                "prompt": request.prompt,
                "image_size": {"width": request.width, "height": request.height},
                "num_images": request.num_images,
                **request.extra_params,
            }
            logger.info(f"[fal] Submitting job_id={request.job_id}")
            response = await client.post(submit_url, json=payload)
            self._raise_for_status(response)

            data = response.json()
            fal_request_id = data.get("request_id")
            # Use URLs returned by FAL directly
            status_url = data.get("status_url") or f"{FAL_QUEUE_BASE}/{self.model}/requests/{fal_request_id}/status"
            response_url = data.get("response_url") or f"{FAL_QUEUE_BASE}/{self.model}/requests/{fal_request_id}"

            if not fal_request_id:
                raise ProviderError("FAL did not return a request_id")

            logger.info(f"[fal] Submitted fal_request_id={fal_request_id} status_url={status_url}")

        except httpx.TimeoutException:
            raise ProviderTimeoutError("FAL submission timed out")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"FAL submission failed: {e}")

        # Step 2: Poll status
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed > self.timeout_seconds:
                raise ProviderTimeoutError(f"FAL job {fal_request_id} timed out after {self.timeout_seconds}s")

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
                    error_msg = status_data.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", "FAL job failed")
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
            result_response = await client.get(response_url)
            self._raise_for_status(result_response)
            result_data = result_response.json()

            images = result_data.get("images", [])
            image_urls = [img.get("url") for img in images if img.get("url")]

            if not image_urls:
                raise ProviderError("FAL returned no image URLs")

            latency_ms = (time.monotonic() - start_time) * 1000
            logger.info(f"[fal] Completed fal_request_id={fal_request_id} latency={latency_ms:.0f}ms")

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
            response = await client.get(f"{FAL_QUEUE_BASE}/{self.model}", timeout=5.0)
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
            raise ProviderError(f"FAL server error: {response.status_code}", is_retryable=True, status_code=response.status_code)
        elif response.status_code >= 400:
            raise ProviderError(f"FAL client error: {response.status_code} — {response.text[:200]}", is_retryable=True, status_code=response.status_code)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()