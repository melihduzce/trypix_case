# TRYPIX — Multi-Provider Generation Service with Smart Failover

A production-grade backend service that fronts multiple AI image generation providers behind a single internal API, with automatic failover, health-based routing, and an operator dashboard.

## Live URLs

- **Backend API**: `https://trypix-backend.onrender.com`
- **Operator Dashboard**: `https://trypix-dashboard.onrender.com`
- **API Docs**: `https://trypix-backend.onrender.com/docs`

---

## Provider Abstraction Approach

All providers implement the `BaseProvider` abstract class (`app/providers/base.py`):

```python
class BaseProvider(ABC):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
    async def health_check(self) -> bool: ...
```

Each provider encapsulates its own async pattern internally. The routing engine, failover logic, and health tracker **only interact with `BaseProvider`** — they have no knowledge of how a provider works internally. Adding a third provider requires only:
1. Creating a new file in `app/providers/`
2. Implementing `generate()` and `health_check()`
3. Registering it in `app/main.py`

No changes to routing, failover, or health logic.

---

## Providers & Async Patterns

### FAL.ai — Polling Pattern
**File:** `app/providers/fal_provider.py`

1. `POST /fal-run/{model}` → returns `request_id` immediately
2. `GET /requests/{request_id}/status` → poll every 2s until `COMPLETED`
3. `GET /requests/{request_id}` → fetch final image URLs

Latency: 10–45s typical. The polling loop runs inside `generate()`, making it invisible to the caller.

### OpenRouter — Synchronous Pattern
**File:** `app/providers/openrouter_provider.py`

1. `POST /api/v1/images/generations` → blocks until generation completes, returns image URLs directly

Latency: 5–30s typical. Simplest pattern — single HTTP call with a long timeout.

### How the abstraction unifies them
Both patterns resolve to the same return type (`GenerationResult`) with the same fields. The caller receives a completed result regardless of how the provider handled the async work internally.

---

## Failover Trigger Criteria

| Trigger | Action | Rationale |
|---------|--------|-----------|
| Any `ProviderError` with `is_retryable=True` | Failover to next provider | Transient errors may succeed elsewhere |
| `ProviderRateLimitError` (HTTP 429) | Failover | Rate limits are per-provider, not global |
| `ProviderTimeoutError` | Failover | Hung providers should not block the user |
| Unexpected exceptions | Failover | Defensive — unknown errors treated as transient |
| `ProviderAuthError` (HTTP 401/403) | **No failover, fail immediately** | Auth errors indicate misconfiguration, not transience — retrying wastes time and budget |
| `ProviderError` with `is_retryable=False` | **No failover** | Client errors (4xx) won't improve on retry |

---

## Health-Tracking Algorithm

**Algorithm:** Time-based sliding window (60 seconds)

**Implementation:** `app/router/health_tracker.py`

```
success_rate = successful_outcomes / total_outcomes in last 60s
```

### Thresholds

| Threshold | Value | Rationale |
|-----------|-------|-----------|
| `DEGRADED` | success_rate < 0.70 | Provider is struggling but still usable as fallback |
| `CIRCUIT_OPEN` | success_rate < 0.40 | Provider is failing more than half the time — skip it |
| Consecutive failure limit | 3 | Immediate circuit open without waiting for the window to fill — catches sudden outages |
| `RECOVERY_THRESHOLD` | success_rate ≥ 0.80 over 30s | Requires sustained recovery before trusting the provider again (avoids flapping) |
| `MIN_OBSERVATIONS` | 3 | Don't penalize providers with very few data points |

### Why sliding window over alternatives
- **vs. simple counters**: Counters accumulate state forever. A provider that had 100 failures an hour ago but is healthy now would still look bad with counters.
- **vs. exponential smoothing (EMA)**: EMA is harder to reason about when debugging. The sliding window is transparent: "70% of calls in the last 60 seconds succeeded."
- **vs. count-based windows**: A count-based window (last N requests) doesn't decay naturally with time. A provider that has had no traffic for 10 minutes could have a stale health score.

### Routing priority
1. `OPERATOR_DISABLED` → skip
2. `CIRCUIT_OPEN` → skip
3. `HEALTHY` → preferred
4. `DEGRADED` → used as fallback
5. Within each tier, sort by `avg_latency_ms` ascending (prefer faster providers)

---

## Real vs. Mocked Providers

**Both providers are real.** TRYPIX was provided API tokens for FAL.ai and OpenRouter.ai, so there is no reason to mock.

Using real APIs means:
- The async patterns are genuinely different (polling vs. synchronous)
- Latency profiles reflect real-world conditions
- Error semantics (rate limits, server errors) are authentic
- The failover logic is exercised against real provider behavior

If an API token runs out of credits during testing, the circuit breaker will open on that provider and the service will route all traffic to the other — which is exactly the behavior the health-tracking system is designed to produce.

---

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

### Test scenarios covered

| Test | Scenario |
|------|----------|
| `test_successful_primary_call` | Happy path — primary provider succeeds |
| `test_primary_failure_fallback_to_secondary` | Primary throws retryable error → routes to secondary |
| `test_health_triggered_demotion` | 3 consecutive failures → CIRCUIT_OPEN → skipped by router |
| `test_operator_override_takes_provider_out_of_rotation` | Operator disables provider → OPERATOR_DISABLED → not routed |
| `test_all_providers_unavailable` | All circuits open → graceful FAILED result, no exception |
| `test_rate_limit_triggers_failover` | 429 from primary → failover |
| `test_auth_error_does_not_failover` | 401 from primary → immediate failure, no failover |
| `test_health_tracker_sliding_window` | Old failures outside window don't affect current health |

---

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your API keys

# Run backend
uvicorn app.main:app --reload

# Run dashboard (separate terminal)
cd dashboard
npm install
npm run dev
```

---

## Deployment (Render)

1. Push repository to GitHub
2. Create a **Web Service** on Render pointing to this repo
3. Set environment variables: `FAL_API_KEY`, `OPENROUTER_API_KEY`
4. Create a **Static Site** on Render pointing to the `dashboard/` directory
   - Build command: `npm install && npm run build`
   - Publish directory: `dist`
   - Set `VITE_API_URL` to the backend URL

---

## Optional Bonus: Production-Scale Failover Discussion

### Observation persistence across worker restarts

Currently, health state lives in memory (`HealthTracker`). On restart, all providers start at `HEALTHY`. In production:

- Persist `generation_events` to the database (already done via `EventStore`)
- On startup, replay the last 60s of events from the DB to warm the health tracker
- This gives continuity across restarts without complex distributed state

### Cross-instance health sharing in multi-worker deployment

With multiple workers (e.g. Gunicorn with 4 workers), each worker has its own `HealthTracker` instance. A failure seen by worker 1 doesn't affect worker 2's routing decision.

Solutions in increasing complexity:
1. **DB-based health**: Compute health scores from `generation_events` at query time (already possible with the current schema). Workers share the DB, so all routing decisions are based on the same data. Adds ~1ms per request for the health query.
2. **Redis pub/sub**: Workers publish failure events to a Redis channel; all workers subscribe and update their in-memory state. Low latency, eventually consistent.
3. **Shared memory (single host)**: If all workers run on the same machine, use a shared memory segment (e.g. `multiprocessing.Manager`) for the health state.

### Sharding by user ID or flow type

If the service is sharded by user ID, each shard needs its own health state — a provider that is rate-limited for user group A may be healthy for user group B (if the rate limit is per-API-key and different keys are used per shard).

If sharded by flow type (e.g. "portrait generation" vs "landscape generation"), different flows may have different provider preferences — e.g. one flow always prefers FAL for its style, regardless of latency. This would require per-flow provider priority lists in `RoutingEngine`, with shared health state but independent routing decisions.
