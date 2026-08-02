"""
Unit tests for remind_me_mcp.rate_limit — the in-memory, dependency-free
rate limiter shared by webhook_server.py's POST /ingest and remote.py's MCP
Streamable HTTP endpoint (issue #183).

Every test drives a fake, manually-advanced clock instead of time.sleep(),
mirroring the clock-injection convention already used elsewhere in this
codebase (vitality.build_vitality_report's `now` parameter, oauth.py's
module-level `_now()`).
"""

from __future__ import annotations

import threading

from remind_me_mcp.rate_limit import (
    RateLimiter,
    RateLimitResult,
    resolve_key,
    retry_after_seconds,
)


class FakeClock:
    """A manually-advanced clock for deterministic rate-limit tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def test_allows_up_to_the_limit_within_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)

    for _ in range(3):
        result = limiter.hit("k")
        assert result.allowed is True
        assert result.retry_after == 0.0


def test_blocks_the_nplus1th_request() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)

    for _ in range(3):
        assert limiter.hit("k").allowed is True

    result = limiter.hit("k")
    assert result.allowed is False
    assert result.retry_after > 0.0
    assert result.retry_after <= 60.0


def test_rejected_hits_do_not_extend_or_reset_the_window() -> None:
    """A client hammering past the limit doesn't get its own rejected
    attempts counted towards a fresh window — retry_after should shrink
    monotonically as the original window ages, not stay pinned at 60."""
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)

    assert limiter.hit("k").allowed is True
    first_retry = limiter.hit("k").retry_after
    clock.advance(10)
    second_retry = limiter.hit("k").retry_after
    assert second_retry < first_retry
    assert abs((first_retry - second_retry) - 10) < 1e-9


def test_resets_after_the_window_elapses() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=30, clock=clock)

    assert limiter.hit("k").allowed is True
    assert limiter.hit("k").allowed is True
    assert limiter.hit("k").allowed is False

    clock.advance(30)  # window fully elapsed
    result = limiter.hit("k")
    assert result.allowed is True
    assert result.retry_after == 0.0

    # And the fresh window has its own full quota, not a leftover count.
    assert limiter.hit("k").allowed is True
    assert limiter.hit("k").allowed is False


def test_distinct_keys_have_independent_buckets() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)

    assert limiter.hit("alice").allowed is True
    assert limiter.hit("alice").allowed is False
    # Bob's quota is untouched by Alice exhausting hers.
    assert limiter.hit("bob").allowed is True


# ---------------------------------------------------------------------------
# Stale-key eviction
# ---------------------------------------------------------------------------


def test_evicts_stale_keys_on_a_later_call() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=5, window_seconds=10, clock=clock, prune_every=2)

    limiter.hit("stale-1")
    limiter.hit("stale-2")
    assert set(limiter._buckets) == {"stale-1", "stale-2"}

    # Advance well past the window, then make prune_every calls on a
    # different key so the sweep triggers and finds the old ones expired.
    clock.advance(100)
    limiter.hit("fresh")
    limiter.hit("fresh")  # prune_every=2 -> this call sweeps

    assert "stale-1" not in limiter._buckets
    assert "stale-2" not in limiter._buckets
    assert "fresh" in limiter._buckets


def test_prune_does_not_evict_a_key_whose_window_is_still_live() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=5, window_seconds=100, clock=clock, prune_every=2)

    limiter.hit("still-live")
    clock.advance(10)  # window (100s) has not elapsed
    limiter.hit("x")
    limiter.hit("x")  # triggers a sweep

    assert "still-live" in limiter._buckets


def test_reset_clears_all_buckets() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.hit("k")
    assert limiter._buckets
    limiter.reset()
    assert limiter._buckets == {}
    # And the quota is fresh again.
    assert limiter.hit("k").allowed is True


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_thread_safe_under_concurrent_hits() -> None:
    """N threads hammering the same key concurrently must yield exactly
    `limit` allowed hits — not more due to a check-then-increment race."""
    clock = FakeClock()  # fixed time: every hit falls in the same window
    limiter = RateLimiter(limit=50, window_seconds=60, clock=clock)

    allowed_count = 0
    count_lock = threading.Lock()

    def worker() -> None:
        nonlocal allowed_count
        result = limiter.hit("shared-key")
        if result.allowed:
            with count_lock:
                allowed_count += 1

    threads = [threading.Thread(target=worker) for _ in range(500)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert allowed_count == 50


def test_thread_safe_across_many_distinct_keys() -> None:
    """Concurrent hits across many keys never leak allowance between keys."""
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    results: dict[int, list[bool]] = {i: [] for i in range(20)}
    results_lock = threading.Lock()

    def worker(key_id: int) -> None:
        result = limiter.hit(f"key-{key_id}")
        with results_lock:
            results[key_id].append(result.allowed)

    threads = []
    for key_id in range(20):
        for _ in range(5):  # 5 concurrent hits per key, limit is 2
            threads.append(threading.Thread(target=worker, args=(key_id,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for key_id, outcomes in results.items():
        assert outcomes.count(True) == 2, f"key {key_id} allowed {outcomes.count(True)} != 2"


# ---------------------------------------------------------------------------
# resolve_key
# ---------------------------------------------------------------------------


def test_resolve_key_matches_known_secret() -> None:
    """`presented` is the raw credential, already stripped of any scheme
    prefix (e.g. the caller strips "Bearer " before calling this, exactly
    as webhook_server._rate_limited / remote.RateLimitMiddleware do)."""
    assert resolve_key("s3cr3t", "1.2.3.4", "s3cr3t") == "auth:known"


def test_resolve_key_wrong_credential_falls_back_to_ip() -> None:
    assert resolve_key("wrong", "1.2.3.4", "s3cr3t") == "ip:1.2.3.4"


def test_resolve_key_missing_credential_falls_back_to_ip() -> None:
    assert resolve_key("", "1.2.3.4", "s3cr3t") == "ip:1.2.3.4"


def test_resolve_key_no_known_secret_always_uses_ip() -> None:
    assert resolve_key("anything", "1.2.3.4", None) == "ip:1.2.3.4"
    assert resolve_key("anything", "1.2.3.4", "") == "ip:1.2.3.4"


def test_resolve_key_unknown_ip_has_a_stable_placeholder() -> None:
    assert resolve_key("", "", None) == "ip:unknown"


def test_resolve_key_does_not_mint_a_bucket_per_forged_credential() -> None:
    """A different bogus credential on every call must still land in the
    same IP bucket — otherwise an attacker could rotate a random
    Authorization value per request and dodge the limiter entirely."""
    keys = {resolve_key(f"forged-{i}", "9.9.9.9", "s3cr3t") for i in range(10)}
    assert keys == {"ip:9.9.9.9"}


# ---------------------------------------------------------------------------
# retry_after_seconds
# ---------------------------------------------------------------------------


def test_retry_after_seconds_rounds_up() -> None:
    assert retry_after_seconds(0.1) == 1
    assert retry_after_seconds(1.0) == 1
    assert retry_after_seconds(1.5) == 2
    assert retry_after_seconds(59.9) == 60


def test_retry_after_seconds_never_zero() -> None:
    assert retry_after_seconds(0.0) == 1
    assert retry_after_seconds(-5.0) == 1


def test_rate_limit_result_is_a_plain_dataclass() -> None:
    result = RateLimitResult(allowed=True, retry_after=0.0)
    assert result.allowed is True
    assert result.retry_after == 0.0
