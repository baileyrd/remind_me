"""
remind_me_mcp.metrics — Prometheus-format metrics exposition (issue #197).

Off by default (``REMIND_ME_METRICS_ENABLED``, see config.py) -- matches the
OTel tracing opt-in precedent (README's "Observability" section): this is
instrumentation surface, not a core feature. ``GET /metrics`` (api.py) is a
plain 404 while disabled.

**Dependency decision: hand-rolled exposition, not ``prometheus_client``.**
The format this module emits is the well-known text exposition format
(https://github.com/prometheus/docs/blob/main/content/docs/instrumenting/exposition_formats.md#text-based-format)
-- a handful of ``# HELP``/``# TYPE`` lines plus ``name{labels} value`` lines.
That's a few dozen lines of string formatting, not a real parsing/protocol
problem, and this codebase has repeatedly chosen to hand-roll small, simple
formats over adding a dependency for them (rate_limit.py's fixed-window
limiter, telemetry.py's own OTLP-only surface, notifications.py's webhook
POST) -- ``pyproject.toml`` has no ``prometheus_client`` dependency today,
and adding one here would be new base-dependency weight for something this
small. The one thing a client library buys for free -- safe concurrent
counter increments -- is solved the exact same way ``rate_limit.py`` already
solved an identical problem: a single ``threading.Lock`` guarding plain
dicts/counters. That's correct for the same reason given in
``rate_limit.py``'s module docstring: CPython's GIL means the critical
section (dict reads/writes, no I/O, nothing ever awaited while the lock is
held) can't be preempted, so one lock safely serves both a worker thread
(webhook/peer-server style callers) and an asyncio coroutine (the Starlette
``/metrics`` handler, the FastMCP tool dispatch hook) without ever blocking
the event loop for longer than the tiny critical section itself takes.

**Auth stance: unauthenticated, gated on REMIND_ME_METRICS_ENABLED instead.**
See ``api.py``'s ``metrics_endpoint`` docstring for the full rationale
(mirrors ``/health``'s SE-04 precedent and typical Prometheus scrape
configs, which usually send no custom headers at all).

Every ``record_*`` function below is a no-op unless
``config.METRICS_ENABLED`` is set -- mirrors ``telemetry.maybe_span``'s
"always call it, it decides for itself" pattern, so call sites (server.py,
rate_limit.py, tools/search.py) never need their own
``if config.METRICS_ENABLED:`` guard. ``config`` is read fresh on every call
(not captured at import time) so tests can monkeypatch
``config.METRICS_ENABLED``, matching the "reads module attributes at call
time" convention already used throughout ``config.py``'s own
``resolve_*`` functions.

**What's tracked as running counter state vs. computed fresh:** tool-call
counts/latency, search-tier counts, and rate-limit rejections are genuinely
about *events over time* -- no database query can reconstruct "how many
times was remind_me_search called since the process started" after the
fact, so they need real counter state, which is what this module owns.
Anything that's already a cheap point-in-time DB query (total memory count,
sync outbox depth) is deliberately NOT duplicated here as a counter --
``api.py``'s ``/metrics`` handler computes those fresh on each scrape
(exactly like ``GET /api/stats``/``GET /api/vitality`` already do) and
passes them into :func:`render_prometheus_text` as ad hoc gauges, via
:class:`GaugeSpec`, rather than this module tracking a shadow copy that
could drift from the real table.
"""

from __future__ import annotations

import threading
from typing import NamedTuple

from remind_me_mcp import config as _config

_lock = threading.Lock()

_tool_call_counts: dict[str, int] = {}
_tool_call_seconds: dict[str, float] = {}
_search_tier_counts: dict[str, int] = {"keyword": 0, "semantic": 0, "hybrid": 0}
_rate_limit_rejections: int = 0


# ---------------------------------------------------------------------------
# Recording (no-op unless config.METRICS_ENABLED)
# ---------------------------------------------------------------------------


def record_tool_call(tool_name: str, duration_seconds: float) -> None:
    """Record one completed MCP tool call's name and wall-clock duration.

    Called from the single choke point every real tool invocation passes
    through -- ``server.py``'s ``_TracedFastMCP.call_tool`` -- the same spot
    already used for the OTEL span and the slow-call watchdog (see that
    class's docstring for why it's the one place that can intercept all
    ~40 individually-decorated tools without touching each of them).
    """
    if not _config.METRICS_ENABLED:
        return
    with _lock:
        _tool_call_counts[tool_name] = _tool_call_counts.get(tool_name, 0) + 1
        _tool_call_seconds[tool_name] = _tool_call_seconds.get(tool_name, 0.0) + duration_seconds


def record_search_tier(tier_breakdown: dict[str, int]) -> None:
    """Add one search response's per-tier result counts to the running totals.

    *tier_breakdown* is exactly what ``retrieval.compute_tier_breakdown``
    already returns per call (``{"keyword": n, "semantic": n, "hybrid": n}``)
    -- this just accumulates it across calls, since that per-call breakdown
    itself is not retained anywhere after the response is sent.
    """
    if not _config.METRICS_ENABLED:
        return
    with _lock:
        for tier in ("keyword", "semantic", "hybrid"):
            _search_tier_counts[tier] += tier_breakdown.get(tier, 0)


def record_rate_limit_rejection() -> None:
    """Record one request rejected by :class:`rate_limit.RateLimiter`.

    Called from ``RateLimiter.hit()``'s own rejection path -- the single
    choke point both of this codebase's rate-limited routes
    (``webhook_server.py``'s ``POST /ingest``, ``remote.py``'s Streamable
    HTTP endpoint) already flow through, rather than instrumenting each
    call site separately.
    """
    global _rate_limit_rejections
    if not _config.METRICS_ENABLED:
        return
    with _lock:
        _rate_limit_rejections += 1


def reset() -> None:
    """Clear all counter state back to zero (test helper / operator hook).

    Unlike the ``record_*`` functions, this always runs regardless of
    ``config.METRICS_ENABLED`` -- a test that flips the flag mid-test still
    needs a clean slate to assert against.
    """
    global _rate_limit_rejections
    with _lock:
        _tool_call_counts.clear()
        _tool_call_seconds.clear()
        for tier in _search_tier_counts:
            _search_tier_counts[tier] = 0
        _rate_limit_rejections = 0


# ---------------------------------------------------------------------------
# Prometheus text exposition (format version 0.0.4)
# ---------------------------------------------------------------------------


class GaugeSpec(NamedTuple):
    """One caller-supplied, freshly-computed gauge line for :func:`render_prometheus_text`.

    Used by ``api.py``'s ``/metrics`` handler for values that are cheap to
    recompute from the database on every scrape (total memory count, sync
    outbox depth) rather than values this module tracks as counter state --
    see the module docstring's "what's tracked vs. computed fresh" note.
    """

    name: str
    help_text: str
    value: float
    labels: dict[str, str] | None = None


def _escape_label_value(value: str) -> str:
    """Escape a label value per the exposition format's quoting rules."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    parts = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in labels.items())
    return "{" + parts + "}"


def _format_metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    return f"{name}{_format_labels(labels)} {value}"


def render_prometheus_text(gauges: list[GaugeSpec] | None = None) -> str:
    """Render current counter state as Prometheus text exposition format.

    Args:
        gauges: Optional additional, already-computed gauge values to append
            (see :class:`GaugeSpec`) -- e.g. a fresh DB-query result the
            caller computed for this one scrape.

    Returns:
        A ``text/plain; version=0.0.4``-compatible payload: one
        ``# HELP``/``# TYPE`` pair per metric family, followed by its
        sample line(s). Sorted by tool/tier name within each family for
        deterministic, diff-friendly output (and easy test assertions).

    Note: always returns valid output for empty state too -- an unused
    counter still emits its ``# HELP``/``# TYPE`` header and zero-valued
    samples where the label set is fully known ahead of time (the three
    search tiers, the single rejection counter); tool-call metrics simply
    have no sample lines yet if no tool has ever been called.
    """
    with _lock:
        tool_counts = dict(_tool_call_counts)
        tool_seconds = dict(_tool_call_seconds)
        tier_counts = dict(_search_tier_counts)
        rejections = _rate_limit_rejections

    lines: list[str] = []

    lines.append("# HELP remind_me_tool_calls_total Total MCP tool calls, by tool name.")
    lines.append("# TYPE remind_me_tool_calls_total counter")
    for tool in sorted(tool_counts):
        lines.append(_format_metric("remind_me_tool_calls_total", tool_counts[tool], {"tool": tool}))

    lines.append(
        "# HELP remind_me_tool_call_duration_seconds_sum "
        "Sum of MCP tool call durations in seconds, by tool name."
    )
    lines.append("# TYPE remind_me_tool_call_duration_seconds_sum counter")
    for tool in sorted(tool_seconds):
        lines.append(
            _format_metric("remind_me_tool_call_duration_seconds_sum", tool_seconds[tool], {"tool": tool})
        )

    lines.append(
        "# HELP remind_me_tool_call_duration_seconds_count "
        "Count of MCP tool calls timed, by tool name (divide the _sum by this for the average)."
    )
    lines.append("# TYPE remind_me_tool_call_duration_seconds_count counter")
    for tool in sorted(tool_counts):
        lines.append(
            _format_metric("remind_me_tool_call_duration_seconds_count", tool_counts[tool], {"tool": tool})
        )

    lines.append(
        "# HELP remind_me_search_tier_results_total "
        "Cumulative remind_me_search result count, by ranking tier (keyword/semantic/hybrid)."
    )
    lines.append("# TYPE remind_me_search_tier_results_total counter")
    for tier in ("keyword", "semantic", "hybrid"):
        lines.append(
            _format_metric("remind_me_search_tier_results_total", tier_counts.get(tier, 0), {"tier": tier})
        )

    lines.append(
        "# HELP remind_me_rate_limit_rejections_total "
        "Total requests rejected by the rate limiter (issue #183)."
    )
    lines.append("# TYPE remind_me_rate_limit_rejections_total counter")
    lines.append(_format_metric("remind_me_rate_limit_rejections_total", rejections))

    for gauge in gauges or []:
        lines.append(f"# HELP {gauge.name} {gauge.help_text}")
        lines.append(f"# TYPE {gauge.name} gauge")
        lines.append(_format_metric(gauge.name, gauge.value, gauge.labels))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "GaugeSpec",
    "record_tool_call",
    "record_search_tier",
    "record_rate_limit_rejection",
    "render_prometheus_text",
    "reset",
]
