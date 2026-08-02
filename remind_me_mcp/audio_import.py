"""
remind_me_mcp.audio_import — audio transcription connector (FT-32, issue #192).

Registers an "audio" import kind (:func:`~remind_me_mcp.importer.register_connector`)
that transcribes a ``.mp3``/``.m4a``/``.wav``/``.ogg`` file via
`faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_ (a CTranslate2
re-implementation of OpenAI's Whisper) and chunks the result **per transcript
segment** — Whisper's own sentence/phrase-level output unit, each with a
start/end timestamp — mirroring how ``pdf_import.py`` uses the page number as
chunk context, but with a ``{"start": <float seconds>, "end": <float
seconds>}`` timestamp range instead.

**Library choice, in the order actually tried (issue #192 asked for this to
be researched and verified, not assumed):**

1. **An ONNX Whisper option first**, to stay on the ONNX runtime this
   codebase already depends on for the embedder/reranker/OCR
   (``embeddings.py``/``reranker.py``/``image_import.py``). The best fit
   found and installed was `onnx-asr <https://github.com/istupakov/onnx-asr>`_,
   which does support a Whisper model over ``onnxruntime`` and downloads it
   from HuggingFace Hub. It was rejected after actually installing and
   testing it: its audio loader only accepts raw PCM (WAV) — it has no
   built-in decoder for compressed containers at all, so ``.mp3``/``.m4a``/
   ``.ogg`` (three of this issue's four required extensions) would need a
   *second* new dependency just for decoding, which is worse than one
   well-chosen dependency that already handles every required format.
2. **pywhispercpp** (Python bindings for whisper.cpp/GGML — lighter-weight
   than a full CTranslate2 or PyTorch runtime, so it was tried next).
   Actually installed and run in this sandbox: model loading and inference
   on a plain 16kHz WAV worked fine (a real ``espeak-ng``-synthesized clip
   transcribed correctly, ~147MB ``base`` GGML model, comparable size to
   faster-whisper's own ``base``). It was rejected once ``.mp3`` was tried,
   reproducibly: pywhispercpp's own audio loader only accepts 16kHz mono WAV
   directly, and for anything else — including a non-16kHz WAV, and
   unconditionally for ``.mp3``/``.m4a``/``.ogg`` — it shells out to a
   **system** ``ffmpeg`` binary (``shutil.which('ffmpeg')``); this sandbox
   has none installed, and the actual failure observed was
   ``Exception: FFMPEG is not installed or not in PATH.`` That reintroduces
   exactly the class of dependency this codebase's own precedent already
   rejected once (``pdf_import.py`` chose pure-Python ``pypdf`` over
   poppler-utils; ``image_import.py`` chose RapidOCR over pytesseract's
   system ``tesseract`` binary) — a pip-only install can no longer be relied
   on to transcribe three of this issue's four required formats.
3. **faster-whisper** (chosen). Verified end-to-end in this sandbox with the
   same real synthesized speech clip transcribed correctly, including from
   an in-memory ``io.BytesIO`` (no temp file needed — see
   :func:`_transcribe_segments`) and from a re-encoded ``.mp3`` of the same
   clip — the exact case that eliminated pywhispercpp. It decodes **every**
   common audio container out of the box via its own bundled
   `PyAV <https://github.com/PyAV-Org/PyAV>`_ (``av``) dependency, which
   ships a statically-linked ffmpeg build inside its own wheel — no system
   ``ffmpeg`` binary required. It also already shares three of its four
   dependencies (``onnxruntime``, ``huggingface-hub``, ``tokenizers``) with
   this project's own ``[semantic]`` extra, even though its core Whisper
   inference itself runs on CTranslate2, not ONNX directly.
4. **openai-whisper** (not needed). The heavier, plain-PyTorch reference
   implementation was the documented last-resort fallback if none of the
   above panned out; faster-whisper worked cleanly, so it was never tried.

**Model size, chosen deliberately smaller than Whisper's largest**: the
default (:data:`~remind_me_mcp.config.AUDIO_MODEL_SIZE` = ``"base"``, ~145MB
int8-quantized, ~74M parameters) trades some transcription accuracy for a
small download and fast CPU-only inference — consistent with this being a
local-first tool's DEFAULT, not its ceiling. ``REMIND_ME_AUDIO_MODEL`` lets a
user opt into ``small``/``medium``/``large-v3``/etc. with no code change,
exactly like ``REMIND_ME_EMBEDDING_MODEL``/``REMIND_ME_RERANK_MODEL`` do for
their own models.

Requires the optional ``audio`` extra (``pip install remind-me-mcp[audio]``).
The import is deferred to :func:`_get_whisper_model` so this module (and the
``audio`` kind's mere registration) never requires faster-whisper to be
installed — only actually importing an audio file does, at which point a
missing dependency raises ``RuntimeError`` with an actionable install
message (mirrors ``embeddings.py``/``reranker.py``/``pdf_import.py``/
``image_import.py``'s "install this extra" phrasing), not a bare
``ModuleNotFoundError`` traceback.

This module is wired in by ``tools/admin.py`` importing it for its
registration side effect (the same shape ``pdf_import.py``/``image_import.py``
use), never by ``importer.py`` itself — keeping the "third-party module
registers without touching importer.py" property intact even though
``audio`` is a first-class :data:`~remind_me_mcp.importer.IMPORT_KINDS` member.

Ingestion itself (hash dedup, chunk storage, batched embedding) is entirely
handled by :func:`remind_me_mcp.importer._ingest_parsed` — this module only
turns audio bytes into ``(chunk_content, chunk_metadata)`` pairs.

**Segmentation is not optional here, unlike the issue's stated fallback.**
faster-whisper's ``transcribe()`` always returns its result as a sequence of
timestamped segments — there is no "single transcript blob" mode to fall
back to, so :func:`_audio_connector` always has segment boundaries to chunk
on. The one-chunk-per-file fallback the issue anticipates (for a
hypothetical transcription backend with no segmentation) is therefore not
implemented: it would be dead code against the library actually chosen.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any

from remind_me_mcp.config import AUDIO_MODEL_SIZE, MODEL_DIR
from remind_me_mcp.importer import _chunk_text, register_connector

log = logging.getLogger("remind_me_mcp.audio_import")

AUDIO_EXTRA_INSTALL_MSG = "Audio import requires the 'audio' extra: pip install remind-me-mcp[audio]"
"""User-facing error message for a missing faster-whisper dependency (matches
the "install this extra" phrasing used by embeddings.py/reranker.py/
pdf_import.py/image_import.py)."""

# Lazy singleton, mirroring embeddings._Embedder / image_import's module-level
# RapidOCR engine: constructing a WhisperModel loads (and, on first use ever,
# downloads) real model weights, so it's done once per process and reused,
# not per call. Missing dependencies are a permanent-for-this-process failure
# (like _deps_missing in embeddings.py) since a pip package that isn't
# installed doesn't become installed mid-run.
_whisper_model: Any = None
_whisper_deps_missing = False
_whisper_model_lock = threading.Lock()


def _get_whisper_model() -> Any:
    """Get or lazily construct the module-level faster-whisper model singleton.

    Thread-safe (mirrors image_import._get_ocr_engine): concurrent
    first-callers block on one real model load instead of racing separate
    downloads/loads of the same weights.

    Raises:
        RuntimeError: faster-whisper (the 'audio' extra) is not installed.
            Cached: once observed missing, every subsequent call in this
            process fails fast without re-attempting the import.
    """
    global _whisper_model, _whisper_deps_missing
    if _whisper_deps_missing:
        raise RuntimeError(AUDIO_EXTRA_INSTALL_MSG)
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_model_lock:
        if _whisper_model is not None:  # a concurrent caller may have just finished
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            _whisper_deps_missing = True
            log.warning(
                "Audio import dependency not installed (%s). %s", e, AUDIO_EXTRA_INSTALL_MSG
            )
            raise RuntimeError(AUDIO_EXTRA_INSTALL_MSG) from e
        # Downloaded from HuggingFace Hub on first use and cached under
        # MODEL_DIR, exactly like the embedder/reranker's own ONNX models
        # (see config.AUDIO_MODEL_SIZE's docstring) -- CPU-only, matching
        # this codebase's other in-process models (embeddings.py/
        # reranker.py/image_import.py all hardcode CPU execution too, with
        # no GPU-device env var of their own).
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Loading Whisper model: %s", AUDIO_MODEL_SIZE)
        _whisper_model = WhisperModel(
            AUDIO_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            download_root=str(MODEL_DIR),
        )
        log.info("Whisper model loaded (%s)", AUDIO_MODEL_SIZE)
        return _whisper_model


def _transcribe_segments(raw_bytes: bytes) -> list[tuple[str, float, float]]:
    """Transcribe raw audio bytes into ``(text, start, end)`` segments.

    Passes an in-memory :class:`io.BytesIO` straight to faster-whisper (which
    decodes it via its own bundled PyAV/ffmpeg) — no temporary file is
    written to disk. ``vad_filter=True`` runs faster-whisper's bundled
    Silero VAD model (shipped inside the package itself, no extra download)
    first, so silent stretches of a recording don't produce spurious
    low-confidence segments.

    Args:
        raw_bytes: The full raw bytes of an .mp3/.m4a/.wav/.ogg file.

    Returns:
        One ``(text, start_seconds, end_seconds)`` tuple per detected speech
        segment, in chronological order, with segments that transcribed to
        no text (rare after VAD filtering, but possible) omitted. Empty list
        for a file with no detected speech.

    Raises:
        RuntimeError: faster-whisper is not installed, or the bytes don't
            decode as audio (e.g. not a real audio file).
    """
    model = _get_whisper_model()
    try:
        segments, _info = model.transcribe(io.BytesIO(raw_bytes), vad_filter=True)
        return [
            (text, segment.start, segment.end)
            for segment in segments
            if (text := segment.text.strip())
        ]
    except RuntimeError:
        raise
    except Exception as e:  # Broad catch intentional: PyAV/ctranslate2 raise
        # varied, non-stdlib exceptions (av.error.InvalidDataError, etc.) for
        # a corrupt/undecodable audio file -- surface one consistent,
        # actionable error rather than letting an arbitrary exception type
        # escape to the caller.
        raise RuntimeError(f"Could not transcribe audio: {e}") from e


def _audio_connector(
    raw: str, meta: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Built-in ``audio`` connector (FT-32): per-segment transcription + chunking.

    ``raw`` (the lossily UTF-8-decoded file content every connector receives
    positionally, per the :class:`~remind_me_mcp.importer.Connector` protocol)
    is ignored here — audio is binary, and importer.py's own decode step
    would have already corrupted it. Instead this connector reads the
    original bytes from ``meta["raw_bytes"]``, which importer.py threads
    through specifically for binary connectors (see its FT-19 module
    docstring note, reused as-is for FT-32).

    A segment whose transcribed text is too long for one memory is further
    split by the shared :func:`~remind_me_mcp.importer._chunk_text`, exactly
    like an oversized PDF page — every resulting sub-chunk still carries that
    segment's full ``start``/``end`` timestamp range (the sub-chunk only
    covers part of that range, but recording the whole segment's range
    mirrors ``pdf_import.py``'s own precedent of an oversized page's
    sub-chunks all keeping that page's single page number).
    """
    raw_bytes = meta["raw_bytes"]
    max_length = meta["max_length"]

    segments = _transcribe_segments(raw_bytes)
    parsed: list[tuple[str, dict[str, Any]]] = []
    for text, start, end in segments:
        chunk_meta = {"start": round(start, 2), "end": round(end, 2)}
        for chunk in _chunk_text(text, max_length):
            parsed.append((chunk, chunk_meta))
    return parsed, len(segments)


register_connector("audio", _audio_connector)


__all__ = [
    "AUDIO_EXTRA_INSTALL_MSG",
    "_get_whisper_model",
    "_transcribe_segments",
    "_audio_connector",
]
