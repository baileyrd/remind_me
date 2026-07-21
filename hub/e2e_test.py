"""End-to-end test: the real remind_me_mcp client against a running hub.

Not part of the pytest suite — needs a live Postgres and a running hub.
Exercises the full sync path with two simulated nodes (real SQLite
databases, real outbox triggers, real `sync._sync_once()`):

- node A pushes a memory + entity + link; the hub stores them
- a fresh node B pulls and converges to node A's state
- an update on node B propagates B -> hub -> A (the origin_node fix:
  the record still carries node_id='node-a', so the peer-style
  exclude filter would have hidden it from node A forever)
- a stale record is processed (marked sent) but not applied (LWW)
- auth and malformed-record isolation

Run it:

    # 1. Postgres with a remindme/remindme database available
    # 2. Hub:  DATABASE_URL=... SYNC_SECRET=test-secret uvicorn main:app --port 8765
    # 3. Test deps: pip install -e ../  psycopg[binary] httpx
    HUB_TEST_DSN=postgresql://remindme:...@host:5432/remindme python e2e_test.py

The test writes its node databases to /tmp/node-a and /tmp/node-b (wipe
them between runs) and inserts records into the hub's database — run it
against a hub whose database you can throw away, never production.
"""
import asyncio
import os
import sys

HUB_URL = os.environ.get("HUB_TEST_URL", "http://127.0.0.1:8765")
SECRET = os.environ.get("HUB_TEST_SECRET", "test-secret")
DSN = os.environ.get(
    "HUB_TEST_DSN", "postgresql://remindme:testpw@127.0.0.1:5444/remindme"
)


def make_node(node_id: str, tmpdir: str):
    """Configure env and (re)import the client modules for one node."""
    os.environ["REMIND_ME_MCP_DIR"] = tmpdir
    os.environ["REMIND_ME_NODE_ID"] = node_id
    os.environ["REMIND_ME_HUB_URL"] = HUB_URL
    os.environ["REMIND_ME_SYNC_SECRET"] = SECRET
    os.environ["REMIND_ME_CLIENT"] = "e2e-test"
    # Force clean re-import so module-level config picks up this node's env
    for mod in list(sys.modules):
        if mod.startswith("remind_me_mcp"):
            del sys.modules[mod]
    from remind_me_mcp import db as db_mod
    from remind_me_mcp import sync as sync_mod
    db = db_mod._get_db()
    return db_mod, sync_mod, db


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        sys.exit(1)


# ---------------- Node A: create local data, sync ----------------
db_mod, sync_mod, db = make_node("node-a", "/tmp/node-a")
now = db_mod._now_iso()

db.execute(
    "INSERT INTO memories (id, content, category, tags, source, metadata, "
    "created_at, updated_at, accessed_at, node_id, client) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("node-a-mem-1", "Memory from node A", "fact", '["sync","e2e"]', "manual",
     '{"k": 1}', now, now, now, "node-a", "e2e-test"),
)
eid = db_mod._entity_id("Bailey Robertson")
db_mod._upsert_entity(db, "Bailey Robertson", kind="person", aliases=["bailey"])
db_mod._link_memory_entity(db, "node-a-mem-1", eid)
eid2 = db_mod._entity_id("remind_me")
db_mod._upsert_entity(db, "remind_me", kind="project")
rel_id = db_mod._upsert_entity_relation(db, eid, "maintains", eid2)
db.commit()

outbox = db.execute(
    "SELECT COUNT(*) c FROM sync_outbox WHERE sent_at = ''"
).fetchone()["c"]
check("node A outbox captured writes", outbox >= 5, f"{outbox} rows")

asyncio.run(sync_mod._sync_once())

unsent = db.execute(
    "SELECT COUNT(*) c FROM sync_outbox WHERE sent_at = '' AND id NOT IN "
    "(SELECT outbox_id FROM sync_sends WHERE remote_id='hub')"
).fetchone()["c"]
check("node A outbox fully pushed to hub", unsent == 0, f"{unsent} unsent")
db_mod._close_db()

# ---------------- Hub-side verification ----------------
import psycopg  # noqa: E402 — optional dep imported lazily, only where used

with psycopg.connect(DSN) as conn:
    mem = conn.execute(
        "SELECT content, tags, metadata, node_id FROM memories "
        "WHERE id = 'node-a-mem-1'"
    ).fetchone()
    check("hub stored node A memory",
          mem is not None and mem[0] == "Memory from node A")
    check("hub stored tags/metadata as JSONB",
          mem[1] == ["sync", "e2e"] and mem[2] == {"k": 1}, f"{mem[1]} {mem[2]}")
    ent = conn.execute("SELECT name, kind, aliases FROM entities").fetchall()
    check("hub stored entity with aliases",
          len(ent) == 1 and ent[0][1] == "person" and "bailey" in ent[0][2],
          str(ent))
    lnk = conn.execute("SELECT memory_id, entity_id FROM memory_entities").fetchall()
    check("hub stored memory-entity link",
          len(lnk) == 1 and lnk[0][0] == "node-a-mem-1")
    rel = conn.execute(
        "SELECT id, subject_entity_id, relation, object_entity_id "
        "FROM entity_relations"
    ).fetchall()
    check("hub stored entity relation",
          len(rel) == 1 and rel[0][0] == rel_id
          and rel[0][1] == eid and rel[0][2] == "maintains" and rel[0][3] == eid2,
          str(rel))

# ---------------- Node B: fresh node converges ----------------
db_mod, sync_mod, db = make_node("node-b", "/tmp/node-b")
asyncio.run(sync_mod._sync_once())

ids = [r["id"] for r in db.execute(
    "SELECT id FROM memories WHERE id = 'node-a-mem-1'").fetchall()]
check("node B pulled node A memory", ids == ["node-a-mem-1"], str(ids))
ents = db.execute("SELECT name, kind, aliases FROM entities").fetchall()
check("node B pulled entity", len(ents) == 1 and ents[0]["kind"] == "person")
links = db.execute("SELECT memory_id, entity_id FROM memory_entities").fetchall()
check("node B pulled link",
      len(links) == 1 and links[0]["memory_id"] == "node-a-mem-1")
rels = db.execute(
    "SELECT id, subject_entity_id, relation, object_entity_id FROM entity_relations"
).fetchall()
check("node B pulled entity relation",
      len(rels) == 1 and rels[0]["id"] == rel_id
      and rels[0]["relation"] == "maintains",
      str([dict(r) for r in rels]))

# ---------------- LWW: node B updates, node A sees it ----------------
# Mirrors remind_me_update: content + updated_at change, node_id does NOT.
later = db_mod._now_iso()
db.execute(
    "UPDATE memories SET content = 'Updated by node B', updated_at = ? "
    "WHERE id = 'node-a-mem-1'", (later,),
)
db.commit()
asyncio.run(sync_mod._sync_once())
db_mod._close_db()

db_mod, sync_mod, db = make_node("node-a", "/tmp/node-a")
asyncio.run(sync_mod._sync_once())
content = db.execute(
    "SELECT content FROM memories WHERE id = 'node-a-mem-1'"
).fetchone()["content"]
check("LWW update propagated B -> hub -> A", content == "Updated by node B", content)

# ---------------- Gap #11: delete/tombstone propagates A -> hub -> B ----------------
# Mirrors remind_me_delete's soft-delete path: an UPDATE setting deleted_at,
# not a hard DELETE (which would produce no outbox row at all).
deleted_at = db_mod._now_iso()
db.execute(
    "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = 'node-a-mem-1'",
    (deleted_at, deleted_at),
)
db.commit()
asyncio.run(sync_mod._sync_once())
db_mod._close_db()

with psycopg.connect(DSN) as conn:
    row = conn.execute(
        "SELECT deleted_at FROM memories WHERE id = 'node-a-mem-1'"
    ).fetchone()
    check("hub stored the tombstone", row is not None and row[0] is not None, str(row))

db_mod, sync_mod, db = make_node("node-b", "/tmp/node-b")
asyncio.run(sync_mod._sync_once())
row = db.execute(
    "SELECT deleted_at FROM memories WHERE id = 'node-a-mem-1'"
).fetchone()
check("node B pulled the tombstone (no resurrection)",
      row is not None and row["deleted_at"] is not None, str(dict(row) if row else None))
db_mod._close_db()

# ---------------- Wire-level edge cases ----------------
import httpx  # noqa: E402 — optional dep imported lazily, only where used

stale = {"node_id": "node-x", "records": [{
    "id": "node-a-mem-1", "content": "STALE", "created_at": now,
    "updated_at": "2020-01-01T00:00:00+00:00"}]}
r = httpx.post(f"{HUB_URL}/sync/push", json=stale,
               headers={"Authorization": f"Bearer {SECRET}"})
body = r.json()
check("stale record processed but not applied",
      body["accepted"] == 0 and body["processed_ids"] == ["node-a-mem-1"],
      str(body))

r = httpx.get(f"{HUB_URL}/sync/pull")
check("unauthenticated pull rejected", r.status_code == 401)
r = httpx.get(f"{HUB_URL}/sync/pull", headers={"Authorization": "Bearer wrong"})
check("wrong-secret pull rejected", r.status_code == 401)

mixed = {"node_id": "node-x", "records": [
    {"id": "bad-1"},  # missing required keys
    {"id": "good-1", "content": "good", "created_at": now, "updated_at": now},
]}
r = httpx.post(f"{HUB_URL}/sync/push", json=mixed,
               headers={"Authorization": f"Bearer {SECRET}"})
body = r.json()
check("malformed record isolated, good record applied",
      body["failed"] == 1 and body["accepted"] == 1
      and body["processed_ids"] == ["good-1"], str(body))

print("\nALL CHECKS PASSED")
