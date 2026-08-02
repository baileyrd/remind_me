"""
Tests for remind_me_mcp.metrics — Prometheus text-exposition metrics (issue #197).

Covers the exposition-format renderer against known counter state, thread
safety under concurrent increments (mirroring test_rate_limit.py's own
concurrency test pattern from issue #183), the GET /metrics route's
disabled-by-default 404 / enabled 200 behavior and content type, the
unauthenticated-without-a-token auth stance, and end-to-end integration
through the real code paths that record each metric (server.py's MCP tool
dispatch, tools/search.py's tier breakdown, rate_limit.py's rejection path)
rather than only unit-testing the counter object itself.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from remind_me_mcp import config as _config
from remind_me_mcp import metrics

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_state() -> None:
    """Every test starts from, and leaves behind, a clean zeroed counter state."""
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def metrics_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on REMIND_ME_METRICS_ENABLED for the duration of one test."""
    monkeypatch.setattr(_config, "METRICS_ENABLED", True)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A dashboard TestClient with dashboard auth explicitly disabled (SE-01
    opt-out) -- mirrors test_api.py's own `client` fixture, kept local here
    so this file has no import-order dependency on test_api.py."""
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.api import _build_api_app

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_config, "API_KEY", "disabled")
    app = _build_api_app()
    return TestClient(app)


# ---------------------------------------------------------------------------
# render_prometheus_text() — output format
# ---------------------------------------------------------------------------


def test_render_prometheus_text_empty_state_is_well_formed() -> None:
    text = metrics.render_prometheus_text()

    assert "# HELP remind_me_tool_calls_total" in text
    assert "# TYPE remind_me_tool_calls_total counter" in text
    assert "# TYPE remind_me_search_tier_results_total counter" in text
    # All three known tiers are always present, even at zero.
    assert 'remind_me_search_tier_results_total{tier="keyword"} 0' in text
    assert 'remind_me_search_tier_results_total{tier="semantic"} 0' in text
    assert 'remind_me_search_tier_results_total{tier="hybrid"} 0' in text
    assert "remind_me_rate_limit_rejections_total 0" in text
    # No tool has ever been called -- no sample lines for that family.
    assert "remind_me_tool_calls_total{" not in text


def test_render_prometheus_text_known_counter_state(metrics_enabled: None) -> None:
    metrics.record_tool_call("remind_me_search", 0.5)
    metrics.record_tool_call("remind_me_search", 1.5)
    metrics.record_tool_call("remind_me_add", 0.25)
    metrics.record_search_tier({"keyword": 3, "semantic": 1, "hybrid": 2})
    metrics.record_search_tier({"keyword": 1})
    metrics.record_rate_limit_rejection()
    metrics.record_rate_limit_rejection()

    text = metrics.render_prometheus_text()

    assert 'remind_me_tool_calls_total{tool="remind_me_search"} 2' in text
    assert 'remind_me_tool_calls_total{tool="remind_me_add"} 1' in text
    assert 'remind_me_tool_call_duration_seconds_sum{tool="remind_me_search"} 2.0' in text
    assert 'remind_me_tool_call_duration_seconds_sum{tool="remind_me_add"} 0.25' in text
    assert 'remind_me_tool_call_duration_seconds_count{tool="remind_me_search"} 2' in text
    assert 'remind_me_search_tier_results_total{tier="keyword"} 4' in text
    assert 'remind_me_search_tier_results_total{tier="semantic"} 1' in text
    assert 'remind_me_search_tier_results_total{tier="hybrid"} 2' in text
    assert "remind_me_rate_limit_rejections_total 2" in text


def test_render_prometheus_text_every_sample_has_preceding_help_and_type(
    metrics_enabled: None,
) -> None:
    """Minimal structural parse check: every sample line's metric name has a
    preceding `# TYPE` (and implicitly `# HELP`, always emitted together)
    declaration for that exact name."""
    metrics.record_tool_call("remind_me_search", 0.1)
    metrics.record_search_tier({"keyword": 1})
    metrics.record_rate_limit_rejection()

    text = metrics.render_prometheus_text(
        gauges=[metrics.GaugeSpec("remind_me_memories_total", "help", 3.0)]
    )
    lines = text.splitlines()
    declared_types: dict[str, str] = {}
    seen_help_before_type = False
    for line in lines:
        if line.startswith("# HELP "):
            seen_help_before_type = True
        elif line.startswith("# TYPE "):
            assert seen_help_before_type, f"# TYPE with no preceding # HELP: {line!r}"
            seen_help_before_type = False
            _, _, rest = line.partition("# TYPE ")
            name, _, kind = rest.partition(" ")
            declared_types[name] = kind
        elif line.strip() and not line.startswith("#"):
            name = line.split("{")[0].split(" ")[0]
            assert name in declared_types, f"sample line with no preceding # TYPE: {line!r}"
            assert declared_types[name] in ("counter", "gauge")


def test_render_prometheus_text_with_extra_gauge() -> None:
    gauge = metrics.GaugeSpec("remind_me_memories_total", "Total memories in the store.", 42.0)

    text = metrics.render_prometheus_text(gauges=[gauge])

    assert "# HELP remind_me_memories_total Total memories in the store." in text
    assert "# TYPE remind_me_memories_total gauge" in text
    assert "remind_me_memories_total 42.0" in text


def test_render_prometheus_text_escapes_label_values() -> None:
    gauge = metrics.GaugeSpec(
        "remind_me_test_gauge", "help", 1.0, labels={"k": 'has "quotes" and \\backslash'}
    )
    text = metrics.render_prometheus_text(gauges=[gauge])
    assert r'k="has \"quotes\" and \\backslash"' in text


# ---------------------------------------------------------------------------
# No-op when disabled (default)
# ---------------------------------------------------------------------------


def test_record_tool_call_is_noop_when_disabled() -> None:
    assert _config.METRICS_ENABLED is False  # sanity: off by default
    metrics.record_tool_call("remind_me_search", 1.0)
    assert 'tool="remind_me_search"' not in metrics.render_prometheus_text()


def test_record_search_tier_is_noop_when_disabled() -> None:
    metrics.record_search_tier({"keyword": 5, "semantic": 5, "hybrid": 5})
    text = metrics.render_prometheus_text()
    assert 'remind_me_search_tier_results_total{tier="keyword"} 0' in text


def test_record_rate_limit_rejection_is_noop_when_disabled() -> None:
    metrics.record_rate_limit_rejection()
    assert "remind_me_rate_limit_rejections_total 0" in metrics.render_prometheus_text()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_thread_safe_tool_call_counting_under_concurrency(metrics_enabled: None) -> None:
    """N threads recording the same tool concurrently must yield exactly N
    total calls -- not more/less due to a check-then-increment race."""

    def worker() -> None:
        metrics.record_tool_call("remind_me_search", 0.01)

    threads = [threading.Thread(target=worker) for _ in range(500)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = metrics.render_prometheus_text()
    assert 'remind_me_tool_calls_total{tool="remind_me_search"} 500' in text
    assert 'remind_me_tool_call_duration_seconds_count{tool="remind_me_search"} 500' in text


def test_thread_safe_rate_limit_rejection_counting_under_concurrency(metrics_enabled: None) -> None:
    def worker() -> None:
        metrics.record_rate_limit_rejection()

    threads = [threading.Thread(target=worker) for _ in range(500)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert "remind_me_rate_limit_rejections_total 500" in metrics.render_prometheus_text()


def test_thread_safe_search_tier_counting_under_concurrency(metrics_enabled: None) -> None:
    def worker() -> None:
        metrics.record_search_tier({"keyword": 1, "semantic": 1, "hybrid": 1})

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = metrics.render_prometheus_text()
    for tier in ("keyword", "semantic", "hybrid"):
        assert f'remind_me_search_tier_results_total{{tier="{tier}"}} 200' in text


# ---------------------------------------------------------------------------
# GET /metrics route
# ---------------------------------------------------------------------------


def test_metrics_route_returns_404_when_disabled(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_metrics_route_returns_200_with_content_when_enabled(
    client: TestClient, metrics_enabled: None
) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in r.headers["content-type"]
    assert "# HELP remind_me_tool_calls_total" in r.text


def test_metrics_route_includes_fresh_memory_gauge(
    client: TestClient, metrics_enabled: None, memory_factory
) -> None:
    memory_factory(content="one")
    memory_factory(content="two")

    r = client.get("/metrics")

    assert "# TYPE remind_me_memories_total gauge" in r.text
    assert "remind_me_memories_total 2.0" in r.text


def test_metrics_route_no_outbox_gauge_when_sync_disabled(
    client: TestClient, metrics_enabled: None
) -> None:
    """SYNC_ENABLED is false in the test environment (no NODE_ID/HUB_URL/
    SYNC_SECRET) -- the sync-outbox gauge should be omitted entirely rather
    than emitted as a misleading zero."""
    r = client.get("/metrics")
    assert "remind_me_sync_outbox_pending" not in r.text


# ---------------------------------------------------------------------------
# Auth stance: unauthenticated, even with the dashboard API key enabled
# ---------------------------------------------------------------------------


def test_metrics_route_requires_no_auth_header_even_with_api_key_set(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, metrics_enabled: None
) -> None:
    """Mirrors /health's SE-04 precedent (test_api.py's
    test_health_route_open_without_auth): /metrics must stay reachable with
    zero Authorization header even when REMIND_ME_API_KEY gates every
    /api/* route."""
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.api import _build_api_app

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_config, "API_KEY", "test-secret-key")
    app = _build_api_app()
    client_with_auth = TestClient(app)

    r = client_with_auth.get("/metrics")  # deliberately: no Authorization header at all

    assert r.status_code == 200


def test_api_stats_still_requires_auth_for_contrast(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast check: an ordinary /api/* route in the same app build DOES
    reject a missing token, proving /metrics' open access is a deliberate
    route-specific exemption, not BearerAuthMiddleware failing to apply."""
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.api import _build_api_app

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_config, "API_KEY", "test-secret-key")
    app = _build_api_app()
    client_with_auth = TestClient(app)

    r = client_with_auth.get("/api/stats")

    assert r.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end integration through the real recording code paths
# ---------------------------------------------------------------------------


async def test_tool_call_increments_counter_through_real_mcp_dispatch(
    db_conn: sqlite3.Connection, metrics_enabled: None
) -> None:
    """Goes through server.py's real _TracedFastMCP.call_tool choke point,
    not a direct metrics.record_tool_call() unit call."""
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401 -- registers tools on server_mod.mcp

    await server_mod.mcp.call_tool("remind_me_stats", {"params": {}})

    text = metrics.render_prometheus_text()
    assert 'remind_me_tool_calls_total{tool="remind_me_stats"} 1' in text
    assert 'remind_me_tool_call_duration_seconds_count{tool="remind_me_stats"} 1' in text


async def test_multiple_tool_calls_accumulate_per_tool_name(
    db_conn: sqlite3.Connection, metrics_enabled: None
) -> None:
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401

    await server_mod.mcp.call_tool("remind_me_stats", {"params": {}})
    await server_mod.mcp.call_tool("remind_me_stats", {"params": {}})

    text = metrics.render_prometheus_text()
    assert 'remind_me_tool_calls_total{tool="remind_me_stats"} 2' in text


async def test_search_increments_tier_counters_through_real_search(
    db_conn: sqlite3.Connection, memory_factory, metrics_enabled: None
) -> None:
    """Goes through the real remind_me_search dispatch (tools/search.py),
    which calls retrieval.compute_tier_breakdown then
    metrics.record_search_tier -- not a direct unit call."""
    from remind_me_mcp.models import MemorySearchInput
    from remind_me_mcp.tools import memory_search

    memory_factory(content="a distinctive zzqmetricsearch phrase for this test")

    await memory_search(MemorySearchInput(query="zzqmetricsearch"))

    text = metrics.render_prometheus_text()
    # Semantic availability varies by test environment (no bundled ONNX
    # model), but the keyword tier is always exercised by an FTS match --
    # assert the total across all three tiers advanced by exactly one hit
    # rather than pinning to a specific tier.
    total = sum(
        int(m.group(1))
        for m in re.finditer(r'remind_me_search_tier_results_total\{tier="\w+"\} (\d+)', text)
    )
    assert total == 1


def test_rate_limit_rejection_increments_counter_through_real_limiter(
    metrics_enabled: None,
) -> None:
    """Goes through RateLimiter.hit()'s real rejection branch (rate_limit.py),
    not a direct metrics.record_rate_limit_rejection() unit call."""
    from remind_me_mcp.rate_limit import RateLimiter

    limiter = RateLimiter(limit=1, window_seconds=60)

    assert limiter.hit("shared-key").allowed is True
    assert limiter.hit("shared-key").allowed is False  # this is the rejection

    assert "remind_me_rate_limit_rejections_total 1" in metrics.render_prometheus_text()


def test_rate_limit_allowed_hits_do_not_increment_rejection_counter(
    metrics_enabled: None,
) -> None:
    from remind_me_mcp.rate_limit import RateLimiter

    limiter = RateLimiter(limit=5, window_seconds=60)
    for _ in range(5):
        assert limiter.hit("shared-key").allowed is True

    assert "remind_me_rate_limit_rejections_total 0" in metrics.render_prometheus_text()
