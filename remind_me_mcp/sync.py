"""
remind_me_mcp.sync — Background sync engine.

Handles hub sync and peer sync independently. Both use the same
outbox/pull protocol — the only difference is the endpoint URL.

Hub sync:   push local outbox → hub, pull hub changes → local
Peer sync:  push local outbox → peer, pull peer changes → local
            (peers discovered via Tailscale local API, plus STATIC_PEERS)

Conflict resolution: last-write-wins on updated_at for most columns
(applied in the upsert query), EXCEPT tags and metadata, which are
field-level merged regardless of which side wins (issue #60) — tags via
union (dedup, order-preserving, mirroring entity alias merging), metadata
via a shallow per-key merge where the LWW winner's value wins on key
collision but keys unique to either side are kept. This closes the common
case of two devices editing different fields of the same memory between
sync cycles: previously whichever write arrived second clobbered the
other's change entirely, not just the field that genuinely conflicted.
All timestamps are normalized to canonical UTC ISO-8601 on ingest so
string comparison is a correct ordering. Whole-row LWW is safe for
genuinely conflicting scalar fields like content because memories are
append-dominant — true concurrent edits to the same field are rare.

Outbox sends are tracked per remote in the ``sync_sends`` table, so every
configured hub/peer receives every outbox row. Pulls use a keyset cursor
``(updated_at, id)`` persisted in ``sync_log`` and drain the remote with
repeated pages until a short page arrives.

FT-04: the entity graph syncs too. Outbox records carry a ``record_type``
discriminator ('entity' / 'memory_entity'; absent = memory, so the wire
format old peers expect is unchanged). Entities resolve conflicts LWW on
updated_at except aliases, which union-merge (a commutative, idempotent
merge, so peers converge). memory_entities links are immutable
insert-or-ignore rows. Dedicated pull endpoints (/sync/pull_entities,
/sync/pull_links) keep separate keyset cursors; a 404 from a pre-FT-04
peer is tolerated.

Phase 3: entity_relations (typed entity-to-entity edges) sync the same way
as memory_entities links -- immutable insert-or-ignore, record_type
'entity_relation', its own /sync/pull_entity_relations endpoint and keyset
cursor. Unlike links, each row already carries its own deterministic id
(see db._entity_relation_id), so no synthetic wire id is needed.

Gap #11: deletion propagates via a deleted_at tombstone column, not a hard
DELETE (which produced no outbox row at all, so a memory deleted on one
device silently resurrected on the next pull elsewhere). A soft-delete is
just another UPDATE, so it rides the existing memories_outbox_au trigger and
LWW-on-updated_at conflict resolution unchanged -- no new operation type or
wire format needed. _upsert_records skips embedding a tombstoned incoming
record and cleans up its chunk vectors instead. _compact_tombstones hard-
deletes old-enough tombstones each cycle (config.TOMBSTONE_RETENTION_DAYS),
same time-based-only approach as _prune_outbox.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

import httpx

from remind_me_mcp import ann_index, config, notifications
from remind_me_mcp.config import (
    HUB_URL,
    NODE_ID,
    PEER_PORT,
    SYNC_ENABLED,
    SYNC_INTERVAL,
    SYNC_SECRET,
)
from remind_me_mcp.db import _delete_chunks, _embed_and_store_rows, _get_db, _now_iso
from remind_me_mcp.maintenance import _due
from remind_me_mcp.telemetry import maybe_span
from remind_me_mcp.version import __version__

log = logging.getLogger("remind_me_mcp.sync")

HEADERS = {"Authorization": f"Bearer {SYNC_SECRET}"}
BATCH_SIZE = 200
PULL_PAGE_SIZE = 500
# Safety valve so a misbehaving remote cannot trap one cycle forever.
MAX_PULL_PAGES = 100
# Caps how much of a large outbox backlog one sync cycle drains, so pull
# always gets a turn in the same cycle instead of waiting behind an
# unbounded push loop (issue #124). 10 batches * BATCH_SIZE = up to 2000
# records pushed per cycle; a bigger backlog just spreads across more cycles.
PUSH_BATCHES_PER_CYCLE = 10

_EPOCH = "1970-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Timestamp canonicalization (SY-08)
# ---------------------------------------------------------------------------


def _canon_ts(value: Any) -> str:
    """Normalize a timestamp to canonical UTC ISO-8601 ('...+00:00').

    Accepts the formats seen on the sync wire: Python isoformat with offset,
    'Z'-suffixed hub timestamps, and SQLite's 'YYYY-MM-DD HH:MM:SS'. Naive
    timestamps are assumed UTC. The canonical form is string-comparable, so
    last-write-wins ordering is correct across heterogeneous nodes.

    Args:
        value: The raw timestamp value from a sync record.

    Returns:
        The canonical ISO-8601 UTC string.

    Raises:
        ValueError: If the value is not a parseable timestamp string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"not a timestamp: {value!r}")
    dt = datetime.fromisoformat(value.strip())
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Outbox push (per-remote send tracking — SY-02)
# ---------------------------------------------------------------------------

def _decode_payload(raw: str) -> dict[str, Any]:
    """Decode an outbox payload, deserializing JSON-string list/object fields."""
    payload: dict[str, Any] = json.loads(raw)
    for fld in ("tags", "metadata", "aliases"):
        if isinstance(payload.get(fld), str):
            try:
                payload[fld] = json.loads(payload[fld])
            except (json.JSONDecodeError, TypeError):
                payload[fld] = {} if fld == "metadata" else []
    return payload


def _touch_sync_log(db: sqlite3.Connection, remote_id: str, column: str) -> None:
    """Upsert a single wall-clock column in ``sync_log`` for *remote_id* to now.

    Used for the liveness columns added in SY-18 (``last_attempt_at``,
    ``last_push_at``, ``last_pull_at``) — each is touched independently of
    the others and of the ``last_pull``/``last_pull_id`` resume cursor, so a
    plain ``ON CONFLICT DO UPDATE`` on just that column leaves everything
    else on the row untouched (and lets a fresh row take the table's default
    for every column but this one).

    Args:
        db: An open SQLite connection.
        remote_id: The remote this row tracks ("hub" or a peer's node id).
        column: One of the ``sync_log`` wall-clock columns — always a
            hardcoded literal from this module, never user input.
    """
    db.execute(
        f"""
        INSERT INTO sync_log (remote_id, {column})
        VALUES (?, ?)
        ON CONFLICT (remote_id) DO UPDATE SET {column} = excluded.{column}
        """,
        (remote_id, _now_iso()),
    )
    db.commit()


async def _push_outbox(
    client: httpx.AsyncClient, url: str, remote_id: str, *, max_batches: int | None = None
) -> int:
    """Push outbox rows not yet sent to *remote_id*; mark what it processed.

    Sends are tracked per remote in ``sync_sends`` so every hub/peer receives
    every row. When the remote reports ``processed_ids`` (our peer server
    does), only those rows are marked sent and the rest are retried next
    cycle; a count-only remote (legacy hub) marks the whole batch, with a
    warning when the accepted count falls short.

    ``last_attempt_at`` is touched before anything else so it reflects "we
    tried this cycle" even if the push raises partway through; ``last_push_at``
    is only touched after the loop completes without error, so the two
    together distinguish "never tried" / "tried and failed" / "tried and
    succeeded" (SY-15, SY-18) — a batch of zero rows still counts as success.

    ``max_batches`` bounds how much of a large backlog one call will drain
    (issue #124): without it, a big enough outbox lets this loop span the
    entire sync cycle — and a transient error partway through a long unbroken
    run aborts ``_sync_once`` before ``_pull_remote`` is ever reached, so pull
    can starve for as long as the backlog persists. Capping batches per call
    guarantees pull gets a turn every cycle regardless of backlog size; the
    outbox selection query naturally resumes from wherever ``sync_sends``
    left off, so nothing needs an explicit resume cursor across calls.

    Returns:
        The number of outbox rows marked sent to this remote.
    """
    db = _get_db()
    _touch_sync_log(db, remote_id, "last_attempt_at")
    total_marked = 0
    cursor = 0
    batches = 0

    while max_batches is None or batches < max_batches:
        rows = db.execute("""
            SELECT id, payload FROM sync_outbox
            WHERE id > ? AND sent_at = ''
              AND id NOT IN (SELECT outbox_id FROM sync_sends WHERE remote_id = ?)
            ORDER BY id ASC
            LIMIT ?
        """, (cursor, remote_id, BATCH_SIZE)).fetchall()

        if not rows:
            break
        batches += 1
        cursor = rows[-1]["id"]

        records = [_decode_payload(r["payload"]) for r in rows]

        resp = await client.post(
            f"{url}/sync/push",
            json={"node_id": NODE_ID, "records": records},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        accepted = data.get("accepted", 0)
        processed_ids = data.get("processed_ids")

        if processed_ids is None:
            # Count-only remote: we cannot tell which records failed. Mark the
            # whole batch — LWW-stale records would never be re-accepted anyway.
            to_mark = [r["id"] for r in rows]
            if accepted < len(records):
                log.warning(
                    "Remote %s accepted %d of %d records but did not report "
                    "which; marking the whole batch sent",
                    remote_id, accepted, len(records),
                )
        else:
            ok = {str(i) for i in processed_ids}
            to_mark = [
                row["id"]
                for row, rec in zip(rows, records, strict=True)
                if str(rec.get("id")) in ok
            ]

        now = _now_iso()
        db.executemany(
            "INSERT OR REPLACE INTO sync_sends (remote_id, outbox_id, sent_at) "
            "VALUES (?, ?, ?)",
            [(remote_id, outbox_id, now) for outbox_id in to_mark],
        )
        db.commit()
        total_marked += len(to_mark)
        log.debug(
            "Pushed %d records to %s (%d accepted, %d marked sent)",
            len(records), url, accepted, len(to_mark),
        )

        if len(rows) < BATCH_SIZE:
            break

    _touch_sync_log(db, remote_id, "last_push_at")
    return total_marked


# ---------------------------------------------------------------------------
# Pull (keyset cursor + drain loop — SY-04)
# ---------------------------------------------------------------------------

async def _pull_remote(client: httpx.AsyncClient, url: str, remote_id: str) -> int:
    """Pull new memories from a remote since our last keyset cursor.

    Pages with a ``(updated_at, id)`` keyset cursor (the ``since_id`` param is
    additive — older servers simply ignore it) and keeps pulling until a short
    page arrives, so one cycle drains the remote and page-boundary timestamp
    ties are never lost.

    Returns:
        The number of records upserted locally.
    """
    db = _get_db()

    row = db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = ?",
        (remote_id,),
    ).fetchone()
    since = row["last_pull"] if row else _EPOCH
    since_id = row["last_pull_id"] if row else ""

    total = 0
    for _ in range(MAX_PULL_PAGES):
        resp = await client.get(
            f"{url}/sync/pull",
            params={
                "since": since,
                "since_id": since_id,
                "exclude_node": NODE_ID,
                "limit": PULL_PAGE_SIZE,
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()

        records = resp.json().get("records", [])
        if not records:
            break

        result = _upsert_records(db, records)
        total += result.applied

        # Advance the keyset cursor to the greatest (updated_at, id) received.
        page_max: tuple[str, str] | None = None
        for rec in records:
            try:
                pair = (_canon_ts(rec["updated_at"]), str(rec["id"]))
            except (KeyError, TypeError, ValueError):
                continue
            if page_max is None or pair > page_max:
                page_max = pair
        if page_max is None or page_max <= (since, since_id):
            # No usable cursor progress — stop rather than spin on this page.
            break
        since, since_id = page_max

        db.execute("""
            INSERT INTO sync_log (remote_id, last_pull, last_pull_id)
            VALUES (?, ?, ?)
            ON CONFLICT (remote_id) DO UPDATE SET
                last_pull = excluded.last_pull,
                last_pull_id = excluded.last_pull_id
        """, (remote_id, since, since_id))
        db.commit()
        # Touched per-page, not just after a clean drain: a mid-drain error on
        # a later page (e.g. a timeout deep into a large backlog) would
        # otherwise leave this at its old value forever, even though the
        # pages before the error landed real records (SY-18 regression).
        _touch_sync_log(db, remote_id, "last_pull_at")

        log.debug(
            "Pulled %d records from %s, upserted %d (failed %d)",
            len(records), url, result.applied, result.failed,
        )

        if len(records) < PULL_PAGE_SIZE:
            break

    _touch_sync_log(db, remote_id, "last_pull_at")
    return total


async def _pull_graph_table(
    client: httpx.AsyncClient,
    url: str,
    remote_id: str,
    *,
    path: str,
    cursor_suffix: str,
    ts_field: str,
) -> int:
    """Pull entity-graph records (FT-04) with a keyset cursor; 404-tolerant.

    Same drain-loop/keyset shape as :func:`_pull_remote`, parameterized for
    the ``/sync/pull_entities`` and ``/sync/pull_links`` endpoints. The
    cursor is stored in ``sync_log`` under ``{remote_id}#{cursor_suffix}``
    so it never collides with the memory cursor. A 404 from the remote means
    a pre-FT-04 peer — skipped silently for backward compatibility.

    Returns:
        The number of records applied locally.
    """
    db = _get_db()
    cursor_id = f"{remote_id}#{cursor_suffix}"

    row = db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = ?",
        (cursor_id,),
    ).fetchone()
    since = row["last_pull"] if row else _EPOCH
    since_id = row["last_pull_id"] if row else ""

    total = 0
    for _ in range(MAX_PULL_PAGES):
        resp = await client.get(
            f"{url}{path}",
            params={
                "since": since,
                "since_id": since_id,
                "exclude_node": NODE_ID,
                "limit": PULL_PAGE_SIZE,
            },
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 404:
            log.debug(
                "Remote %s does not serve %s (pre-FT-04 peer) — skipping",
                remote_id, path,
            )
            _touch_sync_log(db, remote_id, "last_pull_at")
            return total
        resp.raise_for_status()

        records = resp.json().get("records", [])
        if not records:
            break

        result = _upsert_records(db, records)
        total += result.applied

        # Advance the keyset cursor to the greatest (timestamp, id) received.
        page_max: tuple[str, str] | None = None
        for rec in records:
            try:
                pair = (_canon_ts(rec[ts_field]), _record_wire_id(rec))
            except (KeyError, TypeError, ValueError):
                continue
            if page_max is None or pair > page_max:
                page_max = pair
        if page_max is None or page_max <= (since, since_id):
            break
        since, since_id = page_max

        db.execute("""
            INSERT INTO sync_log (remote_id, last_pull, last_pull_id)
            VALUES (?, ?, ?)
            ON CONFLICT (remote_id) DO UPDATE SET
                last_pull = excluded.last_pull,
                last_pull_id = excluded.last_pull_id
        """, (cursor_id, since, since_id))
        db.commit()
        # Touched per-page — see _pull_remote for why a clean-drain-only touch
        # hides real progress when a later page errors out mid-backlog.
        _touch_sync_log(db, remote_id, "last_pull_at")

        log.debug(
            "Pulled %d %s records from %s, applied %d (failed %d)",
            len(records), cursor_suffix, url, result.applied, result.failed,
        )

        if len(records) < PULL_PAGE_SIZE:
            break

    _touch_sync_log(db, remote_id, "last_pull_at")
    return total


async def _pull_entities(client: httpx.AsyncClient, url: str, remote_id: str) -> int:
    """Pull entity records from a remote (FT-04); no-op against old peers."""
    return await _pull_graph_table(
        client, url, remote_id,
        path="/sync/pull_entities", cursor_suffix="entities", ts_field="updated_at",
    )


async def _pull_links(client: httpx.AsyncClient, url: str, remote_id: str) -> int:
    """Pull memory_entities link records from a remote (FT-04)."""
    return await _pull_graph_table(
        client, url, remote_id,
        path="/sync/pull_links", cursor_suffix="links", ts_field="created_at",
    )


async def _pull_entity_relations(client: httpx.AsyncClient, url: str, remote_id: str) -> int:
    """Pull entity_relations records from a remote (Phase 3); no-op against
    peers/hubs that predate this feature."""
    return await _pull_graph_table(
        client, url, remote_id,
        path="/sync/pull_entity_relations", cursor_suffix="entity_relations",
        ts_field="created_at",
    )


# ---------------------------------------------------------------------------
# Upsert (full-column, per-record isolation — SY-03)
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = ("id", "content", "created_at", "updated_at")


@dataclass
class UpsertResult:
    """Outcome of applying a batch of remote records.

    Attributes:
        applied: Records actually written (new or LWW winners).
        failed: Malformed records that were skipped.
        processed_ids: Ids handled successfully — applied OR skipped as
            LWW-stale. These are safe for the sender to mark as sent.
    """

    applied: int = 0
    failed: int = 0
    processed_ids: list[str] = field(default_factory=list)


def _coerce_json_value(value: Any, default: Any) -> Any:
    """Parse a tags/metadata field of unknown shape into its Python value."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = default
    if value is None:
        value = default
    return value


def _coerce_json_field(value: Any, default: Any) -> str:
    """Return a JSON string for a tags/metadata field of unknown shape."""
    return json.dumps(_coerce_json_value(value, default))


def _parse_tags_list(value: Any) -> list[str]:
    """Parse a tags-shaped field into a list of strings, tolerating malformed input."""
    parsed = _coerce_json_value(value, [])
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str)]


def _parse_metadata_dict(value: Any) -> dict[str, Any]:
    """Parse a metadata-shaped field into a dict, tolerating malformed input."""
    parsed = _coerce_json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def merge_tags(local: Any, incoming: Any) -> list[str]:
    """Union-merge two tags fields (issue #60): dedup, order-preserving,
    local first — the same semantics :func:`_upsert_entity_one` already
    uses for entity aliases. Applied regardless of which side wins LWW, so
    a tag added on one device is never silently dropped by a conflicting
    edit to a different field on another device.
    """
    return list(dict.fromkeys([*_parse_tags_list(local), *_parse_tags_list(incoming)]))


def merge_metadata(local: Any, incoming: Any, *, incoming_wins: bool) -> dict[str, Any]:
    """Shallow field-level merge of two metadata dicts (issue #60).

    Both sides' keys are kept; on key collision, whichever side is the LWW
    winner (``incoming_wins``) takes precedence. This is a *shallow* merge —
    a key present on both sides with a dict/list value does not recurse,
    the winner's value replaces the loser's wholesale at that key.
    Deliberately simple: memories' metadata is typically flat per-import
    bookkeeping (``conversation_id``, ``filename``, ...), not nested
    structured data that would need a true recursive merge.
    """
    winner, loser = (incoming, local) if incoming_wins else (local, incoming)
    merged = _parse_metadata_dict(loser).copy()
    merged.update(_parse_metadata_dict(winner))
    return merged


def _upsert_one(db: sqlite3.Connection, rec: dict[str, Any]) -> int | None:
    """Validate and upsert a single remote record.

    Conflict resolution (issue #60): last-write-wins on ``updated_at`` for
    scalar columns, EXCEPT ``tags``/``metadata``, which are field-level
    merged via :func:`merge_tags`/:func:`merge_metadata` regardless of
    which side wins — closing the common case where two devices edit
    different fields of the same memory between sync cycles (one adds a
    tag, another edits content) and whichever write arrived second used to
    clobber the other's change entirely, not just the field that actually
    conflicted.

    When the incoming record loses LWW but the merge still changes local
    tags/metadata, a merge-only UPDATE is applied (not bumping
    ``updated_at`` — the contributing peer's own outbox row propagates its
    side of the merge, same as :func:`_upsert_entity_one`'s alias-fill
    branch) and this function still returns ``None``, exactly as if the
    record had simply lost LWW: content is unchanged, so re-embedding
    would be wasted work and would risk overwriting the local embedding
    with the (stale) incoming content's.

    Writes every schema column, defaulting fields absent from records sent by
    older nodes. Returns the memory rowid when the incoming record won LWW
    (content may have changed, so the caller should re-embed), or None
    otherwise. Raises on malformed input (caller isolates the failure).
    """
    if not isinstance(rec, dict):
        raise ValueError(f"record is not an object: {type(rec).__name__}")
    missing = [k for k in _REQUIRED_KEYS if not rec.get(k)]
    if missing:
        raise ValueError(f"record missing required keys: {missing}")

    created_at = _canon_ts(rec["created_at"])
    updated_at = _canon_ts(rec["updated_at"])
    try:
        accessed_at = _canon_ts(rec.get("accessed_at"))
    except ValueError:
        accessed_at = created_at

    mem_id = str(rec["id"])

    # Snapshot the outbox high-water mark so we can suppress exactly the echo
    # rows created by this upsert's triggers (SY-05).
    outbox_max = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sync_outbox"
    ).fetchone()[0]

    local = db.execute(
        "SELECT tags, metadata, updated_at FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()

    incoming_tags = rec.get("tags")
    incoming_metadata = rec.get("metadata")
    incoming_wins = local is None or updated_at > local["updated_at"]
    merged_tags = merge_tags(local["tags"] if local is not None else None, incoming_tags)
    merged_metadata = merge_metadata(
        local["metadata"] if local is not None else None,
        incoming_metadata,
        incoming_wins=incoming_wins,
    )

    def _suppress_echo() -> None:
        # Suppress only the echo rows this upsert's triggers just created —
        # pending rows from concurrent local edits keep their place in the
        # outbox (SY-05).
        db.execute("""
            UPDATE sync_outbox SET sent_at = ?
            WHERE memory_id = ? AND id > ? AND sent_at = ''
        """, (_now_iso(), mem_id, outbox_max))

    if not incoming_wins:
        # Incoming lost LWW: apply only the merged tags/metadata (if they
        # actually changed), leaving every other column — including
        # updated_at and content — untouched.
        local_tags = _parse_tags_list(local["tags"])
        local_metadata = _parse_metadata_dict(local["metadata"])
        if merged_tags != local_tags or merged_metadata != local_metadata:
            db.execute(
                "UPDATE memories SET tags = ?, metadata = ? WHERE id = ?",
                (json.dumps(merged_tags), json.dumps(merged_metadata), mem_id),
            )
            _suppress_echo()
        return None

    if local is None:
        cur = db.execute("""
            INSERT INTO memories
                (id, content, category, tags, source, metadata,
                 created_at, updated_at, capture_id, node_id, client,
                 accessed_at, access_count, decay_rate, vitality, base_weight,
                 status, memory_type, source_capture_id,
                 subject, predicate, object, superseded_by, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mem_id,
            str(rec["content"]),
            rec.get("category") or "general",
            json.dumps(merged_tags),
            rec.get("source") or "manual",
            json.dumps(merged_metadata),
            created_at,
            updated_at,
            rec.get("capture_id"),
            rec.get("node_id"),
            rec.get("client") or "unknown",
            accessed_at,
            int(rec.get("access_count") or 0),
            float(rec.get("decay_rate") or 0.1),
            float(rec.get("vitality") or 1.0),
            float(rec.get("base_weight") or 1.0),
            rec.get("status") or "active",
            rec.get("memory_type") or "unclassified",
            rec.get("source_capture_id"),
            rec.get("subject"),
            rec.get("predicate"),
            rec.get("object"),
            rec.get("superseded_by"),
            rec.get("deleted_at"),
        ))
        assert cur.lastrowid is not None  # guaranteed by the INSERT above
        rowid = cur.lastrowid
    else:
        result = db.execute("""
            UPDATE memories SET
                content = ?, category = ?, tags = ?, source = ?, metadata = ?,
                updated_at = ?, capture_id = ?, node_id = ?, client = ?,
                accessed_at = ?, access_count = ?, decay_rate = ?, vitality = ?,
                base_weight = ?, status = ?, memory_type = ?, source_capture_id = ?,
                subject = ?, predicate = ?, object = ?, superseded_by = ?, deleted_at = ?
            WHERE id = ?
            RETURNING rowid
        """, (
            str(rec["content"]),
            rec.get("category") or "general",
            json.dumps(merged_tags),
            rec.get("source") or "manual",
            json.dumps(merged_metadata),
            updated_at,
            rec.get("capture_id"),
            rec.get("node_id"),
            rec.get("client") or "unknown",
            accessed_at,
            int(rec.get("access_count") or 0),
            float(rec.get("decay_rate") or 0.1),
            float(rec.get("vitality") or 1.0),
            float(rec.get("base_weight") or 1.0),
            rec.get("status") or "active",
            rec.get("memory_type") or "unclassified",
            rec.get("source_capture_id"),
            rec.get("subject"),
            rec.get("predicate"),
            rec.get("object"),
            rec.get("superseded_by"),
            rec.get("deleted_at"),
            mem_id,
        ))
        rowid = result.fetchone()[0]

    _suppress_echo()
    return int(rowid)


def _upsert_entity_one(db: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """Validate and upsert a single remote entity record (FT-04).

    Conflict semantics: last-write-wins on ``updated_at`` for name/kind/
    node_id (same as memories) EXCEPT aliases, which are always union-merged
    (dedup, order-preserving: local first) regardless of which side wins —
    union is commutative and idempotent, so peers converge on the same alias
    set. A losing record's kind still fills in a locally-missing kind (a
    deterministic-id collision across peers should enrich, not erase).
    The merge does NOT bump updated_at: the contributing peer's own outbox
    row propagates the aliases, so bumping would only cause churn.

    Returns:
        True when local state changed, False when the record was a no-op.
        Raises on malformed input (caller isolates the failure).
    """
    missing = [k for k in ("id", "name", "created_at", "updated_at") if not rec.get(k)]
    if missing:
        raise ValueError(f"entity record missing required keys: {missing}")

    created_at = _canon_ts(rec["created_at"])
    updated_at = _canon_ts(rec["updated_at"])
    eid = str(rec["id"])
    incoming_aliases = rec.get("aliases")
    if isinstance(incoming_aliases, str):
        try:
            incoming_aliases = json.loads(incoming_aliases)
        except json.JSONDecodeError:
            incoming_aliases = []
    if not isinstance(incoming_aliases, list):
        incoming_aliases = []
    incoming_aliases = [a for a in incoming_aliases if isinstance(a, str) and a]

    # Snapshot the outbox high-water mark to suppress exactly the echo rows
    # this upsert's triggers create (SY-05).
    outbox_max = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sync_outbox"
    ).fetchone()[0]

    row = db.execute(
        "SELECT name, kind, aliases, updated_at FROM entities WHERE id = ?", (eid,)
    ).fetchone()

    applied = False
    if row is None:
        db.execute(
            """INSERT INTO entities (id, name, kind, aliases, created_at, updated_at, node_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, str(rec["name"]), rec.get("kind"),
             json.dumps(list(dict.fromkeys(incoming_aliases))),
             created_at, updated_at, rec.get("node_id")),
        )
        applied = True
    else:
        try:
            local_aliases = json.loads(row["aliases"]) if isinstance(row["aliases"], str) else []
        except json.JSONDecodeError:
            local_aliases = []
        if not isinstance(local_aliases, list):
            local_aliases = []
        merged = list(dict.fromkeys([*local_aliases, *incoming_aliases]))
        if updated_at > row["updated_at"]:
            db.execute(
                """UPDATE entities SET name = ?, kind = ?, aliases = ?,
                          updated_at = ?, node_id = ? WHERE id = ?""",
                (str(rec["name"]), rec.get("kind") or row["kind"],
                 json.dumps(merged), updated_at, rec.get("node_id"), eid),
            )
            applied = True
        else:
            fill_kind = row["kind"] or rec.get("kind")
            if merged != local_aliases or fill_kind != row["kind"]:
                db.execute(
                    "UPDATE entities SET aliases = ?, kind = ? WHERE id = ?",
                    (json.dumps(merged), fill_kind, eid),
                )
                applied = True

    if applied:
        db.execute("""
            UPDATE sync_outbox SET sent_at = ?
            WHERE memory_id = ? AND id > ? AND sent_at = ''
        """, (_now_iso(), eid, outbox_max))
    return applied


def _upsert_link_one(db: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """Validate and apply a single remote memory_entities link record (FT-04).

    Mention links are immutable — insert-or-ignore semantics, no conflict
    resolution needed. No FK enforcement: a link may arrive before its memory
    or entity does; the row simply waits for them.

    Returns:
        True when a new link row was inserted, False when it already existed.
        Raises on malformed input (caller isolates the failure).
    """
    missing = [k for k in ("memory_id", "entity_id", "created_at") if not rec.get(k)]
    if missing:
        raise ValueError(f"link record missing required keys: {missing}")
    created_at = _canon_ts(rec["created_at"])

    outbox_max = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sync_outbox"
    ).fetchone()[0]
    cur = db.execute(
        """INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, created_at)
           VALUES (?, ?, ?)""",
        (str(rec["memory_id"]), str(rec["entity_id"]), created_at),
    )
    applied = cur.rowcount > 0
    if applied:
        # The link trigger writes NEW.memory_id into sync_outbox.memory_id.
        db.execute("""
            UPDATE sync_outbox SET sent_at = ?
            WHERE memory_id = ? AND id > ? AND sent_at = ''
        """, (_now_iso(), str(rec["memory_id"]), outbox_max))
    return applied


def _upsert_entity_relation_one(db: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """Validate and apply a single remote entity_relations record (Phase 3).

    Relations are immutable -- insert-or-ignore semantics, no conflict
    resolution needed, same as :func:`_upsert_link_one`. Unlike links, the
    record already carries its own real ``id`` (deterministic, from the
    subject/relation/object triple), so no synthetic wire id is built here.

    Returns:
        True when a new relation row was inserted, False when it already
        existed. Raises on malformed input (caller isolates the failure).
    """
    missing = [
        k for k in ("id", "subject_entity_id", "relation", "object_entity_id", "created_at")
        if not rec.get(k)
    ]
    if missing:
        raise ValueError(f"entity_relation record missing required keys: {missing}")
    created_at = _canon_ts(rec["created_at"])

    outbox_max = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sync_outbox"
    ).fetchone()[0]
    cur = db.execute(
        """INSERT OR IGNORE INTO entity_relations
           (id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(rec["id"]), str(rec["subject_entity_id"]), str(rec["relation"]),
            str(rec["object_entity_id"]), created_at,
            _canon_ts(rec.get("updated_at") or rec["created_at"]),
            rec.get("node_id"),
        ),
    )
    applied = cur.rowcount > 0
    if applied:
        db.execute("""
            UPDATE sync_outbox SET sent_at = ?
            WHERE memory_id = ? AND id > ? AND sent_at = ''
        """, (_now_iso(), str(rec["id"]), outbox_max))
    return applied


def _record_wire_id(rec: dict[str, Any]) -> str:
    """The id the sender matches in processed_ids (links use memory_id|entity_id)."""
    rid = rec.get("id")
    if rid is None and rec.get("memory_id") and rec.get("entity_id"):
        rid = f"{rec['memory_id']}|{rec['entity_id']}"
    return str(rid)


def _upsert_records(db: sqlite3.Connection, records: list[dict[str, Any]]) -> UpsertResult:
    """Upsert remote records with last-write-wins conflict resolution.

    Records are dispatched on their ``record_type`` ('entity' /
    'memory_entity' / 'entity_relation' / absent = memory, FT-04/Phase 3). Each record is applied
    independently: a malformed or unknown-kind record is rolled back, logged,
    and counted as failed without poisoning the rest of the batch (defensive
    against newer peers sending kinds this node does not know). Applied
    memory records are embedded for semantic search after commit (SY-06) —
    best-effort, silently skipped when no embedder is available.

    A memory record whose ``deleted_at`` is set (a tombstone, gap #11) is
    never embedded — instead any chunk vectors it already has locally are
    removed, mirroring what ``remind_me_delete`` does for a local delete.
    The row itself stays (with ``deleted_at`` applied via the normal LWW
    upsert above), since it's the tombstone that needs to keep propagating
    to other nodes; only its now-pointless derived embeddings are cleaned up.

    Args:
        db: An open SQLite connection.
        records: Wire-format records from a hub or peer.

    Returns:
        An :class:`UpsertResult` with applied/failed counts and the ids that
        were processed successfully.
    """
    result = UpsertResult()
    embed_rows: list[tuple[int, str]] = []
    tombstoned_rowids: list[int] = []

    for rec in records:
        try:
            if not isinstance(rec, dict):
                raise ValueError(f"record is not an object: {type(rec).__name__}")
            record_type = rec.get("record_type") or "memory"
            rowid: int | None = None
            if record_type == "memory":
                rowid = _upsert_one(db, rec)
                applied = rowid is not None
            elif record_type == "entity":
                applied = _upsert_entity_one(db, rec)
            elif record_type == "memory_entity":
                applied = _upsert_link_one(db, rec)
            elif record_type == "entity_relation":
                applied = _upsert_entity_relation_one(db, rec)
            else:
                raise ValueError(f"unknown record_type: {record_type!r}")
            # Commit per record: a later malformed record's rollback must not
            # discard work already applied for earlier records.
            db.commit()
        except Exception as e:
            db.rollback()
            result.failed += 1
            log.warning("Skipping malformed sync record: %s", e)
            continue
        result.processed_ids.append(_record_wire_id(rec))
        if applied:
            result.applied += 1
        if rowid is not None:
            if rec.get("deleted_at"):
                tombstoned_rowids.append(rowid)
            else:
                embed_rows.append((rowid, str(rec.get("content") or "")))

    if embed_rows:
        try:
            _embed_and_store_rows(embed_rows)
        except Exception as e:
            log.debug("Embedding pulled records failed (non-fatal): %s", e)

    for rowid in tombstoned_rowids:
        try:
            removed = _delete_chunks(db, rowid)
            db.commit()
            for vec_rowid in removed:
                ann_index.remove_vector(db, vec_rowid)
        except Exception as e:
            log.debug("Cleaning up a remotely-tombstoned memory's chunks failed (non-fatal): %s", e)

    return result


# ---------------------------------------------------------------------------
# Outbox pruning (SY-07)
# ---------------------------------------------------------------------------


def _prune_outbox(db: sqlite3.Connection) -> int:
    """Prune echo-suppressed rows and rows past the retention window.

    Echo-suppressed rows (``sent_at != ''``) are never pushed, so they are
    removed immediately. Remaining rows are kept for OUTBOX_RETENTION_DAYS so
    intermittently-reachable remotes can still catch up, then dropped along
    with their per-remote send markers.

    Returns:
        The number of outbox rows removed.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(days=config.OUTBOX_RETENTION_DAYS)
    ).isoformat()
    removed = db.execute(
        "DELETE FROM sync_outbox WHERE sent_at != '' OR created_at < ?", (cutoff,)
    ).rowcount
    db.execute(
        "DELETE FROM sync_sends WHERE outbox_id NOT IN (SELECT id FROM sync_outbox)"
    )
    db.commit()
    if removed:
        log.debug("Pruned %d outbox rows", removed)
    return removed


# ---------------------------------------------------------------------------
# Tombstone compaction (gap #11)
# ---------------------------------------------------------------------------


def _compact_tombstones(db: sqlite3.Connection) -> int:
    """Hard-delete memories tombstoned longer than TOMBSTONE_RETENTION_DAYS ago.

    Purely time-based, like :func:`_prune_outbox` — no per-peer acknowledgment
    tracking (this is a single-owner, LWW sync model, not a general-purpose
    replicated database; see config.TOMBSTONE_RETENTION_DAYS for the tradeoff
    this accepts). Chunk vectors, the ANN index entry, and entity mention
    links are cleaned up the same way :func:`remind_me_mcp.tools.crud.memory_delete`
    does for a fresh local delete — a compacted tombstone is a real delete,
    just a deferred one.

    Only ever called from the sync loop (config.SYNC_ENABLED), so a node that
    never syncs never compacts — see memory_delete's hard-delete fast path
    for that case instead.

    Returns:
        The number of tombstoned memories hard-deleted.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(days=config.TOMBSTONE_RETENTION_DAYS)
    ).isoformat()
    rows = db.execute(
        "SELECT id, rowid FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0

    removed_vec_rowids: list[int] = []
    for row in rows:
        removed_vec_rowids.extend(_delete_chunks(db, row["rowid"]))
        db.execute("DELETE FROM memory_entities WHERE memory_id = ?", (row["id"],))
        db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
    db.commit()
    # ANN mutations only after the commit succeeds — see db._delete_chunks.
    for vec_rowid in removed_vec_rowids:
        ann_index.remove_vector(db, vec_rowid)

    log.debug("Compacted %d tombstoned memories", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Status reporting (SY-12)
# ---------------------------------------------------------------------------

# Per-remote last error, keyed by remote_id ("hub" or a peer's node_id). Only
# the most recent failure per remote is kept — this is a "what is wrong right
# now" surface, not a log. Cleared on the next successful cycle for that
# remote so a recovered remote stops reporting a stale error.
_last_errors: dict[str, dict[str, str]] = {}

# Per-remote build identity, keyed the same way as _last_errors. Populated
# from the /health response _probe_peer already fetches every cycle (issue
# #208) -- no extra request. Deliberately in-memory alongside the errors
# above rather than persisted: a version observed before the last restart is
# a guess about a machine this process hasn't spoken to since, and reporting
# a guess as a fact is the failure mode this whole feature exists to avoid.
# Each entry carries the observation time so a peer that has since gone
# unreachable is read as "last seen running X", not "is running X".
_peer_versions: dict[str, dict[str, str]] = {}

# Minimum gap between drain-rate baseline updates. Without this, two status
# calls seconds apart would overwrite the baseline with a near-zero elapsed
# time and report a meaningless rate. Below this threshold the older baseline
# is kept so the reported rate stays informative.
_STATUS_PROBE_MIN_SECONDS = 30


def _record_error(remote_id: str, exc: BaseException) -> None:
    """Remember the most recent sync failure for one remote (SY-12)."""
    _last_errors[remote_id] = {
        "error": f"{type(exc).__name__}: {exc}",
        "at": _now_iso(),
    }


def _clear_error(remote_id: str) -> None:
    """Forget a remote's last error after a successful cycle (SY-12)."""
    _last_errors.pop(remote_id, None)


def _flag(db: sqlite3.Connection, key: str) -> str | None:
    """Read one ``sync_flags`` value, or None when unset."""
    row = db.execute("SELECT value FROM sync_flags WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_flag(db: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one ``sync_flags`` value."""
    db.execute(
        "INSERT INTO sync_flags (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _drain_rate(db: sqlite3.Connection, pending: int) -> dict[str, Any]:
    """Compare the pending count against the last probe to derive direction.

    A single pending count is ambiguous — 10k queued rows look identical
    whether they are draining normally or the push is wedged. This persists
    the previous observation in ``sync_flags`` so one call can answer
    "draining or stuck?" instead of requiring the caller to sample twice and
    diff by hand.

    The baseline is only advanced once ``_STATUS_PROBE_MIN_SECONDS`` have
    elapsed, so rapid successive calls still report against a meaningful
    interval rather than resetting to noise.

    Args:
        db: An open SQLite connection.
        pending: The current unsent outbox row count.

    Returns:
        A dict with ``verdict`` and, when a usable baseline exists,
        ``per_minute``, ``sampled_over_seconds``, and ``eta_minutes``.
    """
    now = datetime.now(UTC)
    prev_count_raw = _flag(db, "status_probe_pending")
    prev_at_raw = _flag(db, "status_probe_at")

    def _save() -> None:
        _set_flag(db, "status_probe_pending", str(pending))
        _set_flag(db, "status_probe_at", now.isoformat())
        db.commit()

    if pending == 0:
        _save()
        return {"verdict": "idle"}

    if prev_count_raw is None or prev_at_raw is None:
        _save()
        return {
            "verdict": "unknown",
            "hint": (
                "no baseline yet — call again in "
                f"{_STATUS_PROBE_MIN_SECONDS}s to get a drain rate"
            ),
        }

    try:
        prev_count = int(prev_count_raw)
        elapsed = (now - datetime.fromisoformat(_canon_ts(prev_at_raw))).total_seconds()
    except (ValueError, TypeError):
        # Corrupt baseline — reset rather than propagate a parse error out of
        # a read-only status call.
        _save()
        return {"verdict": "unknown", "hint": "baseline reset (unparseable)"}

    if elapsed <= 0:
        return {"verdict": "unknown", "hint": "baseline is not older than now"}

    drained = prev_count - pending
    per_minute = round(drained / (elapsed / 60), 1)

    if elapsed >= _STATUS_PROBE_MIN_SECONDS:
        _save()

    result: dict[str, Any] = {
        "per_minute": per_minute,
        "sampled_over_seconds": round(elapsed, 1),
    }
    if drained > 0:
        result["verdict"] = "draining"
        result["eta_minutes"] = round(pending / per_minute, 1) if per_minute else None
    elif drained == 0:
        result["verdict"] = "stalled"
        result["hint"] = (
            "pending count unchanged — check reachability of the hub/peers, "
            "that SYNC_SECRET matches, and the server log for push errors"
        )
    else:
        result["verdict"] = "growing"
        result["hint"] = "local writes are outpacing the push rate"
    return result


def _pending_to_remote(db: sqlite3.Connection, remote_id: str) -> tuple[int, str | None]:
    """Count outbox rows genuinely still owed to *remote_id*, and their oldest.

    Mirrors ``_push_outbox``'s own selection exactly (SY-16): a row stops
    being pending only once it's recorded in ``sync_sends`` for this remote,
    or echo-suppressed (``sent_at != ''``, meaning it originated from a pull
    and was never meant to go back out). The previous approach counted
    ``sent_at = ''`` alone, which conflated "pushed" with "not an echo" — a
    node that mostly writes its own memories (so nothing is ever echoed back
    to it to suppress) read as permanently stalled regardless of whether
    pushes were succeeding.

    Returns:
        A ``(pending_count, oldest_pending_created_at)`` pair; the second
        element is ``None`` when nothing is pending.
    """
    where = """
        o.sent_at = ''
        AND NOT EXISTS (
            SELECT 1 FROM sync_sends s
            WHERE s.remote_id = ? AND s.outbox_id = o.id
        )
    """
    (pending,) = db.execute(
        f"SELECT COUNT(*) FROM sync_outbox o WHERE {where}", (remote_id,)
    ).fetchone()
    (oldest,) = db.execute(
        f"SELECT MIN(o.created_at) FROM sync_outbox o WHERE {where}", (remote_id,)
    ).fetchone()
    return pending, oldest


def get_sync_status() -> dict[str, Any]:
    """Report sync state for the MCP status tools (SY-12).

    Sync is the most failure-prone subsystem here and was the only one with no
    status surface — diagnosing it previously meant shell access and hand-written
    SQL against ``sync_outbox``. This is the read-only equivalent of
    :func:`remind_me_mcp.watcher.get_watch_status`.

    Returns:
        A JSON-serializable dict: enable/gate flags, per-remote push/pull
        watermarks, outbox depth with a drain-rate verdict, tombstone counts,
        and the most recent error per remote. When sync is disabled, includes a
        configuration hint instead of empty counters.
    """
    if not SYNC_ENABLED:
        missing = [
            name
            for name, value in (
                ("REMIND_ME_NODE_ID", NODE_ID),
                ("REMIND_ME_HUB_URL", HUB_URL),
                ("REMIND_ME_SYNC_SECRET", SYNC_SECRET),
            )
            if not value
        ]
        return {
            "enabled": False,
            "node_id": NODE_ID or None,
            "version": __version__,
            "hub_url": HUB_URL or None,
            "hint": (
                "set " + ", ".join(missing) + " to enable sync; the outbox "
                "triggers stay gated off until then so nothing accumulates"
            ),
        }

    db = _get_db()

    pending, oldest_pending = _pending_to_remote(db, "hub")
    (total_outbox,) = db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()
    sent = total_outbox - pending

    (tombstones,) = db.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL"
    ).fetchone()
    tombstone_cutoff = (
        datetime.now(UTC) - timedelta(days=config.TOMBSTONE_RETENTION_DAYS)
    ).isoformat()
    (compactable,) = db.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
        (tombstone_cutoff,),
    ).fetchone()

    remotes = [
        {
            "remote_id": row["remote_id"],
            # Wall-clock contact times (SY-18), not the last_pull content
            # cursor -- a quiet-but-healthy remote still advances these every
            # cycle even when it has nothing new to send.
            "last_attempt_at": row["last_attempt_at"],
            "last_push_at": row["last_push_at"],
            "last_pull_at": row["last_pull_at"],
            # A never-contacted remote sits at the epoch default rather than
            # NULL, which reads as a stale timestamp unless called out.
            "ever_contacted": row["last_attempt_at"] != _EPOCH,
            "pending": _pending_to_remote(db, row["remote_id"])[0],
            "last_error": _last_errors.get(row["remote_id"]),
            # Build last observed on that peer, with the time it was seen
            # (issue #208). Absent for the hub -- its version comes from
            # reconcile's /stats call, not from peer discovery -- and for
            # peers that predate version reporting.
            "version": _peer_versions.get(row["remote_id"]),
        }
        # Graph-table cursors are stored as synthetic "{remote_id}#{suffix}"
        # rows in this same table (_pull_graph_table) so they don't collide
        # with the memory cursor -- exclude them here, they're not remotes.
        for row in db.execute(
            "SELECT remote_id, last_attempt_at, last_push_at, last_pull_at "
            "FROM sync_log WHERE remote_id NOT LIKE '%#%' ORDER BY remote_id"
        ).fetchall()
    ]
    # Surface errors for remotes that have never completed a cycle, so a hub
    # that has failed on every attempt is not silently absent from the report.
    known = {r["remote_id"] for r in remotes}
    for remote_id, err in _last_errors.items():
        if remote_id not in known:
            remotes.append(
                {
                    "remote_id": remote_id,
                    "last_attempt_at": None,
                    "last_push_at": None,
                    "last_pull_at": None,
                    "ever_contacted": False,
                    "pending": _pending_to_remote(db, remote_id)[0],
                    "last_error": err,
                    "version": _peer_versions.get(remote_id),
                }
            )

    return {
        "enabled": True,
        "node_id": NODE_ID,
        # This node's build. Sync bugs are usually version-skew bugs across a
        # fleet, and this is the report an operator already runs per node.
        "version": __version__,
        "hub_url": HUB_URL,
        "sync_interval_seconds": SYNC_INTERVAL,
        # The triggers are gated on this flag (SY-07); if it ever disagrees
        # with `enabled`, local writes are silently not being queued.
        "outbox_trigger_gate": _flag(db, "sync_enabled"),
        "outbox": {
            "pending": pending,
            "sent": sent,
            "oldest_pending_at": oldest_pending,
            "retention_days": config.OUTBOX_RETENTION_DAYS,
            "drain": _drain_rate(db, pending),
        },
        "tombstones": {
            "total": tombstones,
            "compactable_now": compactable,
            "retention_days": config.TOMBSTONE_RETENTION_DAYS,
        },
        "remotes": remotes,
    }


# ---------------------------------------------------------------------------
# Hub reconciliation (SY-14)
# ---------------------------------------------------------------------------

# How stale the last successful pull may be before hub-ahead drift stops being
# ordinary lag and starts meaning "pull isn't running". Three cycles gives a
# missed cycle room to recover; the floor keeps very short intervals from
# producing false faults.
_PULL_LAG_GRACE_SECONDS = 300


def _local_counts(db: sqlite3.Connection) -> dict[str, Any]:
    """Count local records the same way the hub's /stats does.

    Deliberately does NOT filter ``deleted_at IS NULL``. The hub counts every
    row and reports tombstones separately, so filtering here would make a
    healthy node look permanently behind by its own tombstone count — the
    comparison has to be apples-to-apples, not user-visible-to-total.
    """
    (totals,) = db.execute(
        """
        SELECT COUNT(*)                                              AS total,
               SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS tombstones
          FROM memories
        """
    ).fetchall()
    by_category = {
        (row["category"] or "(none)"): row["count"]
        for row in db.execute(
            "SELECT COALESCE(NULLIF(category, ''), '(none)') AS category, "
            "COUNT(*) AS count FROM memories GROUP BY 1"
        ).fetchall()
    }
    counts = {}
    for table in ("entities", "memory_entities", "entity_relations"):
        (row,) = db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
        counts[table] = row["count"]
    return {
        "total": totals["total"] or 0,
        "tombstones": totals["tombstones"] or 0,
        "by_category": by_category,
        **counts,
    }


def _cursor_progress(
    db: sqlite3.Connection, remote_id: str, watermark: tuple[str, str] | None
) -> bool:
    """Has *remote_id*'s pull cursor actually advanced since the last check?

    ``last_pull_at`` alone can't tell a healthy pull apart from a wedged one:
    a keyset cursor only advances when a page has records to advance it past,
    so a cursor stuck behind a gap it can never reach forward from (e.g. a
    historical hole predating the watermark — issue #121) keeps touching
    ``last_pull_at`` every cycle, "attempted and succeeded," while making zero
    content progress indefinitely. This persists the previous
    ``(last_pull, last_pull_id)`` pair in ``sync_flags`` (mirroring
    ``_drain_rate``'s baseline-in-sync_flags pattern) so a caller can ask "did
    the cursor move?" instead of only "did we try recently?".

    Args:
        db: An open SQLite connection.
        remote_id: The cursor to check — 'hub', 'hub#entities', etc.
        watermark: The remote's current ``(last_pull, last_pull_id)``, or
            None if it has never pulled.

    Returns:
        True if the cursor is unchanged from the last time this was called
        (i.e. stalled); False if it moved, or if there's no prior baseline to
        compare against yet.
    """
    key = f"reconcile_watermark_{remote_id}"
    prev_raw = _flag(db, key)
    current_raw = f"{watermark[0]}|{watermark[1]}" if watermark else ""
    _set_flag(db, key, current_raw)
    db.commit()
    if prev_raw is None:
        return False  # no baseline yet
    return prev_raw == current_raw and bool(current_raw)


def _verdict(
    categories: list[dict[str, Any]],
    last_pull_age: float | None,
    cursor_stalled: bool = False,
) -> tuple[str, list[str]]:
    """Classify drift. The judgment is the point — raw deltas are just numbers.

    ``node-ahead`` (local > hub anywhere) is unambiguous: the node holds
    records the hub does not, so pushes aren't landing. It's checked first
    because it's the only direction that means data is at risk.

    Hub-ahead drift is the ordinary state of a healthy node between pulls, so
    it is judged by evidence rather than by guessing which categories "should"
    be static: if the last successful pull is recent it's lag, and if it's
    stale the pull is broken.

    Args:
        categories: Per-category rows with non-zero ``delta`` (hub - local).
        last_pull_age: Seconds since ``last_pull_at`` — the wall clock of the
            last successful pull *attempt* against the hub (SY-18), not the
            content cursor: a quiet-but-healthy hub still updates this every
            cycle even when it has nothing new to send, so a stale value
            here means the pull itself has stopped running. None if the hub
            has never been pulled from.
        cursor_stalled: True if the pull *cursor* (the content watermark, not
            just the attempt timestamp) hasn't moved since the last time this
            was checked, despite drift existing (issue #121). A cursor can be
            attempted successfully every cycle and still never advance if the
            gap it needs to close sits behind its own watermark — recent
            ``last_pull_age`` alone can't tell that apart from ordinary lag,
            so this is checked independently and wins even when the attempt
            looks recent.

    Returns:
        ``(verdict, hints)``.
    """
    hints: list[str] = []
    ahead = [c["category"] for c in categories if c["delta"] < 0]
    if ahead:
        hints.append(
            "this node holds records the hub does not ("
            + ", ".join(sorted(ahead)[:5])
            + ") — pushes are not landing; check remind_me_sync_status for a "
            "stalled outbox or a push error"
        )
        return "node-ahead", hints

    if not categories:
        return "in-sync", hints

    grace = max(3 * SYNC_INTERVAL, _PULL_LAG_GRACE_SECONDS)
    if last_pull_age is None:
        hints.append(
            "the hub has never been pulled from successfully, so this drift is "
            "not lag — check connectivity and SYNC_SECRET"
        )
        return "fault", hints
    if last_pull_age > grace:
        hints.append(
            f"last successful pull was {round(last_pull_age)}s ago (> {grace}s "
            "grace), so the hub being ahead is not ordinary lag — the pull is "
            "not running"
        )
        return "fault", hints
    if cursor_stalled:
        hints.append(
            "the pull cursor has not advanced since the last check, even "
            "though attempts keep succeeding — the gap is likely behind the "
            "cursor's own watermark (e.g. a historical hole from before this "
            "node's current identity) and will not close on its own; a full "
            "re-pull (reset the cursor) is needed"
        )
        return "fault", hints

    hints.append(
        "the hub is ahead only in categories other nodes are writing to, and "
        f"the last pull was {round(last_pull_age)}s ago — ordinary pull lag"
    )
    return "pull-lag", hints


def _notify_sync_fault(hub_url: str, hints: list[str]) -> None:
    """Notify on a ``fault`` reconcile verdict, throttled (issue #180).

    ``remind_me_sync_reconcile`` is a read-only tool that can be called
    repeatedly (e.g. by an external monitor polling it on a schedule) while
    the same fault persists across many calls; notifying on every single one
    would be exactly the alert-fatigue failure BACKLOG Wave 4 documents (a
    prior production incident's status surfaces were technically correct but
    trained the reader to ignore them). Reuses
    :func:`remind_me_mcp.maintenance._due`'s named-throttle-slot helper
    rather than duplicating it -- cross-module reuse of a leading-underscore
    helper already has precedent in this codebase (e.g. ``sync._upsert_records``
    imported directly into ``peer_server.py``, ``config._env_int`` into this
    module), so this follows the same pattern rather than growing a second
    throttle implementation.

    Never raises: a diagnostic call must not fail because notifying about it did.
    """
    if not _due("sync_fault", config.NOTIFY_SYNC_FAULT_INTERVAL):
        return
    hint = hints[0] if hints else "sync verdict is fault"
    try:
        notifications.notify(
            "remind_me: sync fault",
            f"remind_me_sync_reconcile reported a fault against {hub_url}: {hint}",
        )
    except Exception as e:  # noqa: BLE001 — a reconcile call must never raise over this
        log.warning("Sync fault notification failed: %s", e)


async def reconcile_with_hub(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Diff this node's record counts against the hub's, with a verdict (SY-14).

    Read-only on both sides. Answers "does my data match the hub?" — previously
    a manual exercise of running psql on the hub host, pulling local counts
    separately, and diffing two tables by eye. The subtle part is that the
    benign case (hub ahead, pull lag) and the real fault (node ahead, push not
    landing) differ only by a sign, which is easy to skim past; encoding it
    here means it's decided once, not re-derived per incident.

    A ``fault`` verdict also triggers a throttled
    :func:`remind_me_mcp.notifications.notify` call (issue #180, see
    :func:`_notify_sync_fault`) — a no-op unless a notification channel is
    configured, and rate-limited so a persisting fault doesn't re-alert on
    every call.

    Args:
        client: Optional httpx client, for tests. A new one is created when
            omitted.

    Returns:
        A JSON-serializable dict. ``status`` is ``ok`` when the hub answered;
        otherwise ``disabled`` / ``unreachable`` / ``unsupported`` /
        ``unauthorized`` / ``error`` with a hint, and no verdict.
    """
    if not SYNC_ENABLED:
        return {
            "status": "disabled",
            "hint": "sync is not configured — see remind_me_sync_status",
        }

    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        resp = await client.get(f"{HUB_URL}/stats", headers=HEADERS, timeout=15)
        if resp.status_code == 404:
            # A hub predating SY-13 has no /stats. Say so plainly rather than
            # reporting it as unreachable — the fix is a hub upgrade, and
            # during a rolling upgrade the node lands here first.
            return {
                "status": "unsupported",
                "hub_url": HUB_URL,
                "hint": (
                    "the hub has no GET /stats endpoint (added in SY-13) — "
                    "upgrade the hub to reconcile"
                ),
            }
        if resp.status_code in (401, 403):
            return {
                "status": "unauthorized",
                "hub_url": HUB_URL,
                "hint": "the hub rejected our bearer token — SYNC_SECRET mismatch",
            }
        resp.raise_for_status()
        hub = resp.json()
    except httpx.ConnectError as e:
        return {"status": "unreachable", "hub_url": HUB_URL, "hint": str(e)}
    except Exception as e:  # noqa: BLE001 — a status call must never raise
        return {
            "status": "error",
            "hub_url": HUB_URL,
            "hint": f"{type(e).__name__}: {e}",
        }
    finally:
        if owns_client:
            await client.aclose()

    db = _get_db()
    local = _local_counts(db)
    hub_mem = hub.get("memories", {})
    hub_categories: dict[str, int] = hub_mem.get("by_category", {}) or {}

    # Union of both sides, so a category present on only one is still compared
    # (a bulk import missing entirely from the hub would otherwise vanish).
    categories = []
    for name in sorted(set(hub_categories) | set(local["by_category"])):
        hub_n = hub_categories.get(name, 0)
        local_n = local["by_category"].get(name, 0)
        if hub_n != local_n:
            categories.append(
                {
                    "category": name,
                    "hub": hub_n,
                    "local": local_n,
                    "delta": hub_n - local_n,
                }
            )

    row = db.execute(
        "SELECT last_pull_at, last_pull, last_pull_id FROM sync_log WHERE remote_id = 'hub'"
    ).fetchone()
    last_pull_at = row["last_pull_at"] if row else None
    last_pull_age: float | None = None
    if last_pull_at and last_pull_at != _EPOCH:
        try:
            last_pull_age = (
                datetime.now(UTC) - datetime.fromisoformat(_canon_ts(last_pull_at))
            ).total_seconds()
        except (ValueError, TypeError):
            last_pull_age = None

    watermark = (row["last_pull"], row["last_pull_id"]) if row else None
    cursor_stalled = bool(categories) and _cursor_progress(db, "hub", watermark)

    verdict, hints = _verdict(categories, last_pull_age, cursor_stalled)
    if verdict == "fault":
        _notify_sync_fault(HUB_URL, hints)
    pending, _ = _pending_to_remote(db, "hub")

    def _cmp(hub_value: Any, local_value: int) -> dict[str, Any]:
        hub_n = hub_value if isinstance(hub_value, int) else 0
        return {"hub": hub_n, "local": local_value, "delta": hub_n - local_value}

    def _graph_verdict(
        cursor_id: str, label: str, hub_value: Any, local_value: int
    ) -> dict[str, Any] | None:
        """Same fault-vs-lag diagnosis as the memory cursor, for one entity-
        graph cursor (issue #123). Returns None when the two sides agree —
        nothing to diagnose.

        The three graph cursors (``hub#entities``/``hub#links``/
        ``hub#entity_relations``) each keep their own content watermark
        (``last_pull``/``last_pull_id``) but share the memory cursor's
        ``last_pull_at`` wall clock — every pull attempt against the hub
        touches all four in the same cycle (see ``_pull_graph_table``), so
        "was this attempted recently" is one shared signal even though "did
        this specific cursor progress" is not.
        """
        hub_n = hub_value if isinstance(hub_value, int) else 0
        delta = hub_n - local_value
        if delta == 0:
            return None
        fake_categories = [{"category": label, "hub": hub_n, "local": local_value, "delta": delta}]
        cur = db.execute(
            "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = ?",
            (cursor_id,),
        ).fetchone()
        cur_watermark = (cur["last_pull"], cur["last_pull_id"]) if cur else None
        stalled = _cursor_progress(db, cursor_id, cur_watermark)
        v, h = _verdict(fake_categories, last_pull_age, stalled)
        return {"verdict": v, "hints": h}

    graph_verdicts = {
        k: v
        for k, v in {
            "entities": _graph_verdict(
                "hub#entities", "entities", hub.get("entities"), local["entities"]
            ),
            "memory_entities": _graph_verdict(
                "hub#links", "memory_entities",
                hub.get("memory_entities"), local["memory_entities"],
            ),
            "entity_relations": _graph_verdict(
                "hub#entity_relations", "entity_relations",
                hub.get("entity_relations"), local["entity_relations"],
            ),
        }.items()
        if v is not None
    }

    return {
        "status": "ok",
        "verdict": verdict,
        "hints": hints,
        "node_id": NODE_ID,
        "version": __version__,
        # Absent on a hub older than the release that added it — reported as
        # None rather than omitted, so "old hub" is visible in the output
        # instead of looking like a field this node forgot to fill in.
        "hub_version": hub.get("version"),
        "hub_url": HUB_URL,
        "last_pull_at": last_pull_at,
        "last_pull_age_seconds": (
            round(last_pull_age) if last_pull_age is not None else None
        ),
        "outbox_pending": pending,
        "memories": _cmp(hub_mem.get("total"), local["total"]),
        "tombstones": _cmp(hub_mem.get("tombstones"), local["tombstones"]),
        # Only drifting categories are listed; the rest would be noise at ~30
        # categories, so agreement is reported as a count instead.
        "categories_with_drift": categories,
        "categories_in_sync": len(
            set(hub_categories) | set(local["by_category"])
        ) - len(categories),
        "entities": _cmp(hub.get("entities"), local["entities"]),
        "memory_entities": _cmp(hub.get("memory_entities"), local["memory_entities"]),
        "entity_relations": _cmp(
            hub.get("entity_relations"), local["entity_relations"]
        ),
        # Per-cursor fault-vs-lag diagnosis for the entity graph (issue #123)
        # — only present for a table whose count actually drifted, mirroring
        # categories_with_drift's noise-reduction.
        "graph_verdicts": graph_verdicts,
        # Hub-side only — origin_node never crosses the wire, so there is no
        # local equivalent to diff against. Informational: it answers "which
        # node hasn't pushed".
        "hub_by_origin_node": hub_mem.get("by_origin_node", {}),
    }


# Every cursor a remote can independently get stuck on (issue #121/#122):
# 'memory' is the bare remote_id itself; the rest are the '#'-suffixed
# entity-graph cursors _pull_graph_table keys its watermark under.
_CURSOR_KINDS = ("memory", "entities", "links", "entity_relations")


def _cursor_id(remote_id: str, kind: str) -> str:
    return remote_id if kind == "memory" else f"{remote_id}#{kind}"


def reset_pull_cursor(
    remote_id: str, kinds: Sequence[str] | None = None
) -> dict[str, Any]:
    """Rewind one or more of *remote_id*'s pull cursors to force a full re-pull.

    A keyset pull cursor only ever moves forward, so it can never recover
    records that sit *behind* its own watermark — e.g. a historical gap left
    by a partial restore, or (as happened in production) a node identity
    change during a migration that stranded older records under a watermark
    the new identity's cursor had already passed. ``remind_me_sync_reconcile``
    detects this (a cursor that stops advancing despite drift — issue #121)
    but had no built-in way to fix it: recovering required hand-editing
    ``sync_log`` via a second raw SQLite connection to the live DB file
    (issue #122). This does the same rewind through the server's own
    connection instead.

    Resetting is safe to run repeatedly and safe against concurrent sync
    activity: the next cycle's pull just re-walks from an earlier point and
    idempotently upserts (last-write-wins) whatever it finds, so already-
    -current local records are simply revisited, not duplicated or lost.

    Args:
        remote_id: The remote whose cursor(s) to reset — 'hub', or a peer's
            node_id.
        kinds: Which cursor(s) to reset, from ``_CURSOR_KINDS``
            ('memory', 'entities', 'links', 'entity_relations'). Defaults to
            all four.

    Returns:
        A dict with the reset cursor ids and their prior watermarks, for the
        caller to report back.
    """
    kinds = tuple(kinds) if kinds else _CURSOR_KINDS
    unknown = set(kinds) - set(_CURSOR_KINDS)
    if unknown:
        raise ValueError(f"unknown cursor kind(s): {sorted(unknown)} — expected one of {_CURSOR_KINDS}")

    db = _get_db()
    reset: list[dict[str, Any]] = []
    for kind in kinds:
        cursor_id = _cursor_id(remote_id, kind)
        row = db.execute(
            "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = ?",
            (cursor_id,),
        ).fetchone()
        before = {
            "last_pull": row["last_pull"] if row else _EPOCH,
            "last_pull_id": row["last_pull_id"] if row else "",
        }
        db.execute(
            """
            INSERT INTO sync_log (remote_id, last_pull, last_pull_id)
            VALUES (?, ?, '')
            ON CONFLICT (remote_id) DO UPDATE SET last_pull = excluded.last_pull, last_pull_id = ''
            """,
            (cursor_id, _EPOCH),
        )
        reset.append({"cursor": cursor_id, "kind": kind, "was": before})
    db.commit()
    log.info(
        "Reset pull cursor(s) for %s: %s", remote_id, [r["cursor"] for r in reset]
    )
    return {"remote_id": remote_id, "reset": reset}


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------


def _tailscale_socket() -> str:
    """Resolve the tailscaled local API socket path (config override first)."""
    if config.TAILSCALE_SOCKET:
        return config.TAILSCALE_SOCKET
    if sys.platform == "darwin":
        return "/var/run/tailscaled.socket"
    return "/var/run/tailscale/tailscaled.sock"


async def _discover_peers() -> list[dict[str, str]]:
    """Discover other remind_me peers.

    Static peers from config.STATIC_PEERS are always included (entries must
    be objects with string 'node_id' and 'url' keys). The Tailscale local API
    is then queried for online peers, each assumed reachable on PEER_PORT.

    Returns:
        A list of dicts with 'node_id' and 'url' keys, deduplicated by URL.
    """
    peers: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for entry in config.STATIC_PEERS:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("node_id"), str)
            and isinstance(entry.get("url"), str)
        ):
            url = entry["url"].rstrip("/")
            if url not in seen_urls:
                seen_urls.add(url)
                peers.append({"node_id": entry["node_id"], "url": url})
        else:
            log.warning("Ignoring malformed STATIC_PEERS entry: %r", entry)

    try:
        # Tailscale local API — available on all Tailscale nodes
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=_tailscale_socket())
        ) as ts_client:
            resp = await ts_client.get("http://local-tailscaled.sock/localapi/v0/status")
            status = resp.json()

        for name, peer in status.get("Peer", {}).items():
            if not peer.get("Online"):
                continue
            addrs = peer.get("TailscaleIPs", [])
            if not addrs:
                continue
            ip = addrs[0]
            url = f"http://{ip}:{PEER_PORT}"
            if url not in seen_urls:
                seen_urls.add(url)
                peers.append({"node_id": peer.get("HostName", name), "url": url})

    except Exception as e:
        log.debug("Tailscale peer discovery failed: %s", e)

    return peers


async def _probe_peer(client: httpx.AsyncClient, peer: dict) -> bool:
    """Check if a peer is running remind_me by hitting its /health endpoint.

    Also records the peer's reported build in ``_peer_versions`` (issue
    #208). This request already happened every cycle and its body was
    discarded; the body now carries ``version``, which is the single most
    useful fact when two nodes disagree, so throwing it away meant either
    living without it or making a second request for something already on
    the wire.

    A peer predating version reporting simply has no field, and nothing is
    recorded for it -- absent reads as "unknown", which is honest, whereas a
    placeholder would not be. Parsing failures are non-fatal for the same
    reason the probe itself is: this function's contract is reachability,
    and a peer that answers 200 is reachable whatever its body looks like.
    """
    try:
        resp = await client.get(
            f"{peer['url']}/health",
            headers=HEADERS,
            timeout=3,
        )
        if resp.status_code != 200:
            return False
        try:
            body = resp.json()
            version = body.get("version") if isinstance(body, dict) else None
            if version:
                _peer_versions[peer["node_id"]] = {
                    "version": str(version),
                    "at": _now_iso(),
                }
        except Exception as e:  # noqa: BLE001 — reachability is the contract
            log.debug("Peer %s /health body unparsable: %s", peer["node_id"], e)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------

async def _sync_once() -> None:
    """Run one full sync cycle: hub push/pull, peer push/pull, outbox prune."""
    # maybe_span is a plain (sync) context manager -- it can't share an
    # `async with` statement with httpx.AsyncClient (async), so it wraps the
    # whole cycle as its own outer `with` instead.
    with maybe_span("sync.cycle"):
        async with httpx.AsyncClient() as client:

            # --- Hub sync ---
            if HUB_URL:
                try:
                    await _push_outbox(
                        client, HUB_URL, "hub", max_batches=PUSH_BATCHES_PER_CYCLE
                    )
                    await _pull_remote(client, HUB_URL, "hub")
                    # Entity graph (FT-04) — no-ops against pre-FT-04 remotes.
                    await _pull_entities(client, HUB_URL, "hub")
                    await _pull_links(client, HUB_URL, "hub")
                    # Entity relations (Phase 3) — no-op against older remotes.
                    await _pull_entity_relations(client, HUB_URL, "hub")
                    log.info("Hub sync complete")
                    _clear_error("hub")
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    # ConnectTimeout/ReadTimeout etc. are TimeoutException, not
                    # ConnectError — both are ordinary transient-network
                    # conditions and belong at the same debug level, not in
                    # the catch-all "unexpected error" branch below (issue #124).
                    log.debug("Hub unreachable/timed out, skipping: %s", e)
                    _record_error("hub", e)
                except httpx.HTTPStatusError as e:
                    log.warning("Hub sync error: %s", e)
                    _record_error("hub", e)
                except Exception as e:
                    log.warning("Hub sync unexpected error: %s", e)
                    _record_error("hub", e)

            # --- Peer sync ---
            peers = await _discover_peers()
            for peer in peers:
                if peer["node_id"] == NODE_ID:
                    continue  # skip self
                if not await _probe_peer(client, peer):
                    continue
                try:
                    await _push_outbox(
                        client, peer["url"], peer["node_id"],
                        max_batches=PUSH_BATCHES_PER_CYCLE,
                    )
                    await _pull_remote(client, peer["url"], peer["node_id"])
                    # Entity graph (FT-04) — no-ops against pre-FT-04 peers.
                    await _pull_entities(client, peer["url"], peer["node_id"])
                    await _pull_links(client, peer["url"], peer["node_id"])
                    # Entity relations (Phase 3) — no-op against older peers.
                    await _pull_entity_relations(client, peer["url"], peer["node_id"])
                    log.info("Peer sync complete: %s", peer["node_id"])
                    _clear_error(peer["node_id"])
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    log.debug("Peer %s unreachable/timed out: %s", peer["node_id"], e)
                    _record_error(peer["node_id"], e)
                except Exception as e:
                    log.warning("Peer sync error (%s): %s", peer["node_id"], e)
                    _record_error(peer["node_id"], e)

        try:
            _prune_outbox(_get_db())
        except Exception as e:
            log.warning("Outbox prune failed: %s", e)

        try:
            _compact_tombstones(_get_db())
        except Exception as e:
            log.warning("Tombstone compaction failed: %s", e)


async def sync_loop() -> None:
    """Background task — runs forever, syncing every SYNC_INTERVAL seconds."""
    if not SYNC_ENABLED:
        log.info("Sync disabled (NODE_ID/HUB_URL/SYNC_SECRET not configured)")
        return
    log.info("Sync started — node_id=%s hub=%s interval=%ds", NODE_ID, HUB_URL, SYNC_INTERVAL)
    cycle = 0
    while True:
        cycle += 1
        log.info("Sync cycle %d starting", cycle)
        try:
            await _sync_once()
            log.info("Sync cycle %d complete", cycle)
        except Exception as e:
            log.error("Sync cycle %d error: %s", cycle, e, exc_info=True)
        log.info("Sleeping %ds until next cycle", SYNC_INTERVAL)
        await asyncio.sleep(SYNC_INTERVAL)
        log.info("Sync woke up for cycle %d", cycle + 1)

# ---------------------------------------------------------------------------
# Thread runner
# ---------------------------------------------------------------------------

_stop = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def start_sync_thread() -> threading.Thread:
    """Run the sync loop in a daemon thread with its own event loop.

    More reliable than asyncio.create_task() inside FastMCP's lifespan,
    which may run in a thread pool rather than the main event loop.

    Idempotent — a second call while already running returns the existing
    thread. The check-then-act is lock-protected (mirroring
    ``peer_server.start_peer_server``/``webhook_server.start_webhook_server``)
    so two concurrent callers can't both pass the liveness check and start
    two competing loops.
    """
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread

        def _run():
            log.info("Sync thread starting")
            while not _stop.is_set():
                try:
                    from remind_me_mcp.sidecars import ensure_sidecars
                    ensure_sidecars()
                except Exception as e:
                    log.warning("Sidecar ensure failed: %s", e)
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(_sync_once())
                    finally:
                        loop.close()
                        asyncio.set_event_loop(None)
                except Exception as e:
                    log.error("Sync thread error: %s", e, exc_info=True)
                log.info("Sync thread sleeping %ds", SYNC_INTERVAL)
                _stop.wait(SYNC_INTERVAL)
            log.info("Sync thread stopped")

        _stop.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="sync-loop")
        _thread.start()
        log.info("Sync loop thread started — alive: %s", _thread.is_alive())
        return _thread


def stop_sync_thread(timeout: float = 10.0) -> None:
    """Signal the sync loop to stop and join the thread (no-op when not running).

    Called from the server lifespan shutdown *before* ``_close_db()`` so an
    in-flight sync cycle finishes (or aborts) before the connection closes
    (SE-07), mirroring ``watcher.stop_watcher()`` and
    ``peer_server.stop_peer_server()``.

    Args:
        timeout: Max seconds to wait for the thread to exit.
    """
    global _thread
    _stop.set()
    with _thread_lock:
        thread = _thread
        _thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout)
