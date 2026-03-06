"""
remind_me_mcp.sync — Background sync engine.

Handles hub sync and peer sync independently. Both use the same
outbox/pull protocol — the only difference is the endpoint URL.

Hub sync:   push local outbox → hub, pull hub changes → local
Peer sync:  push local outbox → peer, pull peer changes → local
            (peers discovered via Tailscale local API or mDNS)

Conflict resolution: last-write-wins on updated_at (applied in the
upsert query). This is safe because memories are append-dominant —
true concurrent updates to the same record are rare.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

import httpx

from remind_me_mcp.config import (
    HUB_URL,
    NODE_ID,
    PEER_PORT,
    SYNC_ENABLED,
    SYNC_INTERVAL,
    SYNC_SECRET,
)
from remind_me_mcp.db import _get_db, _now_iso

log = logging.getLogger("remind_me_mcp.sync")

HEADERS = {"Authorization": f"Bearer {SYNC_SECRET}"}
BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# Outbox push
# ---------------------------------------------------------------------------

async def _push_outbox(client: httpx.AsyncClient, url: str) -> int:
    db = _get_db()

    while True:
        rows = db.execute("""
            SELECT id, payload FROM sync_outbox
            WHERE sent_at = ''
            ORDER BY id ASC
            LIMIT ?
        """, (BATCH_SIZE,)).fetchall()

        if not rows:
            return 0

        records = []
        for r in rows:
            payload = json.loads(r["payload"])
            # Deserialize JSON string fields into proper types
            for field in ("tags", "metadata"):
                if isinstance(payload.get(field), str):
                    try:
                        payload[field] = json.loads(payload[field])
                    except (json.JSONDecodeError, TypeError):
                        payload[field] = [] if field == "tags" else {}
            records.append(payload)

        row_ids = [r["id"] for r in rows]

        resp = await client.post(
            f"{url}/sync/push",
            json={"node_id": NODE_ID, "records": records},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        accepted = resp.json().get("accepted", 0)

        now = _now_iso()
        db.execute(
            f"UPDATE sync_outbox SET sent_at = ? "
            f"WHERE id IN ({','.join('?' * len(row_ids))})",
            [now, *row_ids],
        )
        db.commit()
        log.debug("Pushed %d records to %s (%d accepted)", len(records), url, accepted)

        if len(rows) < BATCH_SIZE:
            break

    return accepted


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------

async def _pull_remote(client: httpx.AsyncClient, url: str, remote_id: str) -> int:
    """Pull new memories from a remote since our last pull timestamp.

    Returns the number of records upserted locally.
    """
    db = _get_db()

    last_pull = db.execute(
        "SELECT last_pull FROM sync_log WHERE remote_id = ?", (remote_id,)
    ).fetchone()
    since = last_pull["last_pull"] if last_pull else "1970-01-01T00:00:00+00:00"

    resp = await client.get(
        f"{url}/sync/pull",
        params={"since": since, "exclude_node": NODE_ID, "limit": 500},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    data = resp.json()
    records = data.get("records", [])
    if not records:
        return 0

    upserted = _upsert_records(db, records)

    # Update sync_log with the latest updated_at we received
    latest = max(r["updated_at"] for r in records)
    db.execute("""
        INSERT INTO sync_log (remote_id, last_pull)
        VALUES (?, ?)
        ON CONFLICT (remote_id) DO UPDATE SET last_pull = excluded.last_pull
    """, (remote_id, latest))
    db.commit()

    log.debug("Pulled %d records from %s, upserted %d", len(records), url, upserted)
    return upserted


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert_records(db: sqlite3.Connection, records: list[dict[str, Any]]) -> int:
    """Upsert a list of memory records with last-write-wins conflict resolution.

    Skips inserting to the outbox — these are incoming remote records,
    not local writes. We temporarily disable the outbox triggers by checking
    a flag... actually we use a cleaner approach: direct insert bypassing
    triggers is not possible in SQLite, so instead we mark these as
    already-sent in the outbox immediately after insert.
    """
    upserted = 0
    for rec in records:
        # Tags come in as a JSON string from SQLite peers or a list from Postgres hub
        tags = rec.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError:
                tags = []

        metadata = rec.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        result = db.execute("""
            INSERT INTO memories
                (id, content, category, tags, source, metadata,
                 created_at, updated_at, capture_id, node_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content    = excluded.content,
                category   = excluded.category,
                tags       = excluded.tags,
                metadata   = excluded.metadata,
                updated_at = excluded.updated_at,
                capture_id = excluded.capture_id,
                node_id    = excluded.node_id
            WHERE excluded.updated_at > memories.updated_at
            RETURNING id
        """, (
            rec["id"],
            rec["content"],
            rec.get("category", "general"),
            json.dumps(tags),
            rec.get("source", "manual"),
            json.dumps(metadata),
            rec["created_at"],
            rec["updated_at"],
            rec.get("capture_id"),
            rec.get("node_id"),
        ))

        if result.fetchone():
            upserted += 1
            # Mark the outbox entry for this record as already sent
            # so we don't echo it back to the remote we just got it from
            db.execute("""
                UPDATE sync_outbox SET sent_at = ?
                WHERE memory_id = ? AND sent_at = ''
            """, (_now_iso(), rec["id"]))

    db.commit()
    return upserted


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------

async def _discover_peers() -> list[dict[str, str]]:
    """Discover other remind_me peers via Tailscale local API.

    Queries the Tailscale local API for all online peers, then probes
    each one on PEER_PORT to see if they're running remind_me.

    Returns a list of dicts with 'node_id' and 'url' keys.
    """
    peers = []
    try:
        # Tailscale local API — available on all Tailscale nodes
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds="/var/run/tailscale/tailscaled.sock")
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
            peers.append({"node_id": peer.get("HostName", name), "url": url})

    except Exception as e:
        log.debug("Tailscale peer discovery failed: %s", e)

    return peers


async def _probe_peer(client: httpx.AsyncClient, peer: dict) -> bool:
    """Check if a peer is running remind_me by hitting its /health endpoint."""
    try:
        resp = await client.get(
            f"{peer['url']}/health",
            headers=HEADERS,
            timeout=3,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------

async def _sync_once() -> None:
    """Run one full sync cycle: hub push/pull, then peer push/pull."""
    async with httpx.AsyncClient() as client:

        # --- Hub sync ---
        if HUB_URL:
            try:
                await _push_outbox(client, HUB_URL)
                await _pull_remote(client, HUB_URL, "hub")
                log.info("Hub sync complete")
            except httpx.ConnectError:
                log.debug("Hub unreachable, skipping")
            except httpx.HTTPStatusError as e:
                log.warning("Hub sync error: %s", e)
            except Exception as e:
                log.warning("Hub sync unexpected error: %s", e)

        # --- Peer sync ---
        peers = await _discover_peers()
        for peer in peers:
            if peer["node_id"] == NODE_ID:
                continue  # skip self
            if not await _probe_peer(client, peer):
                continue
            try:
                await _push_outbox(client, peer["url"])
                await _pull_remote(client, peer["url"], peer["node_id"])
                log.info("Peer sync complete: %s", peer["node_id"])
            except httpx.ConnectError:
                log.debug("Peer %s unreachable", peer["node_id"])
            except Exception as e:
                log.warning("Peer sync error (%s): %s", peer["node_id"], e)


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


def start_sync_thread() -> threading.Thread:
    """Run the sync loop in a daemon thread with its own event loop.

    More reliable than asyncio.create_task() inside FastMCP's lifespan,
    which may run in a thread pool rather than the main event loop.
    """
    import time

    def _run():
        log.info("Sync thread starting")
        while True:
            try:
                import asyncio
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
            time.sleep(SYNC_INTERVAL)
            log.info("Sync thread waking up")

    thread = threading.Thread(target=_run, daemon=True, name="sync-loop")
    thread.start()
    log.info("Sync loop thread started — alive: %s", thread.is_alive())
    return thread
