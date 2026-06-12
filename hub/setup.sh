#!/usr/bin/env bash
# Remind Me sync hub — one-command server setup for Fedora + rootless Podman.
#
# Usage:
#   ./setup.sh install               Full install: secrets, quadlets, image,
#                                    services. Idempotent — never clobbers
#                                    existing secrets or data.
#   ./setup.sh restore <dump.sql>    Restore a Postgres dump (legacy hub dumps
#                                    supported). Add --force to drop a database
#                                    that already holds memories.
#   ./setup.sh status                Service state, hub health, per-node counts.
#   ./setup.sh update                git pull, rebuild the hub image, restart.
#
# Flags:
#   --force      allow restore to drop a non-empty database
#   --dry-run    print mutating commands instead of executing them (install)
#
# Layout it manages:
#   ~/remind-me-hub/postgres.env       Postgres credentials   (chmod 600)
#   ~/remind-me-hub/hub.env            DATABASE_URL + SYNC_SECRET (chmod 600)
#   ~/remind-me-hub/postgres-data/     Postgres data directory (bind mount)
#   ~/.config/containers/systemd/      Quadlet units (postgres, hub, network)

set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$HUB_DIR/.." && pwd)"
DATA_DIR="${REMIND_ME_HUB_DATA:-$HOME/remind-me-hub}"
QUADLET_DIR="$HOME/.config/containers/systemd"
HEALTH_URL="http://127.0.0.1:8765/health"
PG_CONTAINER=remind-me-postgres
HUB_SERVICE=remind-me-hub.service
PG_SERVICE=remind-me-postgres.service

DRY_RUN=0
FORCE=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Execute a mutating command, or print it under --dry-run.
run() {
    if (( DRY_RUN )); then
        printf '+ %s\n' "$*"
    else
        "$@"
    fi
}

rand_hex() {  # rand_hex <bytes>
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$1"
    else
        head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

env_value() {  # env_value <file> <KEY>
    sed -n "s/^$2=//p" "$1" | head -1
}

psql_in() {  # psql_in <db> [psql args...]
    local db="$1"; shift
    podman exec "$PG_CONTAINER" psql -U remindme -d "$db" "$@"
}

wait_for_postgres() {
    log "Waiting for Postgres to accept connections"
    for _ in $(seq 1 60); do
        if podman exec "$PG_CONTAINER" pg_isready -U remindme -d remindme -q 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    die "Postgres did not become ready within 60s — check: journalctl --user -u $PG_SERVICE"
}

wait_for_hub() {
    log "Waiting for the hub to answer $HEALTH_URL"
    for _ in $(seq 1 60); do
        if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    die "hub did not become healthy within 60s — check: journalctl --user -u $HUB_SERVICE"
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

check_prereqs() {
    local missing=()
    for cmd in podman systemctl curl; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if (( ${#missing[@]} )); then
        if (( DRY_RUN )); then
            warn "missing commands (ok for --dry-run): ${missing[*]}"
        else
            die "missing commands: ${missing[*]} — install them first (sudo dnf install podman curl)"
        fi
    fi

    if command -v podman >/dev/null 2>&1; then
        local ver major minor
        ver=$(podman --version | sed -n 's/.*version \([0-9.]*\).*/\1/p')
        major=${ver%%.*}; minor=${ver#*.}; minor=${minor%%.*}
        if (( major < 4 || (major == 4 && minor < 4) )); then
            die "podman $ver is too old for Quadlets (need >= 4.4)"
        fi
    fi

    if ! (( DRY_RUN )) && ! systemctl --user is-system-running >/dev/null 2>&1 \
        && ! systemctl --user list-units >/dev/null 2>&1; then
        die "cannot reach the systemd user manager — log in as the target user (no sudo) and ensure XDG_RUNTIME_DIR=/run/user/\$(id -u)"
    fi
}

ensure_linger() {
    local user
    user="${USER:-$(id -un)}"
    if command -v loginctl >/dev/null 2>&1; then
        if [ "$(loginctl show-user "$user" --property=Linger --value 2>/dev/null)" != "yes" ]; then
            run loginctl enable-linger "$user" \
                || warn "could not enable linger — services will stop when you log out (fix: sudo loginctl enable-linger $user)"
        fi
    fi
}

ensure_env_files() {
    run mkdir -p "$DATA_DIR/postgres-data" "$QUADLET_DIR"

    local pgpw secret
    if [ -f "$DATA_DIR/postgres.env" ]; then
        pgpw=$(env_value "$DATA_DIR/postgres.env" POSTGRES_PASSWORD)
        log "Keeping existing $DATA_DIR/postgres.env"
    else
        pgpw=$(rand_hex 24)
        log "Generating $DATA_DIR/postgres.env"
        if ! (( DRY_RUN )); then
            cat > "$DATA_DIR/postgres.env" <<EOF
POSTGRES_USER=remindme
POSTGRES_PASSWORD=$pgpw
POSTGRES_DB=remindme
EOF
            chmod 600 "$DATA_DIR/postgres.env"
        fi
    fi

    if [ -f "$DATA_DIR/hub.env" ]; then
        log "Keeping existing $DATA_DIR/hub.env"
    else
        secret=$(rand_hex 32)
        log "Generating $DATA_DIR/hub.env"
        if ! (( DRY_RUN )); then
            cat > "$DATA_DIR/hub.env" <<EOF
DATABASE_URL=postgresql://remindme:$pgpw@remind-me-postgres:5432/remindme
SYNC_SECRET=$secret
EOF
            chmod 600 "$DATA_DIR/hub.env"
        fi
    fi
}

install_quadlets() {
    log "Installing Quadlet units into $QUADLET_DIR"
    run cp -f "$HUB_DIR/deploy/remind-me.network" \
              "$HUB_DIR/deploy/remind-me-postgres.container" \
              "$HUB_DIR/deploy/remind-me-hub.container" \
              "$QUADLET_DIR/"
}

build_image() {
    log "Building the hub image"
    run podman build -q -t remind-me-hub:latest "$HUB_DIR"
}

start_services() {
    log "Starting services"
    run systemctl --user daemon-reload
    run systemctl --user start "$PG_SERVICE"
    if ! (( DRY_RUN )); then wait_for_postgres; fi
    run systemctl --user restart "$HUB_SERVICE"
    if ! (( DRY_RUN )); then wait_for_hub; fi
}

cmd_install() {
    check_prereqs
    ensure_linger
    ensure_env_files
    install_quadlets
    build_image
    start_services

    if (( DRY_RUN )); then
        log "Dry run complete — no changes made"
        return
    fi

    local secret
    secret=$(env_value "$DATA_DIR/hub.env" SYNC_SECRET)
    log "Hub is up: $(curl -fsS "$HEALTH_URL")"
    cat <<EOF

Server setup complete.

Sync secret (clients need this as REMIND_ME_SYNC_SECRET):
  $secret

Next steps:
  - restore a backup:    $0 restore /path/to/postgres-backup.sql
  - configure a client:  run hub/client-setup.sh on each client machine
  - check anytime:       $0 status
EOF
}

# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

cmd_restore() {
    local dump="${1:-}"
    [ -n "$dump" ] || die "usage: $0 restore <dump.sql> [--force]"
    [ -f "$dump" ] || die "no such file: $dump"
    (( DRY_RUN )) && die "restore does not support --dry-run"
    [ -f "$DATA_DIR/postgres.env" ] || die "no $DATA_DIR/postgres.env — run '$0 install' first"

    # The hub only migrates at startup, and it must not serve mid-restore.
    log "Stopping the hub"
    systemctl --user stop "$HUB_SERVICE" 2>/dev/null || true
    systemctl --user start "$PG_SERVICE"
    wait_for_postgres

    # If the database already has a memories table (e.g. the hub created the
    # new empty schema, or an earlier restore), drop and recreate it so the
    # dump's own schema loads cleanly and the hub migration sees it as-is.
    local existing
    existing=$(psql_in remindme -tAc "SELECT COUNT(*) FROM memories" 2>/dev/null || echo "")
    if [ -n "$existing" ]; then
        if [ "$existing" -gt 0 ] && ! (( FORCE )); then
            die "database already holds $existing memories — re-run with --force to drop it and restore over it"
        fi
        log "Dropping and recreating the remindme database (held $existing memories)"
        psql_in postgres -q \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='remindme' AND pid <> pg_backend_pid();" \
            >/dev/null
        psql_in postgres -c "DROP DATABASE remindme;" -c "CREATE DATABASE remindme OWNER remindme;" >/dev/null
    fi

    # Load the dump. 'already exists' errors are expected (the container
    # pre-creates the role); anything else is a real problem.
    log "Loading $dump"
    local errors
    errors=$(podman exec -i "$PG_CONTAINER" psql -U remindme -d remindme \
        < "$dump" 2>&1 | grep -E 'ERROR|FATAL' | grep -v 'already exists' || true)
    if [ -n "$errors" ]; then
        printf '%s\n' "$errors" >&2
        die "restore reported unexpected errors — inspect the dump before retrying"
    fi

    # Dumps can carry ALTER ROLE ... PASSWORD, silently resetting the role
    # password to the OLD deployment's value. Set it back to match the env
    # files (the in-container socket is trusted, so this always works).
    log "Resetting the remindme password to match $DATA_DIR/postgres.env"
    local pgpw
    pgpw=$(env_value "$DATA_DIR/postgres.env" POSTGRES_PASSWORD)
    psql_in postgres -q -c "ALTER USER remindme WITH PASSWORD '$pgpw';"

    # RESTART (not start): the schema migration only runs at hub startup.
    log "Restarting the hub (runs the legacy-schema migration)"
    systemctl --user restart "$HUB_SERVICE"
    wait_for_hub

    # Verify the migration actually converted the schema.
    local dtype maxts count
    dtype=$(psql_in remindme -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='memories' AND column_name='updated_at';")
    [ "$dtype" = "text" ] || die "memories.updated_at is '$dtype', expected 'text' — migration did not run; check: journalctl --user -u $HUB_SERVICE"
    count=$(psql_in remindme -tAc "SELECT COUNT(*) FROM memories;")
    maxts=$(psql_in remindme -tAc "SELECT COALESCE(MAX(updated_at), '') FROM memories;")
    case "$maxts" in
        *T*+00:00|"") : ;;
        *) die "timestamps are not canonical ISO ('$maxts') — migration incomplete" ;;
    esac

    log "Restore complete: $count memories, latest update $maxts"
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

cmd_status() {
    local svc state
    for svc in "$PG_SERVICE" "$HUB_SERVICE"; do
        state=$(systemctl --user is-active "$svc" 2>/dev/null || true)
        printf '%-32s %s\n' "$svc" "${state:-unknown}"
    done

    if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null; then
        echo
    else
        warn "hub is not answering $HEALTH_URL"
        return 1
    fi

    echo
    psql_in remindme -c \
        "SELECT COALESCE(node_id, '(none)') AS node_id, client, COUNT(*), MAX(updated_at)
         FROM memories GROUP BY 1, 2 ORDER BY 4 DESC NULLS LAST;" \
        2>/dev/null || warn "could not query memory counts"
}

# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

cmd_update() {
    log "Pulling latest changes"
    run git -C "$REPO_DIR" pull --ff-only
    build_image
    log "Restarting the hub"
    run systemctl --user restart "$HUB_SERVICE"
    if ! (( DRY_RUN )); then
        wait_for_hub
        log "Hub updated and healthy"
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    local cmd="" args=()
    while (( $# )); do
        case "$1" in
            --force)   FORCE=1 ;;
            --dry-run) DRY_RUN=1 ;;
            -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            install|restore|status|update)
                cmd="$1" ;;
            *)  args+=("$1") ;;
        esac
        shift
    done

    case "${cmd:-install}" in
        install) cmd_install ;;
        restore) cmd_restore "${args[@]:-}" ;;
        status)  cmd_status ;;
        update)  cmd_update ;;
    esac
}

main "$@"
