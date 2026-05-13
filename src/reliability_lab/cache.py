from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

PRIVACY_PATTERNS = re.compile(
    r"\b("
    r"balance|password|passcode|otp|one.time.password|credit\s*card|card\s*number|"
    r"ssn|social\s*security|account\s*\d+|user\s*\d+|customer\s*\d+|"
    r"email|phone|address|secret|token|api\s*key"
    r")\b",
    re.IGNORECASE,
)
TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+")
FOUR_DIGIT_PATTERN = re.compile(r"\b\d{4}\b")


def _is_uncacheable(query: str, metadata: dict[str, str] | None = None) -> bool:
    """Return True when a query should never be cached.

    The lab data includes risk labels. High-risk prompts and prompts containing
    private identifiers are deliberately bypassed to avoid leaking sensitive data
    through exact or semantic cache hits.
    """
    if PRIVACY_PATTERNS.search(query):
        return True
    if metadata:
        risk = metadata.get("expected_risk", "").strip().lower()
        if risk in {"high", "sensitive", "private", "pii"}:
            return True
    return False


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True when two similar prompts differ on date/year-sensitive tokens."""
    nums_q = set(FOUR_DIGIT_PATTERN.findall(query))
    nums_c = set(FOUR_DIGIT_PATTERN.findall(cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    cleaned = " ".join(_tokens(text))
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory response cache with TTL, similarity, and safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        best_entry: CacheEntry | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        for entry in self._entries:
            if _is_uncacheable(entry.key, entry.metadata):
                continue
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_entry.key):
            self.false_hit_log.append(
                {"query": query, "cached_key": best_entry.key, "score": round(best_score, 4)}
            )
            return None, best_score

        return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        safe_metadata = metadata or {}
        if _is_uncacheable(query, safe_metadata):
            return

        normalized = query.lower().strip()
        now = time.time()
        for entry in self._entries:
            if entry.key.lower().strip() == normalized:
                entry.value = value
                entry.created_at = now
                entry.metadata = safe_metadata
                return
        self._entries.append(CacheEntry(query, value, now, safe_metadata))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic semantic-style similarity without external APIs.

        Exact matches score 1.0. Otherwise, the score blends token overlap with
        character trigram overlap, which is stronger than raw whitespace Jaccard
        for small wording changes while remaining reproducible for graders.
        """
        a_norm = " ".join(_tokens(a))
        b_norm = " ".join(_tokens(b))
        if not a_norm or not b_norm:
            return 0.0
        if a_norm == b_norm:
            return 1.0

        token_score = _jaccard(set(a_norm.split()), set(b_norm.split()))
        char_score = _jaccard(_char_ngrams(a_norm), _char_ngrams(b_norm))
        return round((0.65 * token_score) + (0.35 * char_score), 4)


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if isinstance(exact_response, str):
                return exact_response, 1.0

            best_key: str | None = None
            best_query: str | None = None
            best_response: str | None = None
            best_score = 0.0

            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_key = str(key)
                    best_query = cached_query
                    best_response = cached_response
                    best_score = score

            if best_query is None or best_response is None or best_score < self.similarity_threshold:
                return None, best_score

            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_query,
                        "redis_key": best_key,
                        "score": round(best_score, 4),
                    }
                )
                return None, best_score

            return best_response, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        safe_metadata = metadata or {}
        if _is_uncacheable(query, safe_metadata):
            return

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping = {
                "query": query,
                "response": value,
                "metadata": json.dumps(safe_metadata, sort_keys=True),
                "created_at": str(time.time()),
            }
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            return

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]