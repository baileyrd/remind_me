"""
Tests for remind_me_mcp.audio_import — the "audio" transcription connector
(FT-32, issue #192).

faster-whisper (the 'audio' extra) is optional; every test here skips
gracefully (not fails) when it isn't installed, matching the [pdf]/[image]
extras' own skip convention (test_pdf_import.py/test_image_import.py).

Fixture generation: a genuinely real, spoken-word test clip is generated at
test time with the `espeak-ng` command-line speech synthesizer (a system
binary, NOT part of the 'audio' pip extra — faster-whisper itself needs no
system binary at all, see audio_import.py's module docstring) rendering a
short, clearly-enunciated phrase, then actually transcribed end-to-end with
faster-whisper's real "tiny" model (smaller/faster than the shipped default
"base", chosen here purely to keep the test suite fast — this is a test
tuning choice, not a change to config.AUDIO_MODEL_SIZE's own default).
`espeak-ng` was verified present and working in the sandbox this change was
developed in (`apt-get install espeak-ng`), but is NOT guaranteed present in
every CI/dev environment this suite runs in, so every real-audio test is
additionally gated on `shutil.which("espeak-ng")` and skips cleanly (not
fails) when it's absent — mirroring this module's own "skip, don't fail, on
a missing optional piece" discipline one level up the stack.

Segment-based chunking (multiple segments -> multiple chunks with distinct
timestamp metadata), and the searchable-round-trip test, are exercised
against a *mocked* `_get_whisper_model()` return value instead of real
transcription output. This is a deliberate choice, not a shortcut taken
without trying the real thing first: during development, real
faster-whisper/CTranslate2 CPU inference on the exact same `espeak-ng` WAV
bytes was run back-to-back many times and produced genuinely different
transcriptions run to run (e.g. "The quick brown fox jump over the lazy
dog." vs. "The C-Round Fogs jump over the lazy dog." from byte-identical
input, even with the decode language pinned) — CTranslate2's multi-threaded
CPU matmul reduction order is not guaranteed bit-stable across runs, and a
synthesized, somewhat-robotic voice sits close enough to the model's
decision boundary that the resulting floating-point noise can occasionally
flip a word or two. That's a real, load-bearing finding about the chosen
library's behavior on CPU, not a reason to avoid testing it — so:

- `test_transcribe_segments_real_speech` below IS real, real audio, real
  model, real transcription, gated on `espeak-ng` being available — it
  verifies actual library integration (bytes in, non-trivial timed text
  out) but deliberately asserts on *shape* (non-empty text, sane
  monotonic timestamps) rather than exact wording, for the reason above.
- Tests that need EXACT, stable content (multi-segment chunking with
  precise start/end pairs; the FTS5 search round-trip, which needs a
  specific searchable term to reliably land in the index) mock
  `_get_whisper_model()`'s return value instead, isolating the
  connector's own chunking/storage/search logic — which IS fully
  deterministic — from ASR content variance that isn't. This mirrors
  issue #181's own documented fallback (mock at the library boundary when
  a fully real fixture isn't practically achievable) and matches how
  `test_image_import.py` mocks `RapidOCR(**kwargs)` for its
  model-path-passthrough tests rather than re-deriving real OCR output.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import wave
from typing import TYPE_CHECKING

import pytest

faster_whisper = pytest.importorskip(
    "faster_whisper", reason="faster-whisper (the 'audio' extra) not installed"
)

from remind_me_mcp import audio_import as _audio_mod  # noqa: E402
from remind_me_mcp.audio_import import (  # noqa: E402
    AUDIO_EXTRA_INSTALL_MSG,
    _audio_connector,
    _transcribe_segments,
)
from remind_me_mcp.importer import import_chat_file, import_content  # noqa: E402

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ESPEAK_AVAILABLE = shutil.which("espeak-ng") is not None
_TEST_WHISPER_MODEL = "tiny"  # smaller/faster than config.AUDIO_MODEL_SIZE's "base" default -- test speed only


def _make_speech_wav(text: str, tmp_path: Path, name: str = "speech.wav") -> bytes:
    """Synthesize *text* to a real WAV file via espeak-ng and return its bytes."""
    out = tmp_path / name
    subprocess.run(
        ["espeak-ng", "-v", "en", "-s", "140", "-w", str(out), text],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def _make_silent_wav(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """A real, valid, silent WAV file -- no speech to transcribe."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _small_fast_test_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module uses the tiny model (test speed), never
    whatever config.AUDIO_MODEL_SIZE resolves to in the ambient environment,
    and always starts from a clean (unloaded) singleton so tests don't leak
    a loaded model -- or a cached missing-dependency verdict -- into each
    other (mirrors test_image_import.py's per-test _ocr_engine resets)."""
    monkeypatch.setattr(_audio_mod, "AUDIO_MODEL_SIZE", _TEST_WHISPER_MODEL)
    monkeypatch.setattr(_audio_mod, "_whisper_model", None)
    monkeypatch.setattr(_audio_mod, "_whisper_deps_missing", False)


# ---------------------------------------------------------------------------
# _transcribe_segments — real faster-whisper transcription
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ESPEAK_AVAILABLE, reason="espeak-ng not installed in this environment")
def test_transcribe_segments_real_speech(tmp_path: Path) -> None:
    """Genuinely real audio through the genuinely real library, asserting on
    *shape* rather than exact wording -- see this module's docstring for the
    observed CTranslate2 CPU nondeterminism that makes exact-wording
    assertions against synthesized speech flaky, not a hypothetical
    concern."""
    wav_bytes = _make_speech_wav("The quick brown fox jumps over the lazy dog", tmp_path)
    segments = _transcribe_segments(wav_bytes)
    assert len(segments) >= 1
    full_text = " ".join(text for text, _start, _end in segments)
    assert len(full_text) >= 10  # non-trivial transcription, not a stray token
    # Real timestamps: monotonic, non-negative, and roughly cover the ~3.5s clip.
    for _text, start, end in segments:
        assert 0.0 <= start < end <= 10.0


@pytest.mark.skipif(not ESPEAK_AVAILABLE, reason="espeak-ng not installed in this environment")
def test_transcribe_segments_silence_yields_no_segments() -> None:
    assert _transcribe_segments(_make_silent_wav()) == []


def test_transcribe_segments_raises_clear_error_for_garbage_bytes() -> None:
    with pytest.raises(RuntimeError, match="Could not transcribe audio"):
        _transcribe_segments(b"not an audio file at all, just garbage bytes")


# ---------------------------------------------------------------------------
# _audio_connector — per-segment chunking with start/end metadata
# ---------------------------------------------------------------------------


class _FakeSegment:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeWhisperModel:
    """Stands in for a real WhisperModel, returning pre-scripted segments --
    the mocking boundary this module's docstring documents for deterministic
    multi-segment coverage."""

    def __init__(self, segments: list[_FakeSegment]) -> None:
        self._segments = segments

    def transcribe(self, _audio: object, **_kwargs: object) -> tuple[list[_FakeSegment], object]:
        return self._segments, object()


def test_audio_connector_chunks_per_segment_with_timestamp_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWhisperModel([
        _FakeSegment("  Hello there.  ", 0.0, 1.5),
        _FakeSegment("How are you today?", 1.5, 3.2),
    ])
    monkeypatch.setattr(_audio_mod, "_get_whisper_model", lambda: fake)

    parsed, raw_entries = _audio_connector(
        "", {"suffix": ".wav", "extract_mode": "assistant_messages",
             "max_length": 10000, "raw_bytes": b"irrelevant-fake-bytes"}
    )
    assert raw_entries == 2
    assert len(parsed) == 2
    assert parsed[0] == ("Hello there.", {"start": 0.0, "end": 1.5})
    assert parsed[1] == ("How are you today?", {"start": 1.5, "end": 3.2})


def test_audio_connector_empty_segment_list_yields_no_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_audio_mod, "_get_whisper_model", lambda: _FakeWhisperModel([]))
    parsed, raw_entries = _audio_connector(
        "", {"suffix": ".wav", "extract_mode": "assistant_messages",
             "max_length": 10000, "raw_bytes": b"silence"}
    )
    assert parsed == []
    assert raw_entries == 0


def test_audio_connector_splits_long_segment_into_multiple_chunks_same_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment whose text exceeds max_length is split by _chunk_text like an
    oversized PDF page -- every sub-chunk still carries that segment's full
    start/end range (mirrors _pdf_connector's page-number precedent)."""
    long_text = " ".join(["word"] * 50)  # ~250 chars
    fake = _FakeWhisperModel([_FakeSegment(long_text, 10.0, 25.0)])
    monkeypatch.setattr(_audio_mod, "_get_whisper_model", lambda: fake)

    parsed, raw_entries = _audio_connector(
        "", {"suffix": ".wav", "extract_mode": "assistant_messages",
             "max_length": 50, "raw_bytes": b"irrelevant"}
    )
    assert raw_entries == 1
    assert len(parsed) > 1
    assert all(meta == {"start": 10.0, "end": 25.0} for _c, meta in parsed)
    assert all(len(c) <= 50 for c, _meta in parsed)


# ---------------------------------------------------------------------------
# Missing-dependency error path
# ---------------------------------------------------------------------------


def test_get_whisper_model_missing_dependency_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(RuntimeError, match=re.escape(AUDIO_EXTRA_INSTALL_MSG)):
        _audio_mod._get_whisper_model()


def test_import_chat_file_missing_audio_dependency_raises_actionable_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_file = tmp_path / "needs_extra.wav"
    audio_file.write_bytes(_make_silent_wav())
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(RuntimeError, match=re.escape(AUDIO_EXTRA_INSTALL_MSG)):
        import_chat_file(str(audio_file), "", [], "assistant_messages", 10000)


async def test_admin_tool_surfaces_missing_audio_dependency_as_clean_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from remind_me_mcp.models import ChatImportInput
    from remind_me_mcp.tools import memory_import_chat

    audio_file = tmp_path / "needs_extra2.wav"
    audio_file.write_bytes(_make_silent_wav())
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    result_str = await memory_import_chat(ChatImportInput(file_path=str(audio_file)))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert AUDIO_EXTRA_INSTALL_MSG in result["error"]


# ---------------------------------------------------------------------------
# kind validation (FT-32 additions to _validate_kind_and_suffix)
# ---------------------------------------------------------------------------


def test_audio_kind_forced_on_non_audio_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    result = import_content(b"whatever", "f.txt", "test", [], "assistant_messages", 10000, kind="audio")
    assert result["status"] == "error"
    assert "audio import requires one of" in result["reason"]


def test_chat_kind_forced_on_audio_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    result = import_content(_make_silent_wav(), "f.wav", "test", [], "assistant_messages", 10000, kind="chat")
    assert result["status"] == "error"
    assert "must use kind='audio' or 'auto'" in result["reason"]


# ---------------------------------------------------------------------------
# Full pipeline: hash dedup, kind=auto routing, storage shape, search
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ESPEAK_AVAILABLE, reason="espeak-ng not installed in this environment")
def test_import_chat_file_auto_routes_audio_and_stores_segment_metadata(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Real end-to-end transcription through the full import pipeline,
    asserting on structure (kind/category/source/doc_id/timestamp shape) --
    not exact transcribed wording, per this module's docstring."""
    wav_bytes = _make_speech_wav("The quick brown fox jumps over the lazy dog", tmp_path)
    audio_file = tmp_path / "notes.wav"
    audio_file.write_bytes(wav_bytes)

    result = import_chat_file(str(audio_file), "", [], "assistant_messages", 10000)  # kind=auto (default)
    assert result["status"] == "ok"
    assert result["kind"] == "audio"
    assert result["memories_created"] >= 1

    rows = db_conn.execute(
        "SELECT content, category, source, metadata, doc_id, chunk_index "
        "FROM memories WHERE source = 'audio_import' ORDER BY chunk_index"
    ).fetchall()
    assert len(rows) == result["memories_created"]
    assert all(r["category"] == "audio" for r in rows)
    assert all(r["doc_id"] == result["import_id"] for r in rows)
    for r in rows:
        assert r["content"].strip()  # non-empty transcribed text
        meta = json.loads(r["metadata"])
        assert "start" in meta and "end" in meta
        assert meta["start"] < meta["end"]


@pytest.mark.skipif(not ESPEAK_AVAILABLE, reason="espeak-ng not installed in this environment")
def test_import_chat_file_audio_dedups_by_hash(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    wav_bytes = _make_speech_wav("Dedup content here", tmp_path)
    audio_file = tmp_path / "dedup.wav"
    audio_file.write_bytes(wav_bytes)

    first = import_chat_file(str(audio_file), "", [], "assistant_messages", 10000)
    assert first["status"] == "ok"

    second = import_chat_file(str(audio_file), "", [], "assistant_messages", 10000)
    assert second["status"] == "skipped"
    assert second["import_id"] == first["import_id"]


@pytest.mark.skipif(not ESPEAK_AVAILABLE, reason="espeak-ng not installed in this environment")
def test_import_chat_file_mp3_extension_also_routes_to_audio(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """.mp3 (a compressed container faster-whisper decodes via its own
    bundled PyAV/ffmpeg -- no system ffmpeg binary needed) round-trips the
    same as .wav (see audio_import.py's module docstring for why this
    format-agnostic decoding was the deciding factor in the library choice)."""
    import av

    wav_bytes = _make_speech_wav("Testing empty pockets", tmp_path)
    wav_path = tmp_path / "src.wav"
    wav_path.write_bytes(wav_bytes)
    mp3_path = tmp_path / "notes.mp3"

    inp = av.open(str(wav_path))
    out = av.open(str(mp3_path), mode="w")
    in_stream = inp.streams.audio[0]
    out_stream = out.add_stream("mp3", rate=in_stream.rate)
    for frame in inp.decode(in_stream):
        for packet in out_stream.encode(frame):
            out.mux(packet)
    for packet in out_stream.encode(None):
        out.mux(packet)
    out.close()
    inp.close()

    result = import_chat_file(str(mp3_path), "", [], "assistant_messages", 10000)
    assert result["status"] == "ok"
    assert result["kind"] == "audio"


async def test_audio_import_is_searchable_round_trip(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcribed audio content participates in FTS5 dedup/search like any
    other connector's output -- round-tripped through remind_me_search.

    Mocks `_get_whisper_model()` (see this module's docstring) so the
    searched-for term is guaranteed exact and stable -- this test is about
    the storage/search pipeline downstream of transcription, not about
    ASR accuracy itself, which the (real-audio, shape-only) tests above
    already cover."""
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools import memory_search

    fake = _FakeWhisperModel([_FakeSegment("This recording mentions ZebraTranscriptionTerm", 0.0, 2.0)])
    monkeypatch.setattr(_audio_mod, "_get_whisper_model", lambda: fake)

    audio_file = tmp_path / "searchable.wav"
    audio_file.write_bytes(b"fake-audio-bytes-never-actually-decoded")

    result = import_chat_file(str(audio_file), "", [], "assistant_messages", 10000)
    assert result["status"] == "ok"

    search_result = await memory_search(
        MemorySearchInput(query="ZebraTranscriptionTerm", response_format=ResponseFormat.JSON)
    )
    payload = json.loads(search_result)
    assert payload["returned"] >= 1
    assert any("ZebraTranscriptionTerm" in m["content"] for m in payload["memories"])
