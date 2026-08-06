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

It also covers the hub's own read surfaces (/count and its filters, /stats
agreement, /metrics, the X-Hub-Version header), which the pytest suite can
only check statically — no CI leg can import hub/main.py at all, since
fastapi and psycopg are deliberately not this package's dependencies.

CI runs this on every push and pull request (the `hub-e2e` job in
.github/workflows/ci.yml, against a postgres:16 service container). Run it
locally the same way:

    # 1. Postgres with a remindme/remindme database available
    # 2. Hub:  DATABASE_URL=... SYNC_SECRET=test-secret uvicorn main:app --port 8765
    # 3. Test deps: pip install -e ../  psycopg[binary] httpx
    HUB_TEST_DSN=postgresql://remindme:...@host:5432/remindme python e2e_test.py

The test writes its node databases to /tmp/node-a and /tmp/node-b and refuses
to start if they already exist (E2E_WIPE=1 clears them instead) — node B's
whole job is converging from empty. It inserts records into the hub's
database, so run it against a hub whose database you can throw away, never
production.
"""
import asyncio
import os
import shutil
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


def require_clean_node_dirs(*paths):
    """Refuse to run against leftover node databases.

    Node B's whole job here is converging *from empty*, and node A's outbox
    assertions count rows. A stale /tmp/node-* from an earlier run turns both
    into a different test that happens to pass or fail for unrelated reasons.
    Set E2E_WIPE=1 to clear them instead of aborting (what CI does — a fresh
    runner should be clean anyway, so a failure there means something else
    is wrong and is worth hearing about).
    """
    stale = [p for p in paths if os.path.exists(p)]
    if not stale:
        return
    if os.environ.get("E2E_WIPE") == "1":
        for path in stale:
            shutil.rmtree(path)
        print(f"[INFO] wiped stale node databases: {', '.join(stale)}")
        return
    print(
        f"[FAIL] stale node databases present: {', '.join(stale)}\n"
        "       remove them (or set E2E_WIPE=1) — node B must start empty."
    )
    sys.exit(1)


require_clean_node_dirs("/tmp/node-a", "/tmp/node-b")


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
    # Looked up by name rather than asserting a total: this node also creates
    # the "remind_me" project entity as the object of the entity_relation
    # below, so a count of 1 has been unsatisfiable since relations were added.
    ent = conn.execute(
        "SELECT name, kind, aliases FROM entities WHERE name = 'Bailey Robertson'"
    ).fetchall()
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
ents = db.execute(
    "SELECT name, kind, aliases FROM entities WHERE name = 'Bailey Robertson'"
).fetchall()
check("node B pulled entity", len(ents) == 1 and ents[0]["kind"] == "person",
      str([dict(r) for r in ents]))
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

# ---------------- Version + /count ----------------
# The pytest suite can only check these statically (it can't import hub/main.py
# at all — no fastapi/psycopg), so this is where the responses are read for
# real. The interesting part is agreement: /count must not disagree with
# /stats about the same records, which is the failure a separate cheap query
# path invites.
AUTH = {"Authorization": f"Bearer {SECRET}"}

health = httpx.get(f"{HUB_URL}/health").json()
check("health reports the hub version without auth",
      bool(health.get("version")), str(health))

counts = httpx.get(f"{HUB_URL}/count", headers=AUTH)
check("count requires auth", httpx.get(f"{HUB_URL}/count").status_code == 401)
check("count reports the hub version",
      counts.json().get("version") == health["version"], str(counts.json()))

stats = httpx.get(f"{HUB_URL}/stats", headers=AUTH).json()
c = counts.json()
check("count agrees with stats on totals",
      c["memories"]["total"] == stats["memories"]["total"]
      and c["memories"]["tombstones"] == stats["memories"]["tombstones"]
      and c["entities"] == stats["entities"]
      and c["memory_entities"] == stats["memory_entities"]
      and c["entity_relations"] == stats["entity_relations"],
      f"count={c} stats={stats}")
check("count splits live from tombstoned",
      c["memories"]["live"] == c["memories"]["total"] - c["memories"]["tombstones"],
      str(c["memories"]))

approx = httpx.get(f"{HUB_URL}/count?approx=1", headers=AUTH).json()
check("approx mode declares itself", approx["approximate"] is True, str(approx))
check("exact mode declares itself", c["approximate"] is False, str(c))
# No live/tombstone split in approx mode: that needs a filtered scan, which is
# the cost being avoided, and estimating it would be inventing a number.
check("approx memories reports total only",
      set(approx["memories"]) == {"total"}, str(approx["memories"]))

one = httpx.get(f"{HUB_URL}/count?table=memories", headers=AUTH).json()
check("count?table= narrows to one table",
      "memories" in one and "entities" not in one, str(one))
bad = httpx.get(f"{HUB_URL}/count?table=nope", headers=AUTH)
check("count rejects an unknown table", bad.status_code == 400, str(bad.text))

# Approximate mode reads planner statistics, so it is only meaningful once the
# table has been analysed — a freshly-written table reports 0, which is the
# documented caveat rather than a bug. ANALYZE first, then compare.
with psycopg.connect(DSN) as conn:
    conn.execute("ANALYZE memories")
    conn.commit()
analysed = httpx.get(f"{HUB_URL}/count?approx=1&table=memories", headers=AUTH).json()
check("approx tracks exact once the table is analysed",
      analysed["memories"]["total"] == c["memories"]["total"],
      f'approx={analysed["memories"]} exact={c["memories"]}')

# ---------------- /count filters ----------------
epoch = httpx.get(
    f"{HUB_URL}/count?table=memories&since=1970-01-01T00:00:00Z", headers=AUTH
).json()
check("since=epoch counts everything",
      epoch["memories"]["total"] == c["memories"]["total"], str(epoch))
future = httpx.get(
    f"{HUB_URL}/count?table=memories&since=2099-01-01T00:00:00Z", headers=AUTH
).json()
check("since=future counts nothing", future["memories"]["total"] == 0, str(future))
check("since is echoed back canonicalized",
      epoch.get("since", "").endswith("+00:00"), str(epoch.get("since")))
bad_since = httpx.get(f"{HUB_URL}/count?since=yesterday", headers=AUTH)
check("count rejects an unparsable since", bad_since.status_code == 400, bad_since.text)

grouped = httpx.get(f"{HUB_URL}/count?by=origin_node&table=memories", headers=AUTH).json()
check("by=origin_node matches the stats breakdown",
      grouped["by_origin_node"] == stats["memories"]["by_origin_node"],
      f'count={grouped.get("by_origin_node")} stats={stats["memories"]["by_origin_node"]}')
check("count rejects an unknown grouping",
      httpx.get(f"{HUB_URL}/count?by=category", headers=AUTH).status_code == 400)
check("approx refuses filters it cannot honour",
      httpx.get(f"{HUB_URL}/count?approx=1&since=1970-01-01T00:00:00Z",
                headers=AUTH).status_code == 400)

# ---------------- X-Hub-Version header ----------------
# Middleware, so it must be on responses with no JSON body carrying it too --
# that is the whole reason it exists as a header.
for label, resp in (
    ("health", httpx.get(f"{HUB_URL}/health")),
    ("pull", httpx.get(f"{HUB_URL}/sync/pull", headers=AUTH)),
    ("401", httpx.get(f"{HUB_URL}/count")),
    ("404", httpx.get(f"{HUB_URL}/no-such-route")),
):
    check(f"X-Hub-Version present on {label} ({resp.status_code})",
          resp.headers.get("X-Hub-Version") == health["version"],
          str(resp.headers.get("X-Hub-Version")))

# ---------------- /metrics ----------------
# Enabled via REMIND_ME_HUB_METRICS_ENABLED on the hub under test; when it is
# off the route is a 404, which is also a valid state to observe here.
m = httpx.get(f"{HUB_URL}/metrics", headers=AUTH)
if m.status_code == 404:
    print("[SKIP] /metrics — REMIND_ME_HUB_METRICS_ENABLED is not set on this hub")
else:
    check("metrics requires auth",
          httpx.get(f"{HUB_URL}/metrics").status_code == 401)
    check("metrics reports the build",
          f'remind_me_hub_build_info{{version="{health["version"]}"}} 1' in m.text,
          m.text[:200])
    check("metrics agrees with count on live memories",
          f'remind_me_hub_memories{{state="live"}} {c["memories"]["live"]}' in m.text,
          m.text)

# ---------------- since_seq pull cursor, client half (issue #167) ----------------
# #160 gave the hub a monotonic hub_seq and a since_seq cursor; the client was
# only wired to it in #167, and until then the bug had no end-to-end fix. This
# section reproduces the production scenario against the real hub: a node back
# online after a long absence pushes a record still stamped with an OLD
# updated_at, which sorts behind every other node's already-advanced legacy
# cursor and is therefore invisible to them forever.
#
# The issue was deferred for exactly this reason -- "no way to integration-test
# the hub side in this dev environment". That stopped being true once the
# hub-e2e job existed, so the check belongs here rather than in a mock.
import asyncio  # noqa: E402
import tempfile  # noqa: E402

_OLD_TS = "2019-01-01T00:00:00+00:00"

_seq_dir = tempfile.mkdtemp(prefix="e2e-seq-")
_seq_db_mod, _seq_sync, _seq_db = make_node("node-seq", _seq_dir)

# A late push carrying an old timestamp, from a *different* node so this node
# is eligible to pull it back.
httpx.post(
    f"{HUB_URL}/sync/push",
    headers=AUTH,
    json={
        "node_id": "node-late",
        "records": [{
            "id": "late-push-167",
            "content": "pushed late, stamped old",
            "created_at": _OLD_TS,
            "updated_at": _OLD_TS,
        }],
    },
    timeout=10,
).raise_for_status()

# Wedge this node's legacy cursor past that timestamp -- the stuck state.
_seq_db.execute(
    """INSERT INTO sync_log (remote_id, last_pull, last_pull_id, last_pull_seq)
       VALUES ('hub', ?, 'zzz', ?)
       ON CONFLICT (remote_id) DO UPDATE SET
           last_pull = excluded.last_pull,
           last_pull_id = excluded.last_pull_id,
           last_pull_seq = excluded.last_pull_seq""",
    ("2030-01-01T00:00:00+00:00", _seq_sync.SEQ_UNSUPPORTED),
)
_seq_db.commit()


async def _seq_pull():
    async with httpx.AsyncClient() as c:
        return await _seq_sync._pull_remote(c, HUB_URL, "hub")


# Pinned to SEQ_UNSUPPORTED this is precisely the pre-#167 client, so this
# assertion is what makes the one below meaningful rather than vacuous.
asyncio.run(_seq_pull())
_ids = [r["id"] for r in _seq_db.execute("SELECT id FROM memories").fetchall()]
check("legacy cursor cannot see a late push (the bug being fixed)",
      "late-push-167" not in _ids, str(_ids))

# Now let the client establish the seq cursor, as it does on upgrade.
_seq_db.execute(
    "UPDATE sync_log SET last_pull_seq = ? WHERE remote_id = 'hub'",
    (_seq_sync.SEQ_UNKNOWN,),
)
_seq_db.commit()
asyncio.run(_seq_pull())

_ids = [r["id"] for r in _seq_db.execute("SELECT id FROM memories").fetchall()]
check("since_seq cursor rescues it", "late-push-167" in _ids, str(_ids))

_seq_state = _seq_db.execute(
    "SELECT last_pull_seq FROM sync_log WHERE remote_id = 'hub'"
).fetchone()["last_pull_seq"]
check("the seq cursor advanced past the rescued record",
      _seq_state > 0, f"last_pull_seq={_seq_state}")

_seq_db_mod._close_db()
shutil.rmtree(_seq_dir, ignore_errors=True)


# ---------------- Legacy schema migration ----------------
# The migration that turns a restored legacy dump (TIMESTAMPTZ columns) into
# this hub's canonical TEXT form had no test at all until a `.500000` was
# found being written as `.5`. That is not cosmetic: under COLLATE "C" the
# migrated value sorts BEFORE the client's own ('+' is 0x2B, '0' is 0x30), so
# a migrated row compares as *older* than the identical instant on the node
# that wrote it -- corrupting the keyset pull cursor and the LWW comparison
# alike, for any legacy row whose microseconds end in a zero.
#
# _TS_CONVERT is read out of main.py rather than duplicated here, so this
# cannot pass against a stale copy of the expression, and it is executed by
# the real Postgres rather than reasoned about -- the two properties that
# would have caught the original bug.
import ast  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402


def _ts_convert_expression() -> str:
    """The live _TS_CONVERT from hub/main.py.

    Parsed rather than imported: this step runs with psycopg only, and
    importing main.py would need fastapi.
    """
    source = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TS_CONVERT" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("no _TS_CONVERT assignment found in hub/main.py")


_TS_CASES = [
    # (microseconds, why this case is here)
    (0, "no fraction at all"),
    (500000, "trailing zeros -- the case that was broken"),
    (100000, "one significant digit, five trailing zeros"),
    (123456, "no trailing zeros"),
    (1, "leading zeros, which must survive too"),
    (10, "both leading and trailing zeros"),
]

_expr = _ts_convert_expression().format(col="ts")
with psycopg.connect(DSN) as conn:
    for micros, why in _TS_CASES:
        moment = datetime(2026, 8, 4, 9, 0, 0, micros, tzinfo=UTC)
        (migrated,) = conn.execute(
            f"SELECT {_expr} FROM (SELECT %s::timestamptz AS ts) t", (moment,)
        ).fetchone()
        expected = moment.isoformat()
        check(f"migration matches isoformat for {micros:06d}us ({why})",
              migrated == expected, f"got {migrated!r}, want {expected!r}")

    # The property the byte-level format exists to guarantee, stated directly:
    # a migrated legacy row must not sort before the same instant written by a
    # client. This is what the '.5' bug actually broke.
    earlier = datetime(2026, 8, 4, 9, 0, 0, 500000, tzinfo=UTC)
    later = datetime(2026, 8, 4, 9, 0, 1, 0, tzinfo=UTC)
    (m_earlier,) = conn.execute(
        f"SELECT {_expr} FROM (SELECT %s::timestamptz AS ts) t", (earlier,)
    ).fetchone()
    (m_later,) = conn.execute(
        f"SELECT {_expr} FROM (SELECT %s::timestamptz AS ts) t", (later,)
    ).fetchone()
    # Detail is the values themselves, not a failure phrasing: `check` prints
    # it on a pass too, and "PASS ... x != x" reads as a contradiction.
    check("migrated values order chronologically as bytes",
          m_earlier < m_later, f"{m_earlier!r} then {m_later!r}")
    check("a migrated value is byte-identical to the client's own",
          m_earlier == earlier.isoformat(),
          f"migrated {m_earlier!r}, client {earlier.isoformat()!r}")


print("\nALL CHECKS PASSED")
