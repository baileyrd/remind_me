"""
remind-me sync hub — FastAPI server backed by Postgres.

Central sync point for the remind_me distributed sync engine
(remind_me_mcp/sync.py). Implements the same wire protocol as the peer
server (remind_me_mcp/peer_server.py), which is the reference
implementation:

- POST /sync/push           — upsert a batch of records (LWW on updated_at),
                              respond with accepted/processed_ids/failed
- GET  /sync/pull           — memory records since a (updated_at, id) keyset
                              cursor; legacy strict-since when no since_id
- GET  /sync/pull_entities  — entity records (record_type='entity')
- GET  /sync/pull_links     — memory_entities link records
                              (record_type='memory_entity', synthetic id)
- GET  /sync/pull_entity_relations — entity_relations records
                              (record_type='entity_relation', real id)
- GET  /health              — unauthenticated liveness probe (200 when the
                              DB is reachable, 503 otherwise)

Push records dispatch on record_type: absent = memory, 'entity' upserts
with LWW except aliases (always union-merged), 'memory_entity' and
'entity_relation' are immutable insert-or-ignore edges.

Timestamps are stored as canonical UTC ISO-8601 TEXT with COLLATE "C" so
string comparison is a correct ordering — exactly the convention the
SQLite clients use. The startup migration upgrades a database restored
from the legacy hub schema (TIMESTAMPTZ columns, 11 columns) in place.

One deliberate divergence from the peer server: pull's ``exclude_node``
filters on a hub-only ``origin_node`` column (the node that pushed the
record) rather than the record's ``node_id``. The client never rewrites
``node_id`` on update, so filtering on it makes a record's creator deaf
to every later edit other nodes push — peers compensate by pushing to
each other, but the hub is pull-only, so it must track pushers itself.
``origin_node`` never leaves the hub; the wire format is unchanged.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import psycopg
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

log = logging.getLogger("remind_me_hub")
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SYNC_SECRET = os.environ.get("SYNC_SECRET", "")

MAX_PULL_LIMIT = 500
_EPOCH = "1970-01-01T00:00:00+00:00"
# Wait this long for Postgres to come up before giving up (systemd After=
# only orders unit start, not server readiness).
_DB_WAIT_SECONDS = 120


# ---------------------------------------------------------------------------
# Timestamp canonicalization — must match remind_me_mcp.sync._canon_ts
# ---------------------------------------------------------------------------

def _canon_ts(value: Any) -> str:
    """Normalize a timestamp to canonical UTC ISO-8601 ('...+00:00')."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"not a timestamp: {value!r}")
    dt = datetime.fromisoformat(value.strip())
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat()


def _coerce_json_field(value: Any, default: Any) -> Any:
    """Return a JSON-serializable object for a tags/metadata/aliases field."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = default
    if value is None:
        value = default
    return value


# ---------------------------------------------------------------------------
# Schema + legacy migration
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id                TEXT COLLATE "C" PRIMARY KEY,
    content           TEXT NOT NULL,
    category          TEXT NOT NULL DEFAULT 'general',
    tags              JSONB NOT NULL DEFAULT '[]',
    source            TEXT NOT NULL DEFAULT 'manual',
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TEXT COLLATE "C" NOT NULL,
    updated_at        TEXT COLLATE "C" NOT NULL,
    capture_id        TEXT,
    node_id           TEXT,
    client            TEXT NOT NULL DEFAULT 'unknown',
    accessed_at       TEXT COLLATE "C",
    access_count      INTEGER NOT NULL DEFAULT 0,
    decay_rate        DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    vitality          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    base_weight       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    status            TEXT NOT NULL DEFAULT 'active',
    memory_type       TEXT NOT NULL DEFAULT 'unclassified',
    source_capture_id TEXT,
    subject           TEXT,
    predicate         TEXT,
    "object"          TEXT,
    superseded_by     TEXT,
    deleted_at        TEXT COLLATE "C",
    origin_node       TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    id          TEXT COLLATE "C" PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT,
    aliases     JSONB NOT NULL DEFAULT '[]',
    created_at  TEXT COLLATE "C" NOT NULL,
    updated_at  TEXT COLLATE "C" NOT NULL,
    node_id     TEXT,
    origin_node TEXT
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id  TEXT COLLATE "C" NOT NULL,
    entity_id  TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (memory_id, entity_id)
);

CREATE TABLE IF NOT EXISTS entity_relations (
    id                TEXT COLLATE "C" PRIMARY KEY,
    subject_entity_id TEXT COLLATE "C" NOT NULL,
    relation          TEXT NOT NULL,
    object_entity_id  TEXT COLLATE "C" NOT NULL,
    created_at        TEXT COLLATE "C" NOT NULL,
    updated_at        TEXT COLLATE "C" NOT NULL,
    node_id           TEXT,
    origin_node       TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_updated_at_id
    ON memories (updated_at, id);
CREATE INDEX IF NOT EXISTS idx_entities_updated_at_id
    ON entities (updated_at, id);
CREATE INDEX IF NOT EXISTS idx_links_created_at
    ON memory_entities (created_at);
CREATE INDEX IF NOT EXISTS idx_entity_relations_created_at_id
    ON entity_relations (created_at, id);
"""

# Convert a TIMESTAMPTZ column to the canonical TEXT form. Trailing
# fractional zeros are trimmed so the result matches Python's
# datetime.isoformat() exactly (no microseconds when zero) — mixed
# precision still string-orders correctly under COLLATE "C".
_TS_CONVERT = (
    "regexp_replace(to_char({col} AT TIME ZONE 'UTC', "
    "'YYYY-MM-DD\"T\"HH24:MI:SS.US'), '\\.?0+$', '') || '+00:00'"
)

# Columns added since the legacy hub schema, with client-matching defaults.
_NEW_MEMORY_COLUMNS = (
    ('accessed_at', 'TEXT COLLATE "C"'),
    ('access_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('decay_rate', 'DOUBLE PRECISION NOT NULL DEFAULT 0.1'),
    ('vitality', 'DOUBLE PRECISION NOT NULL DEFAULT 1.0'),
    ('base_weight', 'DOUBLE PRECISION NOT NULL DEFAULT 1.0'),
    ('status', "TEXT NOT NULL DEFAULT 'active'"),
    ('memory_type', "TEXT NOT NULL DEFAULT 'unclassified'"),
    ('source_capture_id', 'TEXT'),
    ('subject', 'TEXT'),
    ('predicate', 'TEXT'),
    ('"object"', 'TEXT'),
    ('superseded_by', 'TEXT'),
    ('deleted_at', 'TEXT COLLATE "C"'),
    ('origin_node', 'TEXT'),
)


def _migrate(conn: psycopg.Connection) -> None:
    """Create the schema; upgrade a legacy-hub database in place.

    The legacy hub stored 11 columns with TIMESTAMPTZ timestamps. A dump
    restored into this database is detected by column type and converted:
    timestamps become canonical ISO TEXT (COLLATE "C"), missing columns are
    added with defaults, and the entity-graph tables are created.
    """
    cols = {
        r["column_name"]: r
        for r in conn.execute(
            "SELECT column_name, data_type, collation_name "
            "FROM information_schema.columns WHERE table_name = 'memories'"
        ).fetchall()
    }

    for col in ("created_at", "updated_at", "accessed_at", "id"):
        info = cols.get(col)
        if info is None:
            continue
        if info["data_type"].startswith("timestamp"):
            expr = _TS_CONVERT.format(col=col)
            conn.execute(
                f'ALTER TABLE memories ALTER COLUMN {col} '
                f'TYPE TEXT COLLATE "C" USING {expr}'
            )
            log.info("Migrated memories.%s from %s to TEXT", col, info["data_type"])
        elif info["data_type"] == "text" and info["collation_name"] != "C":
            conn.execute(
                f'ALTER TABLE memories ALTER COLUMN {col} TYPE TEXT COLLATE "C"'
            )

    if cols:  # table pre-exists: add any missing columns
        for name, decl in _NEW_MEMORY_COLUMNS:
            conn.execute(
                f"ALTER TABLE memories ADD COLUMN IF NOT EXISTS {name} {decl}"
            )
        conn.execute(
            "UPDATE memories SET accessed_at = created_at WHERE accessed_at IS NULL"
        )

    conn.execute(_SCHEMA)
    conn.execute("ALTER TABLE entities ADD COLUMN IF NOT EXISTS origin_node TEXT")
    conn.commit()


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if not SYNC_SECRET:
        raise RuntimeError("SYNC_SECRET is not configured — refusing to start")
    deadline = time.monotonic() + _DB_WAIT_SECONDS
    while True:
        try:
            with _connect() as conn:
                _migrate(conn)
            break
        except psycopg.OperationalError as e:
            if time.monotonic() > deadline:
                raise
            log.info("Waiting for Postgres: %s", e)
            time.sleep(2)
    log.info("Schema ready; hub listening")
    yield


app = FastAPI(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> None:
    """Constant-time bearer check; always rejects when no secret is set."""
    auth = request.headers.get("Authorization", "")
    if not SYNC_SECRET or not hmac.compare_digest(auth, f"Bearer {SYNC_SECRET}"):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> JSONResponse:
    """Liveness probe -- responds even when Postgres is unreachable.

    The HTTP status code reflects DB connectivity (200 when reachable, 503
    when not) so deploy-time healthchecks that gate on 2xx (Railway's
    healthcheckPath, Docker Compose's ``depends_on: condition:
    service_healthy``) correctly catch "the hub can't reach its database" at
    rollout instead of always reporting success. This is deliberately NOT
    wired up as a continuously-polled auto-restart trigger anywhere in this
    repo's deploy templates -- a transient DB blip shouldn't kill and
    restart an otherwise-healthy hub process, only block a *new* deploy from
    being promoted while the old one keeps serving.
    """
    db_status = "ok"
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        db_status = f"error: {e}"
    healthy = db_status == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "role": "hub",
            "db": db_status,
            "time": datetime.now(UTC).isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Push — record-type dispatch, per-record isolation, LWW
# ---------------------------------------------------------------------------

_MEMORY_UPSERT = """
INSERT INTO memories
    (id, content, category, tags, source, metadata,
     created_at, updated_at, capture_id, node_id, client,
     accessed_at, access_count, decay_rate, vitality, base_weight,
     status, memory_type, source_capture_id,
     subject, predicate, "object", superseded_by, deleted_at, origin_node)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET
    content           = EXCLUDED.content,
    category          = EXCLUDED.category,
    tags              = EXCLUDED.tags,
    source            = EXCLUDED.source,
    metadata          = EXCLUDED.metadata,
    updated_at        = EXCLUDED.updated_at,
    capture_id        = EXCLUDED.capture_id,
    node_id           = EXCLUDED.node_id,
    client            = EXCLUDED.client,
    accessed_at       = EXCLUDED.accessed_at,
    access_count      = EXCLUDED.access_count,
    decay_rate        = EXCLUDED.decay_rate,
    vitality          = EXCLUDED.vitality,
    base_weight       = EXCLUDED.base_weight,
    status            = EXCLUDED.status,
    memory_type       = EXCLUDED.memory_type,
    source_capture_id = EXCLUDED.source_capture_id,
    subject           = EXCLUDED.subject,
    predicate         = EXCLUDED.predicate,
    "object"          = EXCLUDED."object",
    superseded_by     = EXCLUDED.superseded_by,
    deleted_at        = EXCLUDED.deleted_at,
    origin_node       = EXCLUDED.origin_node
WHERE EXCLUDED.updated_at > memories.updated_at
"""

_REQUIRED_MEMORY_KEYS = ("id", "content", "created_at", "updated_at")


def _upsert_memory(
    conn: psycopg.Connection, rec: dict[str, Any], origin: str | None
) -> bool:
    """Upsert one memory record (LWW on updated_at). True when applied.

    Whole-row LWW for every column, unlike the client-side peer/hub-pull
    path (``remind_me_mcp.sync._upsert_one``), which field-level merges
    ``tags``/``metadata`` regardless of which side wins (issue #60). Two
    devices pushing conflicting field-level edits *through the hub* can
    still have one clobber the other's non-conflicting field here — a
    known, deliberate scope decision (this module needs a live Postgres to
    test at all; `hub/e2e_test.py` is explicitly outside the pytest suite),
    not an oversight. Once a record reaches a client via pull, that
    client's own merge logic still recovers the union for any field the
    *client* independently changed, so the gap is narrower than it looks —
    it's specifically "two pushes racing at the hub before either side
    pulls," not the general two-devices-diverge case.
    """
    missing = [k for k in _REQUIRED_MEMORY_KEYS if not rec.get(k)]
    if missing:
        raise ValueError(f"record missing required keys: {missing}")

    created_at = _canon_ts(rec["created_at"])
    updated_at = _canon_ts(rec["updated_at"])
    try:
        accessed_at = _canon_ts(rec.get("accessed_at"))
    except ValueError:
        accessed_at = created_at
    deleted_at = _canon_ts(rec["deleted_at"]) if rec.get("deleted_at") else None

    cur = conn.execute(_MEMORY_UPSERT, (
        str(rec["id"]),
        str(rec["content"]),
        rec.get("category") or "general",
        Jsonb(_coerce_json_field(rec.get("tags"), [])),
        rec.get("source") or "manual",
        Jsonb(_coerce_json_field(rec.get("metadata"), {})),
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
        deleted_at,
        origin,
    ))
    return cur.rowcount > 0


def _upsert_entity(
    conn: psycopg.Connection, rec: dict[str, Any], origin: str | None
) -> bool:
    """Upsert one entity record: LWW on updated_at, aliases union-merge.

    Mirrors remind_me_mcp.sync._upsert_entity_one — aliases merge regardless
    of which side wins LWW (union is commutative and idempotent so all nodes
    converge), and a losing record's kind still fills a missing local kind.
    """
    missing = [k for k in ("id", "name", "created_at", "updated_at") if not rec.get(k)]
    if missing:
        raise ValueError(f"entity record missing required keys: {missing}")

    created_at = _canon_ts(rec["created_at"])
    updated_at = _canon_ts(rec["updated_at"])
    eid = str(rec["id"])
    incoming = _coerce_json_field(rec.get("aliases"), [])
    if not isinstance(incoming, list):
        incoming = []
    incoming = [a for a in incoming if isinstance(a, str) and a]

    row = conn.execute(
        "SELECT name, kind, aliases, updated_at FROM entities "
        "WHERE id = %s FOR UPDATE",
        (eid,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO entities (id, name, kind, aliases, created_at, "
            "updated_at, node_id, origin_node) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (eid, str(rec["name"]), rec.get("kind"),
             Jsonb(list(dict.fromkeys(incoming))),
             created_at, updated_at, rec.get("node_id"), origin),
        )
        return True

    local_aliases = row["aliases"] if isinstance(row["aliases"], list) else []
    merged = list(dict.fromkeys([*local_aliases, *incoming]))
    if updated_at > row["updated_at"]:
        conn.execute(
            "UPDATE entities SET name = %s, kind = %s, aliases = %s, "
            "updated_at = %s, node_id = %s, origin_node = %s WHERE id = %s",
            (str(rec["name"]), rec.get("kind") or row["kind"],
             Jsonb(merged), updated_at, rec.get("node_id"), origin, eid),
        )
        return True
    fill_kind = row["kind"] or rec.get("kind")
    if merged != local_aliases or fill_kind != row["kind"]:
        # LWW-losing enrichment. The peer protocol leaves updated_at alone
        # (the contributor's outbox propagates the aliases peer-to-peer),
        # but the hub is pull-only: without a bump, nodes whose cursor has
        # passed this entity would never see the merge. Bumping is safe —
        # union-merge is idempotent, so a re-pulled merge that changes
        # nothing does not bump again, and the cycle terminates.
        conn.execute(
            "UPDATE entities SET aliases = %s, kind = %s, updated_at = %s, "
            "origin_node = NULL WHERE id = %s",
            (Jsonb(merged), fill_kind,
             datetime.now(UTC).isoformat(), eid),
        )
        return True
    return False


def _upsert_link(conn: psycopg.Connection, rec: dict[str, Any]) -> bool:
    """Apply one memory_entities link record (immutable, insert-or-ignore)."""
    missing = [k for k in ("memory_id", "entity_id", "created_at") if not rec.get(k)]
    if missing:
        raise ValueError(f"link record missing required keys: {missing}")
    cur = conn.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) "
        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (str(rec["memory_id"]), str(rec["entity_id"]), _canon_ts(rec["created_at"])),
    )
    return cur.rowcount > 0


def _upsert_entity_relation(
    conn: psycopg.Connection, rec: dict[str, Any], origin: str | None
) -> bool:
    """Apply one entity_relations record (immutable, insert-or-ignore).

    Mirrors remind_me_mcp.sync._upsert_entity_relation_one / _upsert_link --
    relations never change once recorded, so there is no LWW conflict to
    resolve, and the record already carries its own real id (deterministic,
    from the subject/relation/object triple) rather than a synthetic one.
    """
    missing = [
        k for k in ("id", "subject_entity_id", "relation", "object_entity_id", "created_at")
        if not rec.get(k)
    ]
    if missing:
        raise ValueError(f"entity_relation record missing required keys: {missing}")
    created_at = _canon_ts(rec["created_at"])
    updated_at = _canon_ts(rec.get("updated_at") or rec["created_at"])
    cur = conn.execute(
        "INSERT INTO entity_relations "
        "(id, subject_entity_id, relation, object_entity_id, created_at, "
        "updated_at, node_id, origin_node) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            str(rec["id"]), str(rec["subject_entity_id"]), str(rec["relation"]),
            str(rec["object_entity_id"]), created_at, updated_at,
            rec.get("node_id"), origin,
        ),
    )
    return cur.rowcount > 0


def _record_wire_id(rec: dict[str, Any]) -> str:
    """The id the sender matches in processed_ids (links use memory_id|entity_id)."""
    rid = rec.get("id")
    if rid is None and rec.get("memory_id") and rec.get("entity_id"):
        rid = f"{rec['memory_id']}|{rec['entity_id']}"
    return str(rid)


@app.post("/sync/push", dependencies=[Depends(_require_auth)])
def sync_push(body: Annotated[dict, Body(...)]) -> dict:
    records = body.get("records", [])
    if not isinstance(records, list):
        raise HTTPException(status_code=400, detail="invalid push payload")

    applied = 0
    failed = 0
    processed_ids: list[str] = []
    origin = body.get("node_id")
    origin = str(origin) if origin else None

    with _connect() as conn:
        for rec in records:
            try:
                if not isinstance(rec, dict):
                    raise ValueError(f"record is not an object: {type(rec).__name__}")
                record_type = rec.get("record_type") or "memory"
                # Savepoint per record: one malformed record must not poison
                # the rest of the batch.
                with conn.transaction():
                    if record_type == "memory":
                        ok = _upsert_memory(conn, rec, origin)
                    elif record_type == "entity":
                        ok = _upsert_entity(conn, rec, origin)
                    elif record_type == "memory_entity":
                        ok = _upsert_link(conn, rec)
                    elif record_type == "entity_relation":
                        ok = _upsert_entity_relation(conn, rec, origin)
                    else:
                        raise ValueError(f"unknown record_type: {record_type!r}")
            except Exception as e:
                failed += 1
                log.warning("Skipping malformed sync record: %s", e)
                continue
            processed_ids.append(_record_wire_id(rec))
            if ok:
                applied += 1

    log.info(
        "Push from %s: %d records, %d applied, %d failed",
        body.get("node_id", "unknown"), len(records), applied, failed,
    )
    return {"accepted": applied, "processed_ids": processed_ids, "failed": failed}


# ---------------------------------------------------------------------------
# Pull — keyset cursor (updated_at, id), drain-friendly ascending pages
# ---------------------------------------------------------------------------

def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PULL_LIMIT))


# Wire columns — origin_node is hub bookkeeping and never leaves the hub.
_MEMORY_WIRE_COLUMNS = (
    'id, content, category, tags, source, metadata, created_at, updated_at, '
    'capture_id, node_id, client, accessed_at, access_count, decay_rate, '
    'vitality, base_weight, status, memory_type, source_capture_id, '
    'subject, predicate, "object", superseded_by, deleted_at'
)
_ENTITY_WIRE_COLUMNS = "id, name, kind, aliases, created_at, updated_at, node_id"
_ENTITY_RELATION_WIRE_COLUMNS = (
    "id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id"
)


@app.get("/sync/pull", dependencies=[Depends(_require_auth)])
def sync_pull(
    since: str = _EPOCH,
    since_id: str | None = None,
    exclude_node: str | None = None,
    limit: int = Query(default=MAX_PULL_LIMIT),
) -> dict:
    # Keyset cursor when the client sends since_id (current clients always
    # do); legacy strict updated_at comparison otherwise.
    if since_id is not None:
        where = "(updated_at > %s OR (updated_at = %s AND id > %s))"
        params: list[Any] = [since, since, since_id]
    else:
        where = "updated_at > %s"
        params = [since]
    if exclude_node:
        where += " AND (origin_node IS NULL OR origin_node != %s)"
        params.append(exclude_node)
    params.append(_clamp_limit(limit))

    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_MEMORY_WIRE_COLUMNS} FROM memories WHERE {where} "
            f"ORDER BY updated_at ASC, id ASC LIMIT %s",
            params,
        ).fetchall()

    return {"records": rows, "count": len(rows)}


@app.get("/sync/pull_entities", dependencies=[Depends(_require_auth)])
def sync_pull_entities(
    since: str = _EPOCH,
    since_id: str = "",
    exclude_node: str | None = None,
    limit: int = Query(default=MAX_PULL_LIMIT),
) -> dict:
    where = "(updated_at > %s OR (updated_at = %s AND id > %s))"
    params: list[Any] = [since, since, since_id]
    if exclude_node:
        where += " AND (origin_node IS NULL OR origin_node != %s)"
        params.append(exclude_node)
    params.append(_clamp_limit(limit))

    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_ENTITY_WIRE_COLUMNS} FROM entities WHERE {where} "
            f"ORDER BY updated_at ASC, id ASC LIMIT %s",
            params,
        ).fetchall()

    records = [{**row, "record_type": "entity"} for row in rows]
    return {"records": records, "count": len(records)}


@app.get("/sync/pull_links", dependencies=[Depends(_require_auth)])
def sync_pull_links(
    since: str = _EPOCH,
    since_id: str = "",
    limit: int = Query(default=MAX_PULL_LIMIT),
) -> dict:
    # Links are immutable: keyset on created_at with the synthetic key
    # memory_id || '|' || entity_id — the same expression in ORDER BY so
    # server ordering and the client's cursor comparison agree exactly.
    key = "(memory_id || '|' || entity_id)"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT memory_id, entity_id, created_at FROM memory_entities "
            f"WHERE (created_at > %s OR (created_at = %s AND {key} > %s)) "
            f"ORDER BY created_at ASC, {key} ASC LIMIT %s",
            (since, since, since_id, _clamp_limit(limit)),
        ).fetchall()

    records = [
        {
            "record_type": "memory_entity",
            "id": f"{row['memory_id']}|{row['entity_id']}",
            "memory_id": row["memory_id"],
            "entity_id": row["entity_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {"records": records, "count": len(records)}


@app.get("/sync/pull_entity_relations", dependencies=[Depends(_require_auth)])
def sync_pull_entity_relations(
    since: str = _EPOCH,
    since_id: str = "",
    limit: int = Query(default=MAX_PULL_LIMIT),
) -> dict:
    """Relations are immutable: keyset on (created_at, id), like links --
    but each row already carries its own real id, so no synthetic key."""
    where = "(created_at > %s OR (created_at = %s AND id > %s))"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_ENTITY_RELATION_WIRE_COLUMNS} FROM entity_relations "
            f"WHERE {where} ORDER BY created_at ASC, id ASC LIMIT %s",
            (since, since, since_id, _clamp_limit(limit)),
        ).fetchall()

    records = [{**row, "record_type": "entity_relation"} for row in rows]
    return {"records": records, "count": len(records)}
