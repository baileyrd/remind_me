# Remind Me Sync Hub

The central sync point for [distributed sync](../README.md#multi-machine-sync):
a small FastAPI server backed by Postgres, deployed with rootless Podman +
Quadlets on a plain Fedora server. Clients reach it through an SSH tunnel —
nothing is exposed beyond `127.0.0.1:8765` on the server.

```
client (sync.py) ──ssh -L 8765──► server 127.0.0.1:8765 (hub) ──► postgres
```

The hub implements the same wire protocol as the peer server
(`remind_me_mcp/peer_server.py`): bearer-authenticated `/sync/push` with
`processed_ids` responses, keyset-cursor `/sync/pull`, and the FT-04
entity-graph endpoints `/sync/pull_entities` and `/sync/pull_links`.
`GET /health` is an unauthenticated liveness probe.

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

## Files

| File | Purpose |
|------|---------|
| `main.py` | The hub server (FastAPI + psycopg) |
| `Containerfile` | Hub container image |
| `deploy/remind-me.network` | Quadlet network (container-name DNS, no static IPs) |
| `deploy/remind-me-postgres.container` | Quadlet unit for Postgres |
| `deploy/remind-me-hub.container` | Quadlet unit for the hub |
| `deploy/postgres.env.example` | Postgres credentials template |
| `deploy/hub.env.example` | Hub `DATABASE_URL` + `SYNC_SECRET` template |

## Server Setup (Fedora, rootless Podman)

### 1. Prerequisites

```bash
sudo dnf install -y podman
# Let your user's services run without an active login session
loginctl enable-linger $USER
```

### 2. Get the files onto the server

```bash
git clone https://github.com/baileyrd/remind_me.git ~/remind_me
mkdir -p ~/remind-me-hub/postgres-data ~/.config/containers/systemd
```

### 3. Create the env files (secrets — never committed)

```bash
cp ~/remind_me/hub/deploy/postgres.env.example ~/remind-me-hub/postgres.env
cp ~/remind_me/hub/deploy/hub.env.example      ~/remind-me-hub/hub.env
chmod 600 ~/remind-me-hub/*.env
# Edit both: set the same Postgres password in each, and pick the
# SYNC_SECRET your clients will use as REMIND_ME_SYNC_SECRET.
```

### 4. Install the Quadlet units

```bash
cp ~/remind_me/hub/deploy/remind-me.network \
   ~/remind_me/hub/deploy/remind-me-postgres.container \
   ~/remind_me/hub/deploy/remind-me-hub.container \
   ~/.config/containers/systemd/
```

### 5. Build the hub image and start everything

```bash
podman build -t remind-me-hub:latest ~/remind_me/hub

systemctl --user daemon-reload
systemctl --user start remind-me-postgres.service
systemctl --user start remind-me-hub.service

systemctl --user status remind-me-postgres.service remind-me-hub.service
curl -s http://127.0.0.1:8765/health
# {"status":"ok","role":"hub","db":"ok","time":"..."}
```

The hub creates (or migrates) the database schema itself at startup, and
waits up to two minutes for Postgres to come up first.

## Restoring a Backup

For a plain-SQL dump (`postgres-backup.sql`), restore **before or after**
starting the hub — the hub's startup migration upgrades the legacy schema
(TIMESTAMPTZ columns, pre-entity-graph) automatically, so the order is:

```bash
# 1. Postgres up, hub stopped
systemctl --user start remind-me-postgres.service
systemctl --user stop  remind-me-hub.service

# 2. Load the dump (single transaction, abort on first error)
podman exec -i remind-me-postgres \
  psql -v ON_ERROR_STOP=1 --single-transaction -U remindme -d remindme \
  < ~/postgres-backup.sql

# 3. Start the hub — it converts timestamps to canonical ISO TEXT and adds
#    the columns/tables introduced since the legacy hub
systemctl --user start remind-me-hub.service
journalctl --user -u remind-me-hub.service | grep -i migrat

# 4. Verify
podman exec -it remind-me-postgres psql -U remindme -d remindme \
  -c "SELECT COUNT(*), MAX(updated_at) FROM memories;"
```

If the dump contains `CREATE DATABASE` / `\connect` lines, strip them or
restore with `psql -d postgres` instead — the container already created the
`remindme` database.

## Client Access (SSH tunnel)

Each client machine keeps a forward to the server open and points
`REMIND_ME_HUB_URL` at localhost:

```
# ~/.ssh/config on the client
Host remind-me-hub
    HostName <your-server>
    User <you>
    IdentityFile ~/.ssh/remind-me-tunnel
    IdentitiesOnly yes
    LocalForward 8765 localhost:8765
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

## Operations

```bash
# Logs
journalctl --user -u remind-me-hub.service -f
journalctl --user -u remind-me-postgres.service -f

# Rebuild + redeploy the hub after a code change
podman build -t remind-me-hub:latest ~/remind_me/hub
systemctl --user restart remind-me-hub.service

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
