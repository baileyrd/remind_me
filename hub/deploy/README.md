# Hub Deploy Templates

Four ways to run the sync hub (`../main.py`, FastAPI + psycopg against
Postgres), all building the same image from `../Containerfile` and reading
the same two env vars (`DATABASE_URL`, `SYNC_SECRET`). Pick one — they are
not meant to be combined.

| Target | Files | Best for |
|---|---|---|
| Podman Quadlets (default, `hub/setup.sh install`) | `remind-me.network`, `remind-me-*.container`, `*.env.example` | A plain Linux server you already administer (systemd, rootless containers) — see the main [`hub/README.md`](../README.md) |
| Docker Compose | `docker-compose.yml` | Any Docker host, or local testing |
| Fly.io | `fly.toml` | A small managed VM without administering a server yourself |
| Railway | `railway.json` | Same, via Railway's dashboard/CLI instead of `flyctl` |

Every option shares the same posture: **the hub is not meant to be exposed
directly to the public internet.** `SYNC_SECRET` is a bearer token, not a
full authorization system — reach the hub over an SSH tunnel, Tailscale, or
(for Fly) the private 6PN network. If you deliberately want public HTTPS
access, put your own TLS termination and rate limiting in front of it; none
of these templates do that for you.

## Docker Compose

```bash
cd hub/deploy
cp hub.env.example hub.env
cp postgres.env.example postgres.env
# edit both — POSTGRES_PASSWORD (postgres.env) must match the password
# embedded in DATABASE_URL (hub.env); SYNC_SECRET is whatever your clients
# will use as REMIND_ME_SYNC_SECRET.
docker compose up -d
curl http://127.0.0.1:8765/health
```

## Fly.io

Postgres is a separate managed Fly Postgres app, attached to the hub app so
Fly injects `DATABASE_URL` as a secret automatically.

```bash
cd hub
fly postgres create --name remind-me-db
fly apps create remind-me-hub
fly postgres attach remind-me-db -a remind-me-hub
fly secrets set SYNC_SECRET=$(openssl rand -hex 32) -a remind-me-hub
fly deploy --config deploy/fly.toml --dockerfile Containerfile
```

No public HTTP service is declared in `fly.toml` — reach the hub at
`remind-me-hub.internal:8765` over a WireGuard peer (`fly wireguard create`)
or a Tailscale-to-Fly bridge.

## Railway

Railway needs two independent service settings, since `railway.json` lives
alongside the other deploy templates rather than at the repo/hub root:

1. **Root Directory:** `hub` — this is the Docker build context, and must
   match where `Containerfile` expects `main.py` (`COPY main.py .`, no path
   prefix).
2. **Config File Path:** `hub/deploy/railway.json` — set under the service's
   config-as-code setting so Railway finds this file despite the different
   root directory.

Add a Postgres plugin to the project and reference it in the hub service's
variables (`DATABASE_URL = ${{Postgres.DATABASE_URL}}`); set `SYNC_SECRET`
manually. Railway's public URL for the service should stay **disabled** —
this deploy target still expects you to reach the hub over Railway's private
networking or your own tunnel, not a public Railway domain.
