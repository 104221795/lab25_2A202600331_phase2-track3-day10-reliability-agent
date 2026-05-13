from reliability_lab.cache import ResponseCache


def test_semantic_cache_should_not_false_hit_different_intent() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, score = cache.get("Summarize refund policy for 2026 deadline")

    assert cached is None
    assert score >= 0.3
    assert len(cache.false_hit_log) == 1


def test_privacy_query_is_not_cached() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("account balance for user 123", "Balance: $500")
    cached, score = cache.get("account balance for user 123")

    assert cached is None
    assert score == 0.0

    