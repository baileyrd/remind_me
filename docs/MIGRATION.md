# Migrating a remind_me node to new hardware

A "node" is one machine's `memory.db` (default `~/.remind-me/memory.db`) plus its
`NODE_ID`. Moving a node means copying that database to new hardware and letting the
new machine take over syncing to the hub. It's a **clean forward copy** — but three
things bite if you skip them. This runbook is what worked; the gotchas are why.

## Steps

1. **Snapshot the source DB consistently** (safe while the source server keeps running —
   `VACUUM INTO` takes a read-transaction snapshot and compacts free pages):

   ```bash
   sqlite3 ~/.remind-me/memory.db "VACUUM INTO '/path/to/snapshot.db'"
   ```

2. **Back up and replace** the target's DB. Remove any stale `-wal`/`-shm` first:

   ```bash
   mv ~/.remind-me/memory.db ~/.remind-me/memory.db.bak      # if one exists
   rm -f ~/.remind-me/memory.db-wal ~/.remind-me/memory.db-shm
   cp /path/to/snapshot.db ~/.remind-me/memory.db
   ```

3. **Open it once with the current server** so it migrates the schema. Migrations are
   **forward-only and additive** — a v10 DB opened by v12 code just gains a few empty
   tables (wiki index, mempalace dedup, entities); existing `memories` rows are never
   touched. Nothing to do but start the server (or call `_get_db()`).

4. **Reset the sync cursor** — see gotcha 1 below. Do this before the first sync.

5. **Reindex if the embedding backend/model differs** — see gotcha 2.

6. **Verify**: local memory count should reconcile with the hub after a sync cycle
   (compare `SELECT count(*) FROM memories` against a full `/sync/pull` paged by
   `(updated_at, id)`; the gap should reach zero).

## Gotcha 1 — the migration carries the pull watermark

`memory.db` includes the `sync_log` table, which holds this node's **pull cursor**
(`last_pull` / `last_pull_id` per `remote_id`). A copied DB carries the *source*
machine's cursor. If the new machine uses a fresh `NODE_ID`, it inherits that old
watermark and will **silently skip every hub record older than the cursor** — records
the source node never had (e.g. memories authored on other nodes) never get pulled.

Symptom: after the first sync the new node has *most* memories but is missing an older
subset, and repeated sync cycles never close the gap (the cursor is already past them).

Fix — reset the cursor to epoch so the node re-pages the full history. Upserts are
keyed by memory `id`, so re-pulling everything is idempotent (no duplicates):

```sql
UPDATE sync_log SET last_pull = '1970-01-01T00:00:00+00:00', last_pull_id = ''
WHERE remote_id = 'hub';
```

Then run a sync. The node re-pulls all pages and fills the gap.

## Gotcha 2 — embeddings are backend-specific

Vectors are tied to the embedding backend/model that produced them. The default ONNX
backend (`all-MiniLM-L6-v2`) is **384-dim**; Ollama `nomic-embed-text` is **768-dim**.
If the new machine's `REMIND_ME_EMBEDDING_BACKEND` / model differs from the source, the
copied vectors won't match new queries — and the whole fleet must agree, since the hub
stores vectors too. Keep the backend identical across nodes, or run `remind_me_reindex`
after switching (and expect divergence from any node still on the old backend).

Related: if the source stored embeddings in `memories_vec` but left the `vec_chunks`
link table empty (older code), chunk-based search finds nothing. `remind_me_reindex`
rebuilds `vec_chunks` from `memories.content`; clear orphaned `memories_vec` rows first
so you don't double-store.

## Gotcha 3 — give the new node a unique NODE_ID

Each machine needs its own `NODE_ID`. Reusing the retired node's id makes the hub treat
them as the same logical node (shared watermark); a distinct id keeps them independent.
With gotcha 1's cursor reset, a fresh id is the clean choice.
