# Remind Me Sync Hub

The central sync point for [distributed sync](../README.md#multi-machine-sync):
a small FastAPI server backed by Postgres, deployed with rootless Podman +
Quadlets on a plain Fedora server. Clients reach it through an SSH tunnel —
nothing is exposed beyond `127.0.0.1:8765` on the server.

```
client (sync.py) ──ssh -L 8765──► server 127.0.0.1:8765 (hub) ──► postgres
```

Prefer Docker Compose, Fly.io, or Railway instead of Podman quadlets? See
[`deploy/README.md`](deploy/README.md) — same image, same env contract.

## Quick Start

**On the server** (Fedora with Podman ≥ 4.4):

```bash
git clone https://github.com/baileyrd/remind_me.git ~/remind_me
~/remind_me/hub/setup.sh install
~/remind_me/hub/setup.sh restore /path/to/postgres-backup.sql   # optional
```

`install` is idempotent: it generates secrets (kept on re-runs), installs the
Quadlet units, builds the hub image, starts everything, and prints the
`SYNC_SECRET` your clients need. `restore` encodes the full field-tested
procedure — drop/recreate when needed (`--force` for a non-empty database),
tolerant dump loading, the post-restore password reset, the hub restart that
triggers the legacy-schema migration, and verification that the migration
actually ran.

**On each client** (inside Fedora/WSL):

```bash
git clone https://github.com/baileyrd/remind_me.git ~/projects/remind_me
~/projects/remind_me/hub/client-setup.sh \
    --node-id work-pc-wsl \
    --tunnel you@your-server:22 \
    --apply-code --apply-instructions
```

It installs the package (`.venv`), sets up a persistent SSH tunnel (dedicated
key + `~/.ssh/config` block + systemd user service), checks hub connectivity,
and prints ready-to-paste MCP config for **Claude Code** and **Claude
Desktop** — with `--apply-code`, the Claude Code entry is merged into
`~/.claude.json` for you (timestamped backup written first). Drop `--tunnel`
on machines that reach the hub another way (e.g. Tailscale) and pass
`--hub-url` instead. See `--help` for all options.

`--apply-instructions` also teaches Claude Code *how to use* the memory: it
installs a block into `~/.claude/CLAUDE.md` telling Claude to search
remind-me before answering questions about you or your projects, to
auto-capture every substantive conversation, and to save durable facts and
preferences as they come up. The same instructions are printed for pasting
into Claude Desktop / claude.ai personal preferences (those settings are
account-side, so a local script cannot write them).

Day-2 commands:

```bash
~/remind_me/hub/setup.sh status    # services, health, per-node memory counts
~/remind_me/hub/setup.sh update    # git pull, rebuild image, restart hub
```

## Security posture

- **Transport:** clients reach the hub over an SSH tunnel (or Tailscale, if
  `--tunnel` is dropped in favor of `--hub-url`) to `127.0.0.1:8765` on the
  server — nothing is exposed on a public interface. Every hub route except
  `GET /health` requires the bearer `SYNC_SECRET` clients configure at setup.
- **Encryption at rest:** the hub's Postgres database holds plaintext
  records (the same records a client would hold plaintext locally without
  the client-side `REMIND_ME_DB_ENCRYPTION_KEY` opt-in — see
  [ARCHITECTURE.md](../ARCHITECTURE.md#encryption-at-rest-is-opt-in-not-default-issue-184)).
  This codebase does not implement any hub-side encryption today. If you
  need at-rest protection for the hub's data, it's a deployment/
  infrastructure decision, not something to configure here — e.g. your
  cloud provider's disk encryption, or Postgres's `pgcrypto`/enterprise TDE
  extensions if you're running a Postgres distribution that offers one.
  This is stated here as the honest current posture, not a roadmap
  commitment.

## Protocol

The hub implements the same wire protocol as the peer server
(`remind_me_mcp/peer_server.py`): bearer-authenticated `/sync/push` with
`processed_ids` responses, keyset-cursor `/sync/pull`, and the FT-04
entity-graph endpoints `/sync/pull_entities` and `/sync/pull_links`.
`GET /health` is an unauthenticated liveness probe — 200 when Postgres is
reachable, 503 otherwise, so deploy-time healthchecks (Railway, Docker
Compose's `depends_on: condition: service_healthy`) correctly catch a
broken DB connection instead of always reporting success.

Two deliberate divergences from the peer protocol, both required because the
hub is pull-only (peers push to each other; nobody pushes hub state to you):

- **`exclude_node` filters on the pushing node, not the record's `node_id`.**
  Clients never rewrite `node_id` on update, so the peer-style filter makes a
  record's creator deaf to every later edit other nodes make to it. The hub
  tracks who pushed each record in a hub-only `origin_node` column and
  filters on that. The wire format is unchanged.
- **LWW-losing alias merges bump `updated_at`.** When an entity record loses
  last-write-wins but contributes new aliases, the merged result must still
  reach nodes whose pull cursor already passed that entity. Union-merge is
  idempotent, so the bump converges instead of churning.

### Pull cursor: `hub_seq` vs. client-authored `updated_at`

`/sync/pull` supports three cursor modes, checked in this order:

1. **`since_seq`** (opt-in) — a keyset cursor on `hub_seq`, a hub-assigned
   `BIGINT` bumped on every insert *and* every accepted update, independent
   of the pushed record's own `updated_at`. This is the fix for a real
   failure mode: a node offline for two weeks pushes records still stamped
   with two-week-old timestamps — *behind* every other node's already-advanced
   `updated_at` cursor — and a `updated_at`-ordered pull never surfaces them
   again. `updated_at` still drives LWW conflict resolution; `hub_seq` only
   changes what the *pull cursor* orders on, so the two concerns don't get
   conflated the way they used to be.
2. **`since_id` set, no `since_seq`** — legacy `(updated_at, id)` keyset
   cursor.
3. **neither set** — legacy strict `updated_at > since` comparison.

This is purely additive: a client that never sends `since_seq` gets exactly
the old behavior (and the old bug), so existing deployments and the peer
protocol (SQLite peers have no `hub_seq` concept) are unaffected. Every pull
response includes `hub_seq` per record regardless of which mode served the
request — an old client simply ignores the field it doesn't recognize.

**Known gap:** `remind_me_mcp/sync.py` (the client) does not send
`since_seq` yet — the hub-side fix landed first, deliberately, since it
could be written and statically checked without a live Postgres to test
against, while updating the live production sync path needs its own
careful review. Track this at the client end before assuming the
late-push problem is fully closed.

### Full re-seed (`full=1`)

`/sync/pull` and `/sync/pull_entities` accept `full=true`, which drops the
`exclude_node` filter entirely regardless of whether `exclude_node` was also
passed. Without it, a node that loses its local database and gets
re-provisioned with the *same* `node_id` can only ever pull records some
*other* node touched — everything it originally authored and pushed stays on
the hub, permanently excluded by its own `exclude_node`. Point a client with
an empty local `memories` table at `full=1` to recover its full history back
from the hub.

### Observability: `/health`, `/count`, `/stats`

Three read-only routes, in increasing cost. Pick the cheapest one that
answers your question — they are not interchangeable.

| Route | Auth | Cost | Answers |
|-------|------|------|---------|
| `GET /health` | none | no query beyond `SELECT 1` | is the hub up, can it reach Postgres, **which version is deployed** |
| `GET /count` | bearer | one `COUNT(*)` per table | how many records are there |
| `GET /stats` | bearer | two `GROUP BY` passes + `MIN`/`MAX` | how do the counts break down by node and category |

```bash
curl -s http://127.0.0.1:8765/health
# {"status":"ok","role":"hub","version":"1.1.0","db":"ok","time":"..."}

curl -s -H "Authorization: Bearer $SYNC_SECRET" http://127.0.0.1:8765/count
# {"role":"hub","version":"1.1.0",
#  "memories":{"total":812,"live":790,"tombstones":22},
#  "entities":143,"memory_entities":901,"entity_relations":37,"time":"..."}

# Narrow to one table when a poller only watches one:
curl -s -H "Authorization: Bearer $SYNC_SECRET" \
     'http://127.0.0.1:8765/count?table=memories'
```

`/count` is the one to poll — a dashboard tile, a cron drift alarm, a check
right after a bulk import. `/stats` exists for reconciliation (it is what
`remind_me_sync_reconcile` reads) and pays for its breakdowns with full
table scans, which is the right cost once per reconcile and the wrong cost
once per minute. `table=` accepts `memories`, `entities`, `memory_entities`
or `entity_relations`; anything else is a `400`.

`memories.live` is `total - tombstones`, and it is the number that should
agree with a node — a node's user-visible count excludes tombstones while
the hub retains them until `/admin/compact_tombstones` runs, so comparing
raw totals across the two looks like permanent drift.

Both counting routes are bearer-authenticated: totals and category names
leak how much is stored and how fast it grows. `/health` stays public
because deploy healthchecks need it, and it stays free of counts.

**Versioning.** `HUB_VERSION` in `main.py` is a literal string, bumped by
hand — the container image contains `main.py` and nothing else (no
`pyproject.toml`, no git checkout), so there is nothing to derive it from at
runtime. It is versioned independently of the `remind-me-mcp` package, whose
version tracks client releases the hub never participates in. Bump MAJOR for
a wire-protocol break, MINOR for a new endpoint or response field, PATCH for
a fix nothing can key off. Clients should still probe for a `404` to detect
a capability (as `sync.reconcile_with_hub` does for `/stats`) rather than
compare version strings — this is a diagnostic, not feature negotiation.

### Hardening

- **FastAPI's interactive docs are disabled.** `/docs`, `/redoc` and
  `/openapi.json` are ON and unauthenticated by default, and publish every
  route — including `POST /admin/compact_tombstones`, which hard-deletes
  rows — with full schemas to anyone who can reach the port. The app passes
  `docs_url=None, redoc_url=None, openapi_url=None`; the wire protocol is
  documented above instead. FastAPI's own `version=` is wired to
  `HUB_VERSION` so nothing reports the framework's `0.1.0` placeholder.
- **Auth comparison is byte-safe.** `_require_auth` compares UTF-8-encoded
  bytes, not `str` — `hmac.compare_digest` raises `TypeError` on a non-ASCII
  `str`, which a crafted `Authorization` header could trigger to get an
  unhandled 500 (and a log-spam vector) instead of a clean 401.
- **`/health` never echoes the raw exception.** A Postgres connection
  failure logs the full exception server-side but returns a fixed
  `"unreachable"` string publicly — `OperationalError` text routinely embeds
  host, resolved IP, port, database name, and username, and `/health` is
  deliberately unauthenticated.
- **Connect/statement timeouts.** Every request opens its own connection
  with `connect_timeout=5` and a `statement_timeout` (`REMIND_ME_HUB_STATEMENT_TIMEOUT_MS`,
  default 15000ms), so a Postgres host that *hangs* rather than cleanly
  refusing can't block requests for the OS TCP timeout (minutes) and exhaust
  the request threadpool — including `/health`, which is meant to survive
  exactly this.
- **`/sync/push` rejects oversized batches.** Batches over `MAX_PUSH_BATCH`
  (1000 records) get a `413` instead of being fully materialized into memory
  — protection against a client bug that keeps retrying a growing backlog,
  not just malice.
- **`POST /admin/compact_tombstones`** (bearer-authenticated) hard-deletes
  memories tombstoned longer than `REMIND_ME_HUB_TOMBSTONE_RETENTION_DAYS`
  (default 90) ago, mirroring the client-side `sync._compact_tombstones`.
  Purely time-based, same tradeoff as the client: no per-node cursor
  acknowledgment tracking, so a node offline longer than the retention
  window can miss a delete. Operator-triggered (e.g. cron), not automatic —
  the hub has no existing periodic-task loop to hang this off of.

## Files

| File | Purpose |
|------|---------|
| `main.py` | The hub server (FastAPI + psycopg) |
| `Containerfile` | Hub container image |
| `setup.sh` | Server installer: `install` / `restore` / `status` / `update` |
| `client-setup.sh` | Client configurator: venv, SSH tunnel, MCP config |
| `e2e_test.py` | End-to-end test driving the hub with the real client |
| `deploy/remind-me.network` | Quadlet network (container-name DNS, no static IPs) |
| `deploy/remind-me-postgres.container` | Quadlet unit for Postgres |
| `deploy/remind-me-hub.container` | Quadlet unit for the hub |
| `deploy/postgres.env.example` | Postgres credentials template |
| `deploy/hub.env.example` | Hub `DATABASE_URL` + `SYNC_SECRET` template |
| `deploy/docker-compose.yml`, `fly.toml`, `railway.json` | Alternative deploy targets — same image, same env contract; see [`deploy/README.md`](deploy/README.md) |

Layout the installer manages on the server:

```
~/remind-me-hub/postgres.env       Postgres credentials        (chmod 600)
~/remind-me-hub/hub.env            DATABASE_URL + SYNC_SECRET  (chmod 600)
~/remind-me-hub/postgres-data/     Postgres data (bind mount)
~/.config/containers/systemd/      Quadlet units
```

## Manual Setup (reference)

Everything `setup.sh` and `client-setup.sh` do, spelled out — useful for
debugging or non-standard environments.

<details>
<summary>Server: install by hand</summary>

```bash
sudo dnf install -y podman
loginctl enable-linger $USER

git clone https://github.com/baileyrd/remind_me.git ~/remind_me
mkdir -p ~/remind-me-hub/postgres-data ~/.config/containers/systemd

# Env files (secrets — never committed). The Postgres password lives in
# BOTH files and must match; hex secrets inline safely into bash -c strings.
cp ~/remind_me/hub/deploy/postgres.env.example ~/remind-me-hub/postgres.env
cp ~/remind_me/hub/deploy/hub.env.example      ~/remind-me-hub/hub.env
chmod 600 ~/remind-me-hub/*.env
PGPW=$(openssl rand -hex 24)
SECRET=$(openssl rand -hex 32)
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$PGPW|" ~/remind-me-hub/postgres.env
sed -i "s|change-me@|$PGPW@|"                             ~/remind-me-hub/hub.env
sed -i "s|^SYNC_SECRET=.*|SYNC_SECRET=$SECRET|"           ~/remind-me-hub/hub.env
echo "$SECRET"   # → each client's REMIND_ME_SYNC_SECRET

# Quadlets, image, services
cp ~/remind_me/hub/deploy/remind-me.network \
   ~/remind_me/hub/deploy/remind-me-postgres.container \
   ~/remind_me/hub/deploy/remind-me-hub.container \
   ~/.config/containers/systemd/
podman build -t remind-me-hub:latest ~/remind_me/hub
systemctl --user daemon-reload
systemctl --user start remind-me-postgres.service
systemctl --user start remind-me-hub.service
curl -s http://127.0.0.1:8765/health
# {"status":"ok","role":"hub","version":"1.1.0","db":"ok","time":"..."}
```

The hub creates (or migrates) the database schema itself at startup, and
waits up to two minutes for Postgres to come up first.

</details>

<details>
<summary>Server: restore a backup by hand</summary>

```bash
# 1. Postgres up, hub stopped (the hub only migrates at startup, and it
#    must not serve clients mid-restore)
systemctl --user start remind-me-postgres.service
systemctl --user stop  remind-me-hub.service

# 1b. If the database is not pristine (the hub already created the new empty
#     schema, or an earlier restore went in), drop and recreate it first so
#     the hub's startup migration sees the genuine legacy schema:
podman exec remind-me-postgres psql -U remindme -d postgres \
  -c "DROP DATABASE remindme;" -c "CREATE DATABASE remindme OWNER remindme;"

# 2. Load the dump. Expect (and ignore) "role remindme already exists";
#    pipe stderr through a filter so real errors still surface.
podman exec -i remind-me-postgres \
  psql -U remindme -d remindme \
  < ~/postgres-backup.sql \
  2>&1 | grep -E 'ERROR|FATAL' | grep -v 'already exists'

# 3. The dump may contain ALTER ROLE ... PASSWORD, which silently resets
#    the remindme password to whatever the OLD deployment used. Set it
#    back to match your env files (the in-container socket is trusted,
#    so this works even while password auth is broken):
PGPW=$(grep -oP '^POSTGRES_PASSWORD=\K.*' ~/remind-me-hub/postgres.env)
podman exec remind-me-postgres psql -U remindme -d postgres \
  -c "ALTER USER remindme WITH PASSWORD '$PGPW';"

# 4. RESTART the hub (not start — start is a no-op on a running service).
#    Startup converts timestamps to canonical ISO TEXT and adds the
#    columns/tables introduced since the legacy hub.
systemctl --user restart remind-me-hub.service
journalctl --user -u remind-me-hub.service --since '1 min ago' | grep -i migrat

# 5. Verify — max should be ISO text (2026-...T...+00:00), data_type text
podman exec -it remind-me-postgres psql -U remindme -d remindme \
  -c "SELECT COUNT(*), MAX(updated_at) FROM memories;"
curl -s http://127.0.0.1:8765/health
```

If the dump contains `CREATE DATABASE` / `\connect` lines, strip them or
restore with `psql -d postgres` instead — the container already created the
`remindme` database.

</details>

<details>
<summary>Client: SSH tunnel + MCP config by hand</summary>

Each client machine keeps a forward to the server open and points
`REMIND_ME_HUB_URL` at localhost:

```
# ~/.ssh/config on the client
Host remind-me-hub
    HostName <your-server>
    User <you>
    IdentityFile ~/.ssh/remind-me-tunnel
    IdentitiesOnly yes
    LocalForward 8765 127.0.0.1:8765
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```

Run it as a systemd user service so it survives reboots:

```ini
# ~/.config/systemd/user/remind-me-tunnel.service
[Unit]
Description=Remind Me SSH tunnel to sync hub
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N remind-me-hub
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Use a dedicated passphrase-free key for the tunnel, and `IdentitiesOnly yes`
to avoid "Too many authentication failures". Then in the client's MCP env:

```
REMIND_ME_HUB_URL=http://127.0.0.1:8765
REMIND_ME_SYNC_SECRET=<the SYNC_SECRET from hub.env>
REMIND_ME_NODE_ID=<unique per machine>
```

Claude Code takes these in the `env` block of its `mcpServers` entry
(`~/.claude.json`). Claude Desktop on Windows launching into WSL does NOT
pass the `env` block through `wsl.exe` — inline the variables in the
`bash -c` command string instead (see the main README's WSL section).
`client-setup.sh` prints both forms with your values filled in.

</details>

## Expose to claude.ai (remote connector)

The hub is deliberately localhost-only, but the same always-on box is the
right place to also serve the [claude.ai custom connector](../README.md#claudeai-custom-connector-remote-mcp).
claude.ai connectors are fetched by **Anthropic's servers**, not your
browser, so they need a public HTTPS endpoint that stays up even when your
laptops are asleep.

The connector is **not** the hub and does **not** read Postgres. It is a
normal `remind-me-mcp` process in `--serve-remote` mode serving its own local
SQLite (`~/.remind-me/memory.db`) over Streamable HTTP. To make that SQLite
actually hold your memories, run it as one more **sync node** pointed at the
co-located hub: its lifespan starts the same background sync as every other
node, pulling the full store from the hub on localhost and pushing writes
from claude.ai back out to all your machines.

```
claude.ai ──HTTPS──► Caddy :443 ──► connector node ──sync──► hub :8765 ──► Postgres
(Anthropic)          (public)       127.0.0.1:8768           127.0.0.1     (same box)
                                    --serve-remote,
                                    local SQLite
```

### 1. Install the package on the host

The hub runs in a container; the connector runs on the host, so install the
Python package once (the repo is already cloned at `~/remind_me`):

```bash
cd ~/remind_me
uv tool install -e .                 # entrypoint → ~/.local/bin/remind-me-mcp
# optional semantic search: uv tool install -e ".[semantic]"
```

### 2. Configure it as a sync node + connector

Reuse the hub's `SYNC_SECRET` (from `~/remind-me-hub/hub.env`) and point the
node at the hub on localhost. Keep the values in an env file, mode 0600:

```bash
# ~/remind-me-hub/connector.env   (chmod 600)
REMIND_ME_NODE_ID=server-connector
REMIND_ME_HUB_URL=http://127.0.0.1:8765
REMIND_ME_SYNC_SECRET=<the SYNC_SECRET from hub.env>
REMIND_ME_PEER_BIND=127.0.0.1                        # keep the peer port off the public box
REMIND_ME_REMOTE_ISSUER=https://memory.example.com   # your public origin (enables OAuth)
```

`REMIND_ME_NODE_ID` must be unique across your fleet. Setting
`REMIND_ME_REMOTE_ISSUER` turns on the single-user OAuth 2.1 flow; omit it to
fall back to the secret-path URL. The connector still binds `127.0.0.1:8768`
— Caddy does the public exposing.

### 3. Run it under systemd (user service)

The connector is a host process, so it gets a plain user unit (the Quadlet
units stay for the containerized hub):

```ini
# ~/.config/systemd/user/remind-me-connector.service
[Unit]
Description=Remind Me claude.ai connector (remote MCP, hub sync node)
After=network-online.target remind-me-hub.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/remind-me-hub/connector.env
ExecStart=%h/.local/bin/remind-me-mcp --serve-remote
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now remind-me-connector.service
journalctl --user -u remind-me-connector.service -f   # watch for "Sync started"
```

Linger is already enabled by `setup.sh install`, so the service survives
logout and reboot.

### 4. Front it with HTTPS

Only the connector goes public — the hub stays on localhost. Point a real
domain's A record at the server and let Caddy terminate TLS:

```caddyfile
# /etc/caddy/Caddyfile
memory.example.com {
    reverse_proxy 127.0.0.1:8768
}
```

`REMIND_ME_REMOTE_ISSUER` must match this origin exactly. Any HTTPS front
works — `tailscale funnel 8768` is a zero-config alternative if you'd rather
not open 443. Then on claude.ai go to **Settings → Connectors → Add custom
connector**, enter `https://memory.example.com/mcp`, and approve it with the
owner token (`cat ~/.remind-me/connector_token`). The full connector
reference — OAuth vs. secret-path, revocation, security caveats — is in the
[main README](../README.md#claudeai-custom-connector-remote-mcp).

> **Ports:** the hub (`8765`) and the connector's peer server (`8766`,
> pinned to localhost above) never leave the box; the connector listens on
> `127.0.0.1:8768`. Only Caddy's `443` is public.

## Operations

```bash
# One-stop overview: services, health, per-node counts
~/remind_me/hub/setup.sh status

# Logs
journalctl --user -u remind-me-hub.service -f
journalctl --user -u remind-me-postgres.service -f

# Update after a code change (pull, rebuild, restart)
~/remind_me/hub/setup.sh update

# Backup
podman exec remind-me-postgres pg_dump -U remindme remindme \
  > ~/postgres-backup-$(date +%F).sql

# Poke at the data
podman exec -it remind-me-postgres psql -U remindme -d remindme
```

Useful queries:

```sql
-- Memory count by node and client
SELECT node_id, client, COUNT(*) FROM memories GROUP BY node_id, client;

-- Is sync current?
SELECT MAX(updated_at) FROM memories;

-- Entity graph size
SELECT (SELECT COUNT(*) FROM entities) AS entities,
       (SELECT COUNT(*) FROM memory_entities) AS links;
```
