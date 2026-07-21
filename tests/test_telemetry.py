"""
Tests for remind_me_mcp.telemetry — optional OpenTelemetry instrumentation
(cognee gap #9, Phase 7a).

Disabled-path tests never require the ``opentelemetry`` package (the whole
point of the graceful-degradation design). Tests that exercise a real
tracer use ``pytest.importorskip("opentelemetry")`` and an
``InMemorySpanExporter`` so nothing touches the network.
"""

from __future__ import annotations

import sys

import pytest

import remind_me_mcp.telemetry as tel


@pytest.fixture(autouse=True)
def _reset_telemetry_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a fresh, disabled, uninitialized module state."""
    monkeypatch.setattr(tel, "OTEL_ENABLED", False)
    monkeypatch.setattr(tel, "_init_attempted", False)
    monkeypatch.setattr(tel, "_tracer", None)


# ---------------------------------------------------------------------------
# Disabled / no-op path (no opentelemetry package required)
# ---------------------------------------------------------------------------


def test_maybe_span_is_noop_when_disabled() -> None:
    entered = False
    with tel.maybe_span("test.span"):
        entered = True
    assert entered


def test_maybe_span_disabled_does_not_import_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled (the default) never even attempts the opentelemetry import."""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with tel.maybe_span("test.span"):
        pass  # would raise ImportError inside _get_tracer if it were reached


def test_maybe_span_propagates_exceptions_when_disabled() -> None:
    with pytest.raises(ValueError, match="boom"), tel.maybe_span("test.span"):
        raise ValueError("boom")


def test_is_enabled_false_when_disabled() -> None:
    assert tel.is_enabled() is False


def test_get_tracer_returns_none_when_disabled() -> None:
    assert tel._get_tracer() is None


def test_get_tracer_caches_after_first_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy init only runs once per process — subsequent calls are cheap."""
    assert tel._init_attempted is False
    tel._get_tracer()
    assert tel._init_attempted is True

    # Flip OTEL_ENABLED after the fact -- if _get_tracer() re-checked it, this
    # would attempt a real init; the cached None must be returned instead.
    monkeypatch.setattr(tel, "OTEL_ENABLED", True)
    assert tel._get_tracer() is None


# ---------------------------------------------------------------------------
# Graceful degradation when the 'otel' extra isn't installed
# ---------------------------------------------------------------------------


def test_get_tracer_degrades_gracefully_without_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the 'otel' extra not being installed, regardless of whether
    it's actually present in this environment."""
    monkeypatch.setattr(tel, "OTEL_ENABLED", True)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    tracer = tel._get_tracer()

    assert tracer is None
    assert tel.is_enabled() is False


def test_maybe_span_noop_when_package_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tel, "OTEL_ENABLED", True)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    entered = False
    with tel.maybe_span("test.span"):
        entered = True
    assert entered


# ---------------------------------------------------------------------------
# Real tracer path (requires the 'otel' extra)
# ---------------------------------------------------------------------------


def _in_memory_tracer():
    """Build a real OTEL tracer wired to an InMemorySpanExporter (no network)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_maybe_span_records_a_real_span(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry")
    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(tel, "_init_attempted", True)
    monkeypatch.setattr(tel, "_tracer", tracer)

    with tel.maybe_span("test.span", foo="bar"):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "test.span"
    assert spans[0].attributes["foo"] == "bar"


def test_maybe_span_records_exception_on_real_span(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry")
    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(tel, "_init_attempted", True)
    monkeypatch.setattr(tel, "_tracer", tracer)

    with pytest.raises(ValueError, match="boom"), tel.maybe_span("test.span"):
        raise ValueError("boom")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    events = spans[0].events
    assert any(e.name == "exception" for e in events)


def test_get_tracer_degrades_gracefully_on_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ImportError failure during setup (e.g. a malformed endpoint)
    also degrades to a no-op instead of raising out of _get_tracer()."""
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace import TracerProvider

    def boom(*args, **kwargs):
        raise RuntimeError("setup exploded")

    monkeypatch.setattr(TracerProvider, "__init__", boom)
    monkeypatch.setattr(tel, "OTEL_ENABLED", True)

    tracer = tel._get_tracer()

    assert tracer is None
    assert tel.is_enabled() is False


def test_get_tracer_real_init_succeeds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercises the real success path of _get_tracer() (construction only —
    no flush/shutdown, so no network call actually happens)."""
    pytest.importorskip("opentelemetry")
    monkeypatch.setattr(tel, "OTEL_ENABLED", True)

    tracer = tel._get_tracer()

    assert tracer is not None
    assert tel.is_enabled() is True
