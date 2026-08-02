"""
remind_me_mcp.obsidian_import — Obsidian vault import connector (FT-31,
issue #193).

Registers a new "obsidian" import kind (:func:`~remind_me_mcp.importer.register_connector`)
that understands the Obsidian-flavored Markdown conventions a generic
``document`` import treats as opaque prose:

* **YAML frontmatter** (a leading ``---\\n ... \\n---`` block) — parsed into
  fields, with a ``tags`` field folded into the memory's tags exactly like an
  inline ``#tag`` (see below).
* **``[[Wikilinks]]``** (``[[Note]]``, ``[[Note|Alias]]``, and
  ``[[Note#Heading]]``) — each resolves to an entity for the linked note's
  title (created or matched via the existing FT-04 entity-upsert machinery)
  and the mentioning memory is linked to it, so the vault's own link graph
  becomes queryable via ``remind_me_entity``/``remind_me_entity_traverse``
  instead of being flattened into opaque text.
* **Inline ``#tag`` syntax** — distinct from a Markdown heading (which
  requires a space after the ``#``) and from a wikilink's heading anchor
  (``[[Note#Heading]]`` is not a tag) — extracted and folded into the
  memory's tags, deduplicated against frontmatter tags.

Chunking itself is **not reimplemented** — this connector strips the
frontmatter block off the front of the note and hands the remaining body to
:func:`remind_me_mcp.importer._parse_document`, the same per-section
(heading-context) Markdown chunker the plain ``document`` kind uses. Only the
per-note frontmatter/wikilink/tag extraction is new.

**Frontmatter parsing: hand-rolled, not pyyaml.** ``pyyaml`` is not a direct,
always-installed dependency of this project's base install — it only ever
appears in the venv transitively through *optional* extras (e.g.
``huggingface_hub`` under the ``semantic`` extra, ``rapidocr-onnxruntime``
under ``image``), so a bare ``pip install remind-me-mcp`` has no guarantee it
is present. Given this codebase's consistently minimal-dependency stance
(``rate_limit.py``/``telemetry.py``/``notifications.py``/``metrics.py`` all
hand-roll small formats rather than reach for a library, and the FT-25
metrics endpoint's docstring makes the same call explicitly for the same
reason), and that real Obsidian frontmatter is overwhelmingly a *flat*
key: value / key: [list] / key:\\n  - item shape — full YAML's nested maps,
anchors, multi-document streams, and block scalars are vanishingly rare in
practice — a small hand-rolled parser (:func:`parse_frontmatter`) covering
exactly that common shape is preferable to a new mandatory dependency for
this bounded a need. It never crashes on what it can't parse: any
frontmatter line it doesn't recognize (a nested mapping, a flow mapping, an
anchor/alias, …) makes the whole frontmatter block degrade to "no fields
extracted" rather than raising — the frontmatter delimiters are still
stripped from the body either way, so the note's prose always imports
cleanly. This is a deliberate, documented v1 scope limit, not an oversight —
if a real vault needs full YAML fidelity, layering ``pyyaml`` in behind this
same :func:`parse_frontmatter` call site is a contained, additive change.

**Wikilink parsing reuses this codebase's own implementation.** Rather than
writing a second bracket-matching regex, :func:`parse_obsidian_wikilinks`
reuses :data:`remind_me_mcp.wiki._WIKILINK_RE` (the FT-08 LLM Wiki layer's own
``[[Target]]``/``[[Target|alias]]`` pattern) and layers ``#Heading``/``^block``
anchor-stripping on top. **Gap noted, not silently papered over**: the wiki
layer's own :func:`remind_me_mcp.wiki.parse_wikilinks` does NOT strip a
``#Heading``/``^block`` suffix — ``[[Page#Section]]`` in a *wiki page* slugifies
the whole ``"Page#Section"`` string as one distinct target, silently forking
a second link target instead of resolving to the ``Page`` page's existing
slug. That is an existing, pre-FT-31 gap in ``wiki.py`` itself, deliberately
left unfixed here (changing ``wiki.py``'s own resolution semantics is a
separate, riskier change with its own blast radius across every existing
wiki page) and is out of scope for this issue; it does not affect Obsidian
import, since this module does its own anchor-stripping independently.
**Documented v1 limitation for Obsidian import itself**: the heading/block
anchor is stripped and discarded, not resolved to a specific section — a
link to ``[[Note#Heading]]`` resolves to an entity for ``Note`` as a whole,
the same entity a plain ``[[Note]]`` link would resolve to.

**Vault detection and per-file `.obsidian/` exclusion** are NOT handled in
this module: see :func:`remind_me_mcp.config.is_obsidian_vault` (the
``.obsidian/``-presence auto-detection helper, deliberately placed in
``config.py`` rather than here to avoid a circular import — see that
function's docstring) and ``watcher.py``'s existing dot-directory skip in
``FolderWatcher._candidate_files`` (which already excludes ``.obsidian/`` as
just another hidden directory, with no changes needed here — confirmed by
``tests/test_obsidian_import.py``).

**Kind vs. enhancement**: registered as its own ``obsidian`` kind (mirroring
``pdf``/``image``/``readwise``'s FT-19/FT-20 precedent) rather than folded
into ``document``, because it needs its own metadata contract (frontmatter
fields, wikilink-derived entity mentions, merged tags) that a plain document
chunk never carries — keeping it a separate kind means a `document` import
of a non-Obsidian Markdown file is completely unaffected, byte-for-byte.
Unlike ``readwise`` (never reachable via ``kind="auto"`` because a Readwise
export and a chat export are both bare, visually-indistinguishable JSON),
``kind="auto"`` here deliberately still does NOT route to ``obsidian`` for
content reasons — frontmatter delimiters and double-bracket text both have
legitimate uses outside Obsidian (Jekyll/Hugo front matter, footnote-style
``[[1]]`` references in plain prose), so content-sniffing alone risks
misrouting an unrelated Markdown file. Instead, ``obsidian`` is reached two
ways, both zero-risk: (1) explicitly, via ``kind="obsidian"``, and (2)
automatically but only when *directory* context — not just content — signals
Obsidian: :func:`remind_me_mcp.config.resolve_import_kind` upgrades a
``kind="auto"`` ``.md``/``.markdown`` file to ``obsidian`` when it sits inside
a detected vault (a ``.obsidian/`` directory at or above the watched/imported
root), which both ``watcher.py`` and ``importer.import_directory`` call.

This module is wired in by ``tools/admin.py`` importing it for its
registration side effect, the same shape ``pdf_import.py``/``image_import.py``/
``readwise_import.py`` use — never by ``importer.py`` itself.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from remind_me_mcp.importer import _parse_document, register_connector
from remind_me_mcp.wiki import _WIKILINK_RE

log = logging.getLogger("remind_me_mcp.obsidian_import")

OBSIDIAN_SOURCE = "obsidian_import"
"""``memories.source`` value for Obsidian imports (FT-31)."""

OBSIDIAN_CATEGORY = "obsidian"
"""Default ``memories.category`` for Obsidian imports when the caller passed
the generic chat default ('chat_import') or an empty category — kept
distinct from :data:`remind_me_mcp.importer.DOCUMENT_CATEGORY` (the same way
``pdf``/``image``/``readwise`` each get their own category) so a search or
listing can filter specifically on notes ingested from a vault."""

# ---------------------------------------------------------------------------
# Frontmatter (hand-rolled minimal YAML — see module docstring for rationale)
# ---------------------------------------------------------------------------

_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_\-]+):\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")


def _parse_scalar(raw: str) -> Any:
    """Coerce one YAML-ish scalar token to a Python value.

    Handles quoted strings, booleans, null, and plain integers/floats;
    anything else is returned as a plain (whitespace-trimmed) string. This is
    intentionally a small subset of real YAML scalar resolution — exactly
    the shapes common in Obsidian frontmatter (see the module docstring).
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    """Parse a flat key: value / key: [list] / key:\\n  - item block.

    Returns ``{}`` the moment a line doesn't match one of those three shapes
    (a nested mapping, a flow mapping ``{...}``, an anchor/alias/tag —
    ``&``/``*``/``!``, a multi-line block scalar ``|``/``>``, ...) — the
    degrade-gracefully contract :func:`parse_frontmatter` promises. Does not
    raise on anything; the caller always gets a dict back.
    """
    result: dict[str, Any] = {}
    key: str | None = None
    pending_list: list[Any] | None = None

    for raw_line in lines:
        if not raw_line.strip():
            continue
        if raw_line[:1] in (" ", "\t"):
            m = _LIST_ITEM_RE.match(raw_line)
            if m is None or key is None:
                return {}  # nested mapping or an item with no owning key
            item = _parse_scalar(m.group(1))
            if pending_list is None:
                pending_list = []
                result[key] = pending_list
            pending_list.append(item)
            continue

        m = _KEY_LINE_RE.match(raw_line.rstrip())
        if m is None:
            return {}  # not a recognized "key: value" line at all
        key, value = m.group(1), m.group(2).strip()
        pending_list = None
        if value == "":
            # Either a bare null scalar, or a block list follows on the next
            # indented lines (handled by the branch above as we continue).
            result[key] = None
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            result[key] = [_parse_scalar(x) for x in inner.split(",") if x.strip() != ""]
            continue
        if value.startswith("{") or any(ch in value for ch in ("&", "*", "!")):
            return {}  # flow mapping / anchor / alias / tag -- unsupported
        result[key] = _parse_scalar(value)
    return result


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading YAML frontmatter block off *text*.

    Returns ``(fields, body)``. ``fields`` is ``{}`` when there is no
    frontmatter block, when it has no closing delimiter (not actually
    frontmatter — e.g. a note that just happens to start with a horizontal
    rule), or when its content uses a YAML construct
    :func:`_parse_simple_yaml` doesn't support. In every case ``body`` is the
    note content with the delimited block removed if one was found — a note
    with unparseable frontmatter still imports as "skip frontmatter, ingest
    the body" rather than leaving raw ``---`` delimiters embedded in the
    stored content, and never raises.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text  # unterminated -- not a real frontmatter block

    fields = _parse_simple_yaml(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    return fields, body


def _frontmatter_tags(fields: dict[str, Any]) -> list[str]:
    """Extract a frontmatter ``tags`` field as a flat list of strings.

    Accepts Obsidian's two common shapes: a YAML list (``tags: [a, b]`` or a
    block list) and a single string, optionally comma-separated
    (``tags: project`` or ``tags: project, work``).
    """
    raw = fields.get("tags")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return [str(raw).strip()] if str(raw).strip() else []


# ---------------------------------------------------------------------------
# Wikilinks (reuses wiki.py's own [[Target]]/[[Target|alias]] pattern)
# ---------------------------------------------------------------------------


def parse_obsidian_wikilinks(text: str) -> list[tuple[str, str, str]]:
    """Extract ``[[Note]]``/``[[Note|Alias]]``/``[[Note#Heading]]`` links.

    Reuses :data:`remind_me_mcp.wiki._WIKILINK_RE` (see the module docstring
    for why a second bracket-matching regex isn't written from scratch), then
    strips a trailing ``#Heading`` or ``^block-id`` anchor before treating the
    remainder as the linked note's title — the documented v1 limitation that
    a heading/block reference resolves to the note as a whole, not a specific
    section (see the module docstring's "Gap noted" paragraph).

    Returns a list of ``(title, alias, raw_match_text)`` tuples in document
    order, one per occurrence (not deduplicated — callers that want the
    unique set of referenced titles, or need to test whether a specific
    rendered chunk contains a specific link, both want every occurrence, not
    a pre-deduplicated set).
    """
    out: list[tuple[str, str, str]] = []
    for m in _WIKILINK_RE.finditer(text):
        raw_target = m.group(1).strip()
        alias = (m.group(2) or "").strip()
        title = raw_target.split("#", 1)[0].split("^", 1)[0].strip()
        if not title:
            continue
        out.append((title, alias, m.group(0)))
    return out


# ---------------------------------------------------------------------------
# Inline #tag extraction
# ---------------------------------------------------------------------------

_FENCE_BLOCK_RE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_INLINE_TAG_RE = re.compile(r"(?<![\w#/])#([A-Za-z0-9_][\w\-/]*)")


def _mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    """Blank out each ``(start, end)`` span in *text* with spaces (newlines
    kept), preserving every other character's position so span indices found
    against the original text still line up against the masked copy."""
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def extract_inline_tags(text: str, exclude_spans: list[tuple[int, int]] | None = None) -> list[str]:
    """Extract Obsidian inline ``#tag``/``#nested/tag`` references from *text*.

    Distinct from a Markdown heading (``# Heading`` has a space after the
    ``#``, so it never matches this pattern) and from a wikilink's heading
    anchor (``[[Note#Heading]]`` — excluded via *exclude_spans*, the caller's
    already-found wikilink match spans). Fenced code blocks and inline code
    spans are masked out first so a ``#`` inside a code sample is never
    mistaken for a tag. A purely numeric tag (Obsidian doesn't allow one) is
    dropped.

    Args:
        text: The note body (frontmatter already stripped).
        exclude_spans: ``(start, end)`` character ranges to mask before
            scanning — callers pass their wikilink match spans here.

    Returns:
        Tags in first-appearance order, without the leading ``#``,
        deduplicated case-insensitively (first-seen casing kept).
    """
    ranges = [(m.start(), m.end()) for m in _FENCE_BLOCK_RE.finditer(text)]
    ranges += [(m.start(), m.end()) for m in _INLINE_CODE_RE.finditer(text)]
    ranges += list(exclude_spans or [])
    masked = _mask_ranges(text, ranges)

    seen: set[str] = set()
    tags: list[str] = []
    for tag in _INLINE_TAG_RE.findall(masked):
        if tag.replace("/", "").isdigit():
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def _dedupe_ci(items: list[str]) -> list[str]:
    """Case-insensitive dedup, first-seen casing kept, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen or not item:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# The obsidian connector
# ---------------------------------------------------------------------------


def _obsidian_connector(raw: str, meta: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Built-in ``obsidian`` connector (FT-31): frontmatter + wikilinks +
    inline tags, layered on top of :func:`~remind_me_mcp.importer._parse_document`'s
    existing per-section Markdown chunker.

    Each chunk's metadata may carry two reserved keys that
    :func:`~remind_me_mcp.importer._ingest_parsed` recognizes and pops before
    storage (see that function's docstring): ``extra_tags`` (merged into the
    stored memory's tags, deduplicated against the caller's own tags) and
    ``mention_entities`` (note titles to resolve into entities and link the
    memory to, via the existing FT-04 entity-upsert machinery). A chunk gets
    ``mention_entities`` only for the specific wikilinks whose raw
    ``[[...]]`` text actually landed in that chunk (chunking never rewrites
    text, so the literal markup survives verbatim in whichever chunk it fell
    into) — not every mention in the note, so a mention is attributed to the
    section that actually made it, not smeared across the whole file.
    """
    max_length = meta["max_length"]

    fields, body = parse_frontmatter(raw)
    frontmatter_tags = _frontmatter_tags(fields)
    frontmatter_extra = {k: v for k, v in fields.items() if k != "tags"}

    links = parse_obsidian_wikilinks(body)
    link_spans = [(m.start(), m.end()) for m in _WIKILINK_RE.finditer(body)]
    inline_tags = extract_inline_tags(body, exclude_spans=link_spans)
    combined_tags = _dedupe_ci([*frontmatter_tags, *inline_tags])

    pairs = _parse_document(body, ".md", max_length)

    parsed: list[tuple[str, dict[str, Any]]] = []
    for content, section in pairs:
        chunk_meta: dict[str, Any] = {}
        if section is not None:
            chunk_meta["section"] = section
        if frontmatter_extra:
            chunk_meta["obsidian_frontmatter"] = frontmatter_extra
        if combined_tags:
            chunk_meta["extra_tags"] = combined_tags
        chunk_mentions = _dedupe_ci([title for title, _alias, match_text in links if match_text in content])
        if chunk_mentions:
            chunk_meta["mention_entities"] = chunk_mentions
        parsed.append((content, chunk_meta))

    return parsed, len(parsed)


register_connector("obsidian", _obsidian_connector)


__all__ = [
    "OBSIDIAN_SOURCE",
    "OBSIDIAN_CATEGORY",
    "parse_frontmatter",
    "parse_obsidian_wikilinks",
    "extract_inline_tags",
    "_parse_scalar",
    "_parse_simple_yaml",
    "_frontmatter_tags",
    "_dedupe_ci",
    "_obsidian_connector",
]
