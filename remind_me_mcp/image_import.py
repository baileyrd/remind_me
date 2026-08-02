"""
remind_me_mcp.image_import — OCR image connector (FT-19, issue #181).

Registers an "image" import kind (:func:`~remind_me_mcp.importer.register_connector`)
that OCRs a `.png`/`.jpg`/`.jpeg` file via
`RapidOCR <https://github.com/RapidAI/RapidOCR>`_'s ``onnxruntime`` backend
and stores the recognized text as a single memory (one chunk per image —
the issue explicitly allows this simpler shape rather than one memory per
detected text region).

**Why RapidOCR over pytesseract**: this codebase already ships an ONNX
runtime dependency for the embedder and reranker (see ``embeddings.py``/
``reranker.py``), so an ONNX-based OCR engine is another model on
infrastructure already present rather than a new runtime family. RapidOCR's
detection/classification/recognition models ship *inside* the pip package
itself — unlike this codebase's embedder/reranker, which download their
model from HuggingFace Hub on first use, RapidOCR needs no network access
and no model cache directory. The tradeoff considered and rejected:
``pytesseract`` additionally requires the system ``tesseract`` binary (not
installable via pip, and not present in this project's CI/sandbox images),
making it a strictly heavier install for an equivalent capability here.

Requires the optional ``image`` extra (``pip install remind-me-mcp[image]``).
The import is deferred to :func:`_get_ocr_engine` so this module (and the
``image`` kind's mere registration) never requires rapidocr-onnxruntime to
be installed — only actually importing an image file does, at which point a
missing dependency raises ``RuntimeError`` with an actionable install
message (mirrors ``embeddings.py``/``reranker.py``'s "install this extra"
phrasing), not a bare ``ModuleNotFoundError`` traceback.

This module is wired in by ``tools/admin.py`` importing it for its
registration side effect (the same shape ``mempalace_import.py``/
``dbs_import.py`` use), never by ``importer.py`` itself — keeping the
"third-party module registers without touching importer.py" property intact
even though ``image`` is a first-class :data:`~remind_me_mcp.importer.IMPORT_KINDS`
member.

Ingestion itself (hash dedup, chunk storage, batched embedding) is entirely
handled by :func:`remind_me_mcp.importer._ingest_parsed` — this module only
turns image bytes into a single ``(chunk_content, chunk_metadata)`` pair.

**Language coverage (issue #202)**: :func:`_get_ocr_engine` constructs
``RapidOCR()`` with no arguments, so it loads the models bundled inside the
``rapidocr-onnxruntime`` wheel — ``ch_PP-OCRv4`` detection/recognition plus
``ch_ppocr_mobile_v2.0`` orientation classification. That recognition
model's character set (baked into its ONNX metadata) covers Chinese and
English/Latin script + digits only; other scripts (Japanese, Korean,
Arabic, Cyrillic, Devanagari, ...) are not recognized. ``REMIND_ME_OCR_
DET_MODEL_PATH``/``_CLS_MODEL_PATH``/``_REC_MODEL_PATH`` (see
``config.py``) are an optional passthrough to RapidOCR's own
``det_model_path``/``cls_model_path``/``rec_model_path`` constructor
kwargs — unset by default, so behavior is unchanged unless a user points
them at an alternate-script model downloaded from RapidOCR's model zoo.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from remind_me_mcp.config import OCR_CLS_MODEL_PATH, OCR_DET_MODEL_PATH, OCR_REC_MODEL_PATH
from remind_me_mcp.importer import register_connector

log = logging.getLogger("remind_me_mcp.image_import")

IMAGE_EXTRA_INSTALL_MSG = (
    "Image import requires the 'image' extra: pip install remind-me-mcp[image]"
)
"""User-facing error message for a missing rapidocr-onnxruntime dependency
(matches the "install this extra" phrasing used by embeddings.py/reranker.py)."""

# Lazy singleton, mirroring embeddings._Embedder / reranker.CrossEncoderReranker:
# RapidOCR() construction loads its ONNX detection/classification/recognition
# sessions, so it's done once per process and reused, not per call. Missing
# dependencies are a permanent-for-this-process failure (like _deps_missing
# in embeddings.py) since a pip package that isn't installed doesn't become
# installed mid-run.
_ocr_engine: Any = None
_ocr_deps_missing = False
_ocr_engine_lock = threading.Lock()


def _get_ocr_engine() -> Any:
    """Get or lazily construct the module-level RapidOCR engine singleton.

    Thread-safe (mirrors embeddings._Embedder._ensure_loaded, issue #153):
    concurrent first-callers block on one real construction instead of
    racing separate ONNX session loads.

    Raises:
        RuntimeError: rapidocr-onnxruntime (the 'image' extra) is not
            installed. Cached: once observed missing, every subsequent call
            in this process fails fast without re-attempting the import.
    """
    global _ocr_engine, _ocr_deps_missing
    if _ocr_deps_missing:
        raise RuntimeError(IMAGE_EXTRA_INSTALL_MSG)
    if _ocr_engine is not None:
        return _ocr_engine
    with _ocr_engine_lock:
        if _ocr_engine is not None:  # a concurrent caller may have just finished
            return _ocr_engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            _ocr_deps_missing = True
            log.warning(
                "Image import dependency not installed (%s). %s", e, IMAGE_EXTRA_INSTALL_MSG
            )
            raise RuntimeError(IMAGE_EXTRA_INSTALL_MSG) from e
        # Optional passthrough (issue #202) to RapidOCR's own model-path
        # kwargs, so a user can point at an alternate-script detection/
        # classification/recognition model instead of the bundled
        # Chinese+English-only ch_PP-OCRv4 default. All three env vars are
        # unset (None) by default, so kwargs stays empty and this is exactly
        # RapidOCR() -- today's behavior, unchanged.
        kwargs: dict[str, str] = {}
        if OCR_DET_MODEL_PATH:
            kwargs["det_model_path"] = OCR_DET_MODEL_PATH
        if OCR_CLS_MODEL_PATH:
            kwargs["cls_model_path"] = OCR_CLS_MODEL_PATH
        if OCR_REC_MODEL_PATH:
            kwargs["rec_model_path"] = OCR_REC_MODEL_PATH
        _ocr_engine = RapidOCR(**kwargs)
        return _ocr_engine


def _extract_image_text(raw_bytes: bytes) -> str:
    """OCR an image's raw bytes into a single text blob.

    RapidOCR returns one ``(box, text, confidence)`` triple per detected text
    region (in reading order); the recognized lines are joined with newlines
    into one blob per the issue's "whole image as one chunk" default —
    bounding boxes/confidences aren't otherwise used here.

    Args:
        raw_bytes: The full raw bytes of a .png/.jpg/.jpeg file.

    Returns:
        The recognized text, or "" if no text was detected.

    Raises:
        RuntimeError: rapidocr-onnxruntime is not installed, or OCR failed
            on these bytes (e.g. not a decodable image).
    """
    engine = _get_ocr_engine()
    try:
        result, _elapse = engine(raw_bytes)
    except RuntimeError:
        raise
    except Exception as e:  # Broad catch intentional: opencv/onnxruntime raise
        # varied, non-stdlib exceptions for a corrupt/undecodable image —
        # surface one consistent, actionable error instead of an arbitrary
        # exception type escaping to the caller.
        raise RuntimeError(f"Could not OCR image: {e}") from e
    if not result:
        return ""
    return "\n".join(item[1] for item in result)


def _image_connector(
    raw: str, meta: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Built-in ``image`` connector (FT-19): OCR the whole image into one chunk.

    ``raw`` (the lossily UTF-8-decoded file content every connector receives
    positionally, per the :class:`~remind_me_mcp.importer.Connector` protocol)
    is ignored here — an image is binary, and importer.py's own decode step
    would have already corrupted it. Instead this connector reads the
    original bytes from ``meta["raw_bytes"]``, which importer.py threads
    through specifically for binary connectors (see its FT-19 module
    docstring note).
    """
    raw_bytes = meta["raw_bytes"]
    text = _extract_image_text(raw_bytes).strip()
    if not text:
        return [], 0
    return [(text, {})], 1


register_connector("image", _image_connector)


__all__ = [
    "IMAGE_EXTRA_INSTALL_MSG",
    "_get_ocr_engine",
    "_extract_image_text",
    "_image_connector",
]
