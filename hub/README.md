# Remind Me Sync Hub

The central sync point for [distributed sync](../README.md#multi-machine-sync):
a small FastAPI server backed by Postgres, deployed with rootless Podman +
Quadlets on a plain Fedora server. Clients reach it through an SSH tunnel —
nothing is exposed beyond `127.0.0.1:8765` on the server.

```
client (sync.py) ──ssh -L 8765──► server 127.0.0.1:8765 (hub) ──► postgres
```

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
    --apply-code
```

It installs the package (`.venv`), sets up a persistent SSH tunnel (dedicated
key + `~/.ssh/config` block + systemd user service), checks hub connectivity,
and prints ready-to-paste MCP config for **Claude Code** and **Claude
Desktop** — with `--apply-code`, the Claude Code entry is merged into
`~/.claude.json` for you (timestamped backup written first). Drop `--tunnel`
on machines that reach the hub another way (e.g. Tailscale) and pass
`--hub-url` instead. See `--help` for all options.

Day-2 commands:

```bash
~/remind_me/hub/setup.sh status    # services, health, per-node memory counts
~/remind_me/hub/setup.sh update    # git pull, rebuild image, restart hub
```

## Protocol

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
| `setup.sh` | Server installer: `install` / `restore` / `status` / `update` |
| `client-setup.sh` | Client configurator: venv, SSH tunnel, MCP config |
| `e2e_test.py` | End-to-end test driving the hub with the real client |
| `deploy/remind-me.network` | Quadlet network (container-name DNS, no static IPs) |
| `deploy/remind-me-postgres.container` | Quadlet unit for Postgres |
| `deploy/remind-me-hub.container` | Quadlet unit for the hub |
| `deploy/postgres.env.example` | Postgres credentials template |
| `deploy/hub.env.example` | Hub `DATABASE_URL` + `SYNC_SECRET` template |

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
# {"status":"ok","role":"hub","db":"ok","time":"..."}
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
