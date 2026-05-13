# Day 10 Reliability Final Report

## 1. Architecture Summary

The gateway uses a production-style reliability layer in front of simulated LLM providers. Each request first checks the cache, then routes through provider-specific circuit breakers, then falls back to backup providers, and finally returns a static degraded-service message if every provider is unavailable.

```text
User
  ↓
ReliabilityGateway
  ↓
[Cache check: exact/similarity + privacy and false-hit guardrails]
  ↓ cache miss
[Circuit breaker per provider]
  ↓
Primary Provider
  ↓ if failed/open
Backup Provider
  ↓ if failed/open
Static Fallback
  ↓
Metrics: latency, availability, cache hit rate, fallback success, circuit opens, recovery time, cost saved
```

## 2. Configuration Table

| Setting | Value | Why this value |
|---|---:|---|
| failure_threshold | 3 | Low enough to detect repeated failures quickly, high enough to avoid false opens from one-off jitter. |
| reset_timeout_seconds | 2 | Short enough for repeatable lab recovery tests while still showing OPEN → HALF_OPEN behavior. |
| success_threshold | 1 | One successful HALF_OPEN probe is acceptable for this deterministic simulated-provider lab. |
| cache TTL | 300 seconds | Five-minute freshness window for FAQ-style prompts while limiting stale cache risk. |
| similarity_threshold | 0.92 | Conservative threshold to reduce wrong semantic cache hits on date-sensitive prompts. |
| load_test.requests | 200 | Meets the README requirement for a stronger load test. |
| load_test.concurrency | 10 | Exercises the gateway under concurrent request load instead of only sequential execution. |
| cache backend | memory / redis | Memory is fast for one process; Redis demonstrates shared cache for multi-instance deployment. |
| Redis prefix | rl:* | Namespaces lab cache entries and makes Redis evidence easy to inspect. |

## 3. Metrics Table

| Metric | Value |
|---|---:|
| total_requests | 1002 |
| availability | 0.8004 |
| error_rate | 0.1996 |
| latency_p50_ms | 0.1000 |
| latency_p95_ms | 306.7900 |
| latency_p99_ms | 508.6700 |
| fallback_success_rate | 0.2883 |
| cache_hit_rate | 0.5978 |
| circuit_open_count | 4 |
| recovery_time_ms | 2855.3808 |
| estimated_cost | 0.0984 |
| estimated_cost_saved | 0.5990 |
The aggregate availability is lower because the simulation intentionally includes the `all_providers_down` chaos scenario, which validates static fallback behavior under total provider failure.
## 4. Chaos Scenario Table

| Scenario | Expected behavior | Observed status |
|---|---|---|
| primary_timeout_100 | Primary provider fails 100%; circuit opens and backup provider serves traffic. | pass |
| primary_flaky_50 | Primary provider fails intermittently; circuit opens/recover and fallback protects availability. | pass |
| cache_stale_candidate | Low-threshold cache test; similar prompts with different years must not create false cache hits. | pass |
| cache_stale_candidate:cache_false_hit_guardrail | False-hit guardrail records and blocks the 2024 vs 2026 cache candidate. | pass |
| all_providers_down | All providers fail; gateway returns static fallback instead of crashing. | pass |
| all_healthy | Primary provider is healthy; availability should be high with minimal fallback. | pass |
### Example Circuit Transition Evidence

```text
closed -> open: failure_threshold
open -> half_open: reset_timeout_elapsed
half_open -> closed: probe_success

## 5. Cache Comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 226.3000 | 0.1000 | -226.2000 (-99.96%) |
| latency_p95_ms | 470.8700 | 306.7900 | -164.0800 (-34.85%) |
| latency_p99_ms | 520.1100 | 508.6700 | -11.4400 (-2.20%) |
| estimated_cost | 0.3820 | 0.0984 | -0.2836 (-74.23%) |
| estimated_cost_saved | 0.0000 | 0.5990 | 0.5990 |
| cache_hit_rate | 0.0000 | 0.5978 | 0.5978 |

The cache uses exact-match lookup first, then deterministic similarity scoring. Privacy-sensitive prompts and date/year mismatches are bypassed to prevent unsafe false hits.

| Cache guardrail | Evidence |
|---|---|
| Privacy bypass | Queries containing account, user, password, card, token, or other sensitive terms are not stored or returned. |
| False-hit prevention | `refund policy for 2024` and `refund policy for 2026` are detected as different-intent prompts despite high textual similarity. |
| TTL | Entries expire automatically after the configured TTL. |
| Exact-match fast path | Identical prompts return immediately with score `1.0`. |

## 6. Redis Shared Cache

`SharedRedisCache` stores each response as a Redis Hash with fields `query`, `response`, `metadata`, and `created_at`, then applies `EXPIRE` for TTL cleanup. It supports exact lookup by query hash and similarity lookup with `SCAN`, so separate gateway instances can share cached responses in horizontally scaled deployments.

Shared cache matters because production gateways usually run as multiple instances behind a load balancer. If each instance only had its own in-memory cache, cache hits would be inconsistent. Redis gives all instances one shared cache state.

Evidence command:

```bash
docker compose exec redis redis-cli KEYS 'rl:*'
```

Redis CLI output captured in `reports/redis_keys.txt`:

```text
﻿rl:all_healthy:095946136fea
rl:all_healthy:9e413fd814eb
rl:all_healthy:8baa2cfa11fa
rl:primary_timeout_100:095946136fea
rl:cache_stale_candidate:b6af19a70a20
rl:all_providers_down:b2a52f7dc795
rl:primary_timeout_100:b2a52f7dc795
rl:cache_stale_candidate:9e413fd814eb
rl:cache_stale_candidate:095946136fea
rl:primary_timeout_100:9e413fd814eb
rl:all_providers_down:9e413fd814eb
rl:primary_flaky_50:8baa2cfa11fa
rl:primary_flaky_50:9e413fd814eb
rl:primary_flaky_50:095946136fea
rl:cache_stale_candidate:8baa2cfa11fa
rl:cache_stale_candidate:cccf278bceae
rl:all_healthy:b2a52f7dc795
rl:primary_flaky_50:b2a52f7dc795
rl:primary_timeout_100:8baa2cfa11fa
```

Redis test evidence:

```text
pytest -q tests/test_redis_cache.py
Expected: all Redis cache tests pass when Docker Redis is running.
```
The Redis test suite verifies shared state by creating two separate `SharedRedisCache` instances connected to the same Redis backend and confirming that one instance can read an entry written by the other.
## 7. Failure Analysis

Remaining weakness: circuit breaker state is still local to each Python process. In a real multi-instance deployment, one instance could open a circuit while another instance continues sending traffic to the failing provider because the breaker counters are not shared.

Production fix: move circuit breaker state to Redis or another shared state store using atomic counters, expirations, and provider-level health keys. This would make provider health consistent across all gateway instances.

Another production risk is that the current semantic cache uses deterministic local similarity rather than embeddings. This is safer and reproducible for the lab, but in production it may miss valid paraphrases. A future version could use embeddings with stricter policy checks and audit logging.

## 8. Next Steps

1. Store circuit breaker counters and state transitions in Redis so provider health is shared across horizontally scaled gateway instances.
2. Add Prometheus metrics such as `agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, and `circuit_state`.
3. Add cost-aware routing so traffic shifts to cheaper providers after budget thresholds are reached.
4. Add property-based tests for circuit breaker transitions under randomized failure and recovery sequences.

## 9. Reproducibility Commands

```bash
pip install -e ".[dev]"
docker compose up -d
pytest -q
pytest -q tests/test_redis_cache.py
ruff check src tests scripts
mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics_with_cache.json
python scripts/run_chaos.py --config configs/no_cache.yaml --out reports/metrics_no_cache.json
python scripts/generate_report.py --metrics reports/metrics_with_cache.json --out reports/final_report.md
```