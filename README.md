# TRYPIX — Multi-Provider Generation Service with Smart Failover

A production-grade backend service that fronts multiple AI image generation providers behind a single internal API, with automatic failover, health-based routing, and an operator dashboard.

## Live URLs

- **Backend API**: `https://trypix-case-1.onrender.com`
- **Operator Dashboard**: `https://trypix-dashboard.onrender.com`
- **API Docs**: `https://trypix-case-1.onrender.com/docs`

---

## Provider Abstraction Approach

All providers implement the `BaseProvider` abstract class (`app/providers/base.py`):

```python
class BaseProvider(ABC):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
    async def health_check(self) -> bool: ...
```

Each provider encapsulates its own async pattern internally. The routing engine, failover logic, and health tracker **only interact with `BaseProvider`** — they have no knowledge of how a provider works internally.

Adding a third provider requires only:
1. Creating a new file in `app/providers/`
2. Implementing `generate()` and `health_check()`
3. Registering it in `app/main.py`

**No changes** to routing, failover, or health logic.

---

## Providers & Async Patterns

### FAL.ai — Polling Pattern
**File:** `app/providers/fal_provider.py`

FAL uses an asynchronous queue model. The caller never blocks on the generation itself:

1. `POST https://queue.fal.run/fal-ai/flux/schnell` → returns `request_id`, `status_url`, `response_url` immediately
2. `GET {status_url}` → poll every 3 seconds until `status == COMPLETED`
3. `GET {response_url}` → fetch final image URLs

The submit response includes convenience URLs (`status_url`, `response_url`) which are used directly rather than constructing them manually — this makes the implementation resilient to FAL changing their URL structure.

Latency: 10–45s typical.

### OpenRouter — Synchronous Pattern
**File:** `app/providers/openrouter_provider.py`

OpenRouter image generation uses the Chat Completions endpoint with `modalities: ["image", "text"]`. The HTTP call blocks until generation completes — no polling or webhook needed:

1. `POST https://openrouter.ai/api/v1/chat/completions` with `modalities: ["image", "text"]` → blocks → returns base64-encoded image in `choices[0].message.images`

Model used: `google/gemini-2.5-flash-image` (Nano Banana, GA release).

Note: OpenRouter does **not** use the standard `/v1/images/generations` endpoint. Image output is embedded in the chat completion response as `message.images[].image_url.url`.

Latency: 5–30s typical.

### How the abstraction unifies them

Both patterns resolve to the same `GenerationResult` type. From the routing engine's perspective, calling `await provider.generate(request)` always returns a completed result — whether that required 12 polling cycles internally or a single blocking HTTP call is invisible to the caller.

---

## External Interface Contract

The service exposes a **job-id polling** interface to callers:

```
POST /api/v1/generate       → { job_id, status: "pending" }   (returns immediately)
GET  /api/v1/jobs/{job_id}  → { status, provider_used, image_urls, latency_ms }
```

This design avoids HTTP timeout issues on long-running generations (FAL can take 45s+) while remaining simple for callers to implement. The caller polls until `status == "completed"` or `"failed"`.

---

## Failover Trigger Criteria

| Trigger | Action | Rationale |
|---------|--------|-----------|
| `ProviderRateLimitError` (HTTP 429) | Failover to next provider | Rate limits are per-provider; another provider may have capacity |
| `ProviderTimeoutError` | Failover | A hung provider should not block the user indefinitely |
| `ProviderError` with `is_retryable=True` | Failover | Transient server errors (5xx, unexpected responses) may succeed elsewhere |
| Unexpected exceptions | Failover | Defensive — unknown errors are treated as transient |
| `ProviderAuthError` (HTTP 401/403) | **Fail immediately, no failover** | Auth errors indicate misconfiguration, not transience. Retrying on another provider would mask the root cause and waste budget |
| `ProviderError` with `is_retryable=False` | **Fail immediately, no failover** | Client errors (e.g. malformed request) will not improve by retrying elsewhere |

---

## Health-Tracking Algorithm

**Algorithm:** Time-based sliding window (60 seconds)

**Implementation:** `app/router/health_tracker.py`

```
success_rate = successful_outcomes / total_outcomes  (within last 60 seconds)
```

### Thresholds

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Window size | 60s | Long enough to smooth noise, short enough to react to outages within a minute |
| `DEGRADED` threshold | success_rate < 0.70 | Provider is struggling but still usable as a last-resort fallback |
| `CIRCUIT_OPEN` threshold | success_rate < 0.40 | Failing more than half the time — skip entirely |
| Consecutive failure limit | 3 | Immediate circuit open without waiting for the window to fill. Catches sudden outages that would otherwise take ~60s to register |
| Recovery threshold | success_rate ≥ 0.80 over 30s | Requires *sustained* recovery before trusting the provider again — prevents flapping between CIRCUIT_OPEN and HEALTHY |
| Min observations | 3 | Avoid penalising providers with very few data points (e.g. just started receiving traffic) |

### Routing priority

1. Skip `OPERATOR_DISABLED` and `CIRCUIT_OPEN` providers
2. `HEALTHY` providers first
3. `DEGRADED` providers as fallback
4. Within each tier: sort by `avg_latency_ms` ascending (prefer faster providers)

### Why sliding window over alternatives

- **vs. simple counters**: Counters accumulate state forever. A provider that had 100 failures an hour ago but recovered would still look bad.
- **vs. exponential smoothing (EMA)**: EMA is harder to reason about when debugging. The sliding window is transparent: "70% of calls in the last 60 seconds succeeded."
- **vs. count-based windows**: A count-based window (last N requests) doesn't decay naturally with time. A provider with no traffic for 10 minutes could have a stale health score.

---

## Real vs. Mocked Providers

**Both providers are real.** TRYPIX provided API tokens for FAL.ai ($10 budget) and OpenRouter ($5 budget), so there is no reason to mock.

Using real APIs provides stronger guarantees than mocks:

- **Async patterns are genuinely different**: FAL's polling and OpenRouter's synchronous pattern behave differently under real network conditions — latency variance, partial failures, and rate limits cannot be fully replicated by mocks.
- **Error semantics are authentic**: Real 429s, real timeouts, and real malformed responses exercise the failover and health-tracking logic in ways that matter in production.
- **The circuit breaker has been exercised in production**: During development, FAL's polling endpoint returned 405s (incorrect URL construction) which triggered real failovers to OpenRouter, validating the end-to-end failover path.

If the API budget runs out during evaluation, the circuit breaker will open on the affected provider and route all traffic to the other — which is exactly the health-tracking behaviour the system is designed to demonstrate.

---

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
uvicorn app.main:app --reload

# Dashboard
cd dashboard && npm install && npm run dev
```

## Tests

```bash
python -m pytest tests/ -v
```

| Test | Scenario |
|------|----------|
| `test_successful_primary_call` | Happy path — primary provider succeeds |
| `test_primary_failure_fallback_to_secondary` | Primary throws retryable error → routes to secondary |
| `test_health_triggered_demotion` | 3 consecutive failures → CIRCUIT_OPEN → skipped by router |
| `test_operator_override_takes_provider_out_of_rotation` | Operator disables provider → not routed |
| `test_all_providers_unavailable` | All circuits open → graceful FAILED result |
| `test_rate_limit_triggers_failover` | 429 from primary → failover |
| `test_auth_error_does_not_failover` | 401 from primary → immediate failure, no failover |
| `test_health_tracker_sliding_window` | Old failures outside window don't affect current health |

---

## Deployment (Render)

Backend and dashboard are both deployed on Render.

- **Backend**: Web Service, Python, `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Dashboard**: Static Site, `dashboard/` directory, `npm install && npm run build`

Environment variables required: `FAL_API_KEY`, `OPENROUTER_API_KEY`, `DB_PATH`

---

## Optional Bonus: Production-Scale Failover Discussion

### Observation persistence across worker restarts

Currently health state lives in memory. On restart, all providers start at `HEALTHY`. In production:

- `generation_events` are already persisted to SQLite via `EventStore`
- On startup, replay the last 60 seconds of events from the DB to warm the `HealthTracker`
- This gives continuity across restarts without distributed state

### Cross-instance health sharing in multi-worker deployment

With multiple workers, each has its own `HealthTracker`. A failure seen by worker 1 doesn't affect worker 2.

Solutions in increasing complexity:
1. **DB-based health** (simplest): Compute health scores from `generation_events` at query time. Workers share the DB so routing is consistent. Adds ~1ms per request.
2. **Redis pub/sub**: Workers publish failure events; all workers subscribe and update in-memory state. Low latency, eventually consistent.
3. **Shared memory** (single host): Use `multiprocessing.Manager` for shared health state across workers on the same machine.

### Sharding by user ID or flow type

If sharded by user ID, each shard may use different API keys — a rate limit on one shard's key doesn't affect others. Health state should be per-shard, not global.

If sharded by flow type, different flows may have different provider preferences (e.g. one flow always prefers FAL for style consistency). This requires per-flow provider priority lists in `RoutingEngine`, with shared health state but independent routing decisions per flow type.
