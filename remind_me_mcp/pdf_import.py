"""
remind_me_mcp.pdf_import — PDF document connector (FT-19, issue #181).

Registers a "pdf" import kind (:func:`~remind_me_mcp.importer.register_connector`)
that extracts text per-page via `pypdf <https://pypdf.readthedocs.io/>`_ (a
pure-Python library — no system binary dependency, unlike e.g. poppler-utils),
chunking per-page: the page number plays the same "section" role the
``document`` connector's Markdown heading breadcrumb plays, recorded on each
chunk's metadata as ``{"page": <1-based int>}`` (see
:func:`remind_me_mcp.importer._parse_document` for the heading-context
precedent this mirrors). A page whose extracted text is too long for one
memory is further split by the shared :func:`~remind_me_mcp.importer._chunk_text`,
exactly like an oversized document section — every resulting sub-chunk still
carries that page's number.

Requires the optional ``pdf`` extra (``pip install remind-me-mcp[pdf]``).
The import is deferred to :func:`_extract_pages` so this module (and the
``pdf`` kind's mere registration) never requires pypdf to be installed —
only actually importing a ``.pdf`` file does, at which point a missing
dependency raises ``RuntimeError`` with an actionable install message
(mirrors ``embeddings.py``/``reranker.py``'s "install this extra" phrasing),
not a bare ``ModuleNotFoundError`` traceback.

This module is wired in by ``tools/admin.py`` importing it for its
registration side effect (the same shape ``mempalace_import.py``/
``dbs_import.py`` use), never by ``importer.py`` itself — keeping the
"third-party module registers without touching importer.py" property intact
even though ``pdf`` is a first-class :data:`~remind_me_mcp.importer.IMPORT_KINDS`
member.

Ingestion itself (hash dedup, chunk storage, batched embedding) is entirely
handled by :func:`remind_me_mcp.importer._ingest_parsed` — this module only
turns PDF bytes into ``(chunk_content, chunk_metadata)`` pairs.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from remind_me_mcp.importer import _chunk_text, register_connector

log = logging.getLogger("remind_me_mcp.pdf_import")

PDF_EXTRA_INSTALL_MSG = "PDF import requires the 'pdf' extra: pip install remind-me-mcp[pdf]"
"""User-facing error message for a missing pypdf dependency (matches the
"install this extra" phrasing used by embeddings.py/reranker.py)."""


def _extract_pages(raw_bytes: bytes) -> list[str]:
    """Extract per-page text from PDF bytes via pypdf.

    Args:
        raw_bytes: The full raw bytes of a .pdf file.

    Returns:
        One string per page, in page order (page text may be empty for a
        blank or image-only page — callers drop those before chunking).

    Raises:
        RuntimeError: pypdf (the 'pdf' extra) is not installed, or the bytes
            don't parse as a PDF. Both carry an actionable message rather
            than a bare traceback.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        log.warning("PDF import dependency not installed (%s). %s", e, PDF_EXTRA_INSTALL_MSG)
        raise RuntimeError(PDF_EXTRA_INSTALL_MSG) from e

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        return [(page.extract_text() or "") for page in reader.pages]
    except RuntimeError:
        raise
    except Exception as e:  # Broad catch intentional: pypdf raises varied,
        # non-stdlib exceptions (PdfReadError, DependencyError, etc.) for a
        # corrupt/encrypted/non-PDF file — surface one consistent, actionable
        # error rather than letting an arbitrary pypdf exception type escape.
        raise RuntimeError(f"Could not parse PDF: {e}") from e


def _pdf_connector(
    raw: str, meta: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Built-in ``pdf`` connector (FT-19): per-page text extraction + chunking.

    ``raw`` (the lossily UTF-8-decoded file content every connector receives
    positionally, per the :class:`~remind_me_mcp.importer.Connector` protocol)
    is ignored here — a PDF is binary, and importer.py's own decode step would
    have already corrupted it. Instead this connector reads the original
    bytes from ``meta["raw_bytes"]``, which importer.py threads through
    specifically for binary connectors (see its FT-19 module docstring note).
    """
    raw_bytes = meta["raw_bytes"]
    max_length = meta["max_length"]

    pages = _extract_pages(raw_bytes)
    parsed: list[tuple[str, dict[str, Any]]] = []
    for page_num, page_text in enumerate(pages, start=1):
        page_text = page_text.strip()
        if not page_text:
            continue
        for chunk in _chunk_text(page_text, max_length):
            parsed.append((chunk, {"page": page_num}))
    return parsed, len(parsed)


register_connector("pdf", _pdf_connector)


__all__ = ["PDF_EXTRA_INSTALL_MSG", "_extract_pages", "_pdf_connector"]
