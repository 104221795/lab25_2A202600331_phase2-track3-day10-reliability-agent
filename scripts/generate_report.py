from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _format_value(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _read_json_if_exists(path: str) -> dict[str, Any] | None:
    file_path = Path(path)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def _format_delta(no_cache_value: Any, with_cache_value: Any) -> str:
    if no_cache_value is None or with_cache_value is None:
        return "N/A"

    try:
        old = float(no_cache_value)
        new = float(with_cache_value)
    except (TypeError, ValueError):
        return "N/A"

    raw_delta = new - old

    if old == 0:
        return f"{raw_delta:.4f}"

    percent_delta = (raw_delta / old) * 100
    return f"{raw_delta:.4f} ({percent_delta:+.2f}%)"


def _cache_comparison_lines() -> list[str]:
    no_cache = _read_json_if_exists("reports/metrics_no_cache.json")
    with_cache = _read_json_if_exists("reports/metrics_with_cache.json")

    lines = [
        "## 5. Cache Comparison",
        "",
    ]

    if no_cache is None or with_cache is None:
        lines.extend(
            [
                "Cache comparison files were not found.",
                "",
                "To generate them, run:",
                "",
                "```bash",
                "python scripts/run_chaos.py --config configs/no_cache.yaml --out reports/metrics_no_cache.json",
                "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics_with_cache.json",
                "```",
                "",
            ]
        )
        return lines

    metrics = [
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "estimated_cost",
        "estimated_cost_saved",
        "cache_hit_rate",
    ]

    lines.extend(
        [
            "| Metric | Without cache | With cache | Delta |",
            "|---|---:|---:|---:|",
        ]
    )

    for metric in metrics:
        no_cache_value = no_cache.get(metric)
        with_cache_value = with_cache.get(metric)
        lines.append(
            f"| {metric} | {_format_value(no_cache_value)} | "
            f"{_format_value(with_cache_value)} | "
            f"{_format_delta(no_cache_value, with_cache_value)} |"
        )

    lines.append("")
    return lines


def _redis_cli_evidence_lines() -> list[str]:
    evidence_path = Path("reports/redis_keys.txt")

    lines = [
        "## 6. Redis Shared Cache",
        "",
        "`SharedRedisCache` stores each response as a Redis Hash with fields `query`, `response`, `metadata`, and `created_at`, then applies `EXPIRE` for TTL cleanup. It supports exact lookup by query hash and similarity lookup with `SCAN`, so separate gateway instances can share cached responses in horizontally scaled deployments.",
        "",
        "Shared cache matters because production gateways usually run as multiple instances behind a load balancer. If each instance only had its own in-memory cache, cache hits would be inconsistent. Redis gives all instances one shared cache state.",
        "",
        "Evidence command:",
        "",
        "```bash",
        "docker compose exec redis redis-cli KEYS 'rl:*'",
        "```",
        "",
    ]

    if evidence_path.exists():
        try:
            evidence = evidence_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            evidence = evidence_path.read_text(encoding="utf-16").strip()
        
        lines.extend(
            [
                "Redis CLI output captured in `reports/redis_keys.txt`:",
                "",
                "```text",
                evidence or "(no keys returned)",
                "```",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Redis CLI output was not captured automatically. Before submission, run:",
                "",
                "```powershell",
                "docker compose exec redis redis-cli KEYS 'rl:*' > reports/redis_keys.txt",
                "```",
                "",
                "Then regenerate the report:",
                "",
                "```powershell",
                "python scripts/generate_report.py --metrics reports/metrics_with_cache.json --out reports/final_report.md",
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "Redis test evidence:",
            "",
            "```text",
            "pytest -q tests/test_redis_cache.py",
            "Expected: all Redis cache tests pass when Docker Redis is running.",
            "```",
            "",
        ]
    )

    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    scenarios = metrics.get("scenarios", {})

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture Summary",
        "",
        "The gateway uses a production-style reliability layer in front of simulated LLM providers. Each request first checks the cache, then routes through provider-specific circuit breakers, then falls back to backup providers, and finally returns a static degraded-service message if every provider is unavailable.",
        "",
        "```text",
        "User",
        "  ↓",
        "ReliabilityGateway",
        "  ↓",
        "[Cache check: exact/similarity + privacy and false-hit guardrails]",
        "  ↓ cache miss",
        "[Circuit breaker per provider]",
        "  ↓",
        "Primary Provider",
        "  ↓ if failed/open",
        "Backup Provider",
        "  ↓ if failed/open",
        "Static Fallback",
        "  ↓",
        "Metrics: latency, availability, cache hit rate, fallback success, circuit opens, recovery time, cost saved",
        "```",
        "",
        "## 2. Configuration Table",
        "",
        "| Setting | Value | Why this value |",
        "|---|---:|---|",
        "| failure_threshold | 3 | Low enough to detect repeated failures quickly, high enough to avoid false opens from one-off jitter. |",
        "| reset_timeout_seconds | 2 | Short enough for repeatable lab recovery tests while still showing OPEN → HALF_OPEN behavior. |",
        "| success_threshold | 1 | One successful HALF_OPEN probe is acceptable for this deterministic simulated-provider lab. |",
        "| cache TTL | 300 seconds | Five-minute freshness window for FAQ-style prompts while limiting stale cache risk. |",
        "| similarity_threshold | 0.92 | Conservative threshold to reduce wrong semantic cache hits on date-sensitive prompts. |",
        "| load_test.requests | 200 | Meets the README requirement for a stronger load test. |",
        "| load_test.concurrency | 10 | Exercises the gateway under concurrent request load instead of only sequential execution. |",
        "| cache backend | memory / redis | Memory is fast for one process; Redis demonstrates shared cache for multi-instance deployment. |",
        "| Redis prefix | rl:* | Namespaces lab cache entries and makes Redis evidence easy to inspect. |",
        "",
        "## 3. Metrics Table",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]

    for key, value in metrics.items():
        if key == "scenarios":
            continue
        lines.append(f"| {key} | {_format_value(value)} |")

    lines.extend(
        [
            "",
            "## 4. Chaos Scenario Table",
            "",
            "| Scenario | Expected behavior | Observed status |",
            "|---|---|---|",
        ]
    )

    expectations = {
        "primary_timeout_100": "Primary provider fails 100%; circuit opens and backup provider serves traffic.",
        "primary_flaky_50": "Primary provider fails intermittently; circuit opens/recover and fallback protects availability.",
        "cache_stale_candidate": "Low-threshold cache test; similar prompts with different years must not create false cache hits.",
        "cache_stale_candidate:cache_false_hit_guardrail": "False-hit guardrail records and blocks the 2024 vs 2026 cache candidate.",
        "all_providers_down": "All providers fail; gateway returns static fallback instead of crashing.",
        "all_healthy": "Primary provider is healthy; availability should be high with minimal fallback.",
    }

    for name, status in scenarios.items():
        expectation = expectations.get(str(name), "Scenario-specific reliability behavior is checked.")
        lines.append(f"| {name} | {expectation} | {status} |")

    lines.extend([""])
    lines.extend(_cache_comparison_lines())
    lines.extend(
        [
            "The cache uses exact-match lookup first, then deterministic similarity scoring. Privacy-sensitive prompts and date/year mismatches are bypassed to prevent unsafe false hits.",
            "",
            "| Cache guardrail | Evidence |",
            "|---|---|",
            "| Privacy bypass | Queries containing account, user, password, card, token, or other sensitive terms are not stored or returned. |",
            "| False-hit prevention | `refund policy for 2024` and `refund policy for 2026` are detected as different-intent prompts despite high textual similarity. |",
            "| TTL | Entries expire automatically after the configured TTL. |",
            "| Exact-match fast path | Identical prompts return immediately with score `1.0`. |",
            "",
        ]
    )

    lines.extend(_redis_cli_evidence_lines())

    lines.extend(
        [
            "## 7. Failure Analysis",
            "",
            "Remaining weakness: circuit breaker state is still local to each Python process. In a real multi-instance deployment, one instance could open a circuit while another instance continues sending traffic to the failing provider because the breaker counters are not shared.",
            "",
            "Production fix: move circuit breaker state to Redis or another shared state store using atomic counters, expirations, and provider-level health keys. This would make provider health consistent across all gateway instances.",
            "",
            "Another production risk is that the current semantic cache uses deterministic local similarity rather than embeddings. This is safer and reproducible for the lab, but in production it may miss valid paraphrases. A future version could use embeddings with stricter policy checks and audit logging.",
            "",
            "## 8. Next Steps",
            "",
            "1. Store circuit breaker counters and state transitions in Redis so provider health is shared across horizontally scaled gateway instances.",
            "2. Add Prometheus metrics such as `agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, and `circuit_state`.",
            "3. Add cost-aware routing so traffic shifts to cheaper providers after budget thresholds are reached.",
            "4. Add property-based tests for circuit breaker transitions under randomized failure and recovery sequences.",
            "",
            "## 9. Reproducibility Commands",
            "",
            "```bash",
            "pip install -e \".[dev]\"",
            "docker compose up -d",
            "pytest -q",
            "pytest -q tests/test_redis_cache.py",
            "ruff check src tests scripts",
            "mypy src",
            "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics_with_cache.json",
            "python scripts/run_chaos.py --config configs/no_cache.yaml --out reports/metrics_no_cache.json",
            "python scripts/generate_report.py --metrics reports/metrics_with_cache.json --out reports/final_report.md",
            "```",
        ]
    )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()