"""
remind_me_mcp.telemetry — Optional OpenTelemetry instrumentation (cognee gap #9, Phase 7a).

Off by default and zero-cost when unset: ``maybe_span()`` is a no-op context
manager whenever tracing is disabled or the optional ``opentelemetry`` extra
isn't installed — same graceful-degradation pattern as semantic search
(embeddings.py) and reranking (reranker.py). Enable with
``REMIND_ME_OTEL_ENABLED=1`` and point ``REMIND_ME_OTEL_ENDPOINT`` at
whatever OTLP collector you already run (Jaeger, Tempo, Honeycomb, ...) —
remind_me never bundles or manages a collector itself, which would conflict
with the zero-ops, local-first design center.

Instrumented at four boundaries only — every MCP tool call (server.py),
each sync cycle (sync.py), each folder-watcher scan pass (watcher.py), and
each webhook ingest request (webhook_server.py) — enough to see where time
goes without turning this into a general-purpose tracing SDK integration.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger("remind_me_mcp.telemetry")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OTEL_ENABLED: bool = os.environ.get("REMIND_ME_OTEL_ENABLED", "").lower() in ("true", "1", "yes")
"""Set REMIND_ME_OTEL_ENABLED=1 to turn on tracing. Requires the optional
``opentelemetry`` extra (``pip install remind-me-mcp[otel]``); a missing
extra degrades to a no-op with a one-time warning, never a crash."""

OTEL_ENDPOINT: str | None = os.environ.get("REMIND_ME_OTEL_ENDPOINT") or None
"""OTLP/HTTP collector endpoint (e.g. ``http://localhost:4318/v1/traces``).
Unset uses the OTLP exporter's own default (``http://localhost:4318``)."""

OTEL_SERVICE_NAME: str = os.environ.get("REMIND_ME_OTEL_SERVICE_NAME", "remind-me-mcp")
"""``service.name`` resource attribute reported to the collector."""

# ---------------------------------------------------------------------------
# Lazy tracer initialization
# ---------------------------------------------------------------------------

_tracer: Any = None
_init_attempted = False


def _get_tracer() -> Any:
    """Lazily build and cache an OTEL tracer, or None if tracing is unavailable.

    Best-effort and permanent for the process lifetime: any failure (extra
    not installed, bad endpoint, SDK error) logs once and disables tracing
    for the rest of the run — telemetry must never be able to break the
    server it's observing. Reads module attributes at call time so tests
    can monkeypatch ``OTEL_ENABLED``.

    Returns:
        An OTEL ``Tracer``, or None when disabled/unavailable.
    """
    global _tracer, _init_attempted
    if _init_attempted:
        return _tracer
    _init_attempted = True
    if not OTEL_ENABLED:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": OTEL_SERVICE_NAME}))
        exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT) if OTEL_ENDPOINT else OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("remind_me_mcp")
        log.info(
            "OpenTelemetry tracing enabled (service=%s, endpoint=%s)",
            OTEL_SERVICE_NAME,
            OTEL_ENDPOINT or "default",
        )
    except ImportError:
        log.warning(
            "REMIND_ME_OTEL_ENABLED is set but the 'otel' extra isn't installed "
            "-- run: pip install remind-me-mcp[otel]. Tracing disabled."
        )
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break startup
        log.warning("OpenTelemetry setup failed (%s); tracing disabled.", exc)
    return _tracer


def is_enabled() -> bool:
    """Return True when a real tracer is active (used by remind_me_server_status).

    Triggers the same lazy, once-only initialization as :func:`maybe_span`.
    """
    return _get_tracer() is not None


# ---------------------------------------------------------------------------
# Span context manager
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def maybe_span(name: str, **attributes: Any) -> Iterator[None]:
    """Open a tracing span named *name*, or do nothing if tracing is off.

    A plain synchronous context manager works inside both ``async def`` tool
    handlers (``with maybe_span(...):`` doesn't need ``async with`` — it
    isn't awaiting anything itself) and the sync background loops
    (sync.py, watcher.py), so one implementation covers every call site.

    Args:
        name: Span name (e.g. ``'tool.remind_me_search'``, ``'sync.cycle'``,
            ``'watcher.scan'``, ``'webhook.ingest'``).
        **attributes: Optional span attributes. Keep these small — they are
            exported to the collector.

    Yields:
        None. An exception raised inside the block is recorded on the span
        (when tracing is active) and always re-raised unchanged.
    """
    tracer = _get_tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            raise


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "OTEL_ENABLED",
    "OTEL_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "maybe_span",
    "is_enabled",
]
