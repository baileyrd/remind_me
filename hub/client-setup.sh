#!/usr/bin/env bash
# Remind Me — configure a client machine (Fedora / WSL) for hub sync.
#
# Usage:
#   ./client-setup.sh --node-id <id> [options]
#
# Options:
#   --node-id ID        Unique id for this machine (e.g. home-pc-wsl). Required.
#   --secret HEX        SYNC_SECRET from the server's hub.env. Prompted if absent.
#   --hub-url URL       Hub URL as seen from this machine
#                       (default http://127.0.0.1:8765 — the tunnel's local end).
#   --tunnel USER@HOST[:PORT]
#                       Also set up a persistent SSH tunnel to the hub server:
#                       dedicated key, ~/.ssh/config block, systemd user service.
#   --peer-port N       Local peer-sync port (default 8766; use a different
#                       port on each machine that shares a network).
#   --apply-code        Merge the MCP server entry into ~/.claude.json
#                       (Claude Code). A timestamped backup is written first.
#   --apply-instructions
#                       Install the memory-usage instructions (search before
#                       answering, auto-capture conversations) into
#                       ~/.claude/CLAUDE.md so Claude Code follows them.
#                       Idempotent: the marker-delimited block is replaced
#                       on re-runs.
#   --skip-install      Don't create/verify the .venv (e.g. installed via
#                       'uv tool install' instead).
#
# What it does (idempotent):
#   1. ensures the remind_me_mcp package is installed (.venv in the repo root)
#   2. optional --tunnel: keeps an SSH forward to the hub alive across reboots
#   3. verifies the hub answers /health through the chosen URL
#   4. prints ready-to-paste MCP config for Claude Code and Claude Desktop
#      (Claude Desktop on Windows/WSL needs env vars INLINED in the command
#      string — the config's env block does not cross the wsl.exe boundary)
#   5. prints the memory-usage instructions for Claude Desktop / claude.ai
#      (their settings are account-side and cannot be written by a script);
#      with --apply-instructions, installs them for Claude Code locally

set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$HUB_DIR/.." && pwd)"
PYBIN="$REPO_DIR/.venv/bin/python"
SSH_KEY="$HOME/.ssh/remind-me-tunnel"
SSH_HOST_ALIAS=remind-me-hub
TUNNEL_SERVICE=remind-me-tunnel.service

NODE_ID=""
SECRET="${REMIND_ME_SYNC_SECRET:-}"
HUB_URL="http://127.0.0.1:8765"
TUNNEL=""
PEER_PORT=8766
APPLY_CODE=0
APPLY_INSTRUCTIONS=0
SKIP_INSTALL=0

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

while (( $# )); do
    case "$1" in
        --node-id)      NODE_ID="${2:?}"; shift ;;
        --secret)       SECRET="${2:?}"; shift ;;
        --hub-url)      HUB_URL="${2:?}"; shift ;;
        --tunnel)       TUNNEL="${2:?}"; shift ;;
        --peer-port)    PEER_PORT="${2:?}"; shift ;;
        --apply-code)   APPLY_CODE=1 ;;
        --apply-instructions) APPLY_INSTRUCTIONS=1 ;;
        --skip-install) SKIP_INSTALL=1 ;;
        -h|--help)      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ -n "$NODE_ID" ] || die "--node-id is required (e.g. --node-id home-pc-wsl)"
if [ -z "$SECRET" ]; then
    read -rsp "SYNC_SECRET (from the server's ~/remind-me-hub/hub.env): " SECRET
    echo
fi
[ -n "$SECRET" ] || die "a sync secret is required"

# ---------------------------------------------------------------------------
# 1. Package install
# ---------------------------------------------------------------------------

if (( SKIP_INSTALL )); then
    if [ ! -x "$PYBIN" ]; then
        warn "$PYBIN does not exist — the printed config will need a different command path"
    fi
else
    if [ -x "$PYBIN" ] && "$PYBIN" -c "import remind_me_mcp" 2>/dev/null; then
        log "remind_me_mcp already installed in $REPO_DIR/.venv"
    else
        log "Installing remind_me_mcp into $REPO_DIR/.venv"
        if command -v uv >/dev/null 2>&1; then
            (cd "$REPO_DIR" && uv venv -q && uv pip install -q -e .)
        else
            python3 -m venv "$REPO_DIR/.venv"
            "$REPO_DIR/.venv/bin/pip" install -q -e "$REPO_DIR"
        fi
        "$PYBIN" -c "import remind_me_mcp" || die "install failed"
    fi
fi

# ---------------------------------------------------------------------------
# 2. SSH tunnel (optional)
# ---------------------------------------------------------------------------

if [ -n "$TUNNEL" ]; then
    command -v ssh >/dev/null 2>&1 && command -v ssh-keygen >/dev/null 2>&1 \
        || die "--tunnel needs ssh and ssh-keygen (sudo dnf install openssh-clients)"
    TUNNEL_USERHOST="${TUNNEL%%:*}"
    TUNNEL_PORT=22
    case "$TUNNEL" in *:*) TUNNEL_PORT="${TUNNEL##*:}" ;; esac
    TUNNEL_USER="${TUNNEL_USERHOST%%@*}"
    TUNNEL_HOST="${TUNNEL_USERHOST##*@}"
    [ "$TUNNEL_USER" != "$TUNNEL_HOST" ] || die "--tunnel must be USER@HOST[:PORT]"

    if [ ! -f "$SSH_KEY" ]; then
        log "Generating dedicated tunnel key $SSH_KEY"
        ssh-keygen -q -t ed25519 -N "" -C "remind-me-tunnel" -f "$SSH_KEY"
    fi

    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    touch "$HOME/.ssh/config" && chmod 600 "$HOME/.ssh/config"
    if grep -q "^# >>> remind-me-hub tunnel >>>" "$HOME/.ssh/config"; then
        log "Keeping existing $SSH_HOST_ALIAS block in ~/.ssh/config"
    else
        log "Adding $SSH_HOST_ALIAS to ~/.ssh/config"
        cat >> "$HOME/.ssh/config" <<EOF

# >>> remind-me-hub tunnel >>>
Host $SSH_HOST_ALIAS
    HostName $TUNNEL_HOST
    Port $TUNNEL_PORT
    User $TUNNEL_USER
    IdentityFile $SSH_KEY
    IdentitiesOnly yes
    LocalForward 8765 127.0.0.1:8765
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
# <<< remind-me-hub tunnel <<<
EOF
    fi

    log "Installing $TUNNEL_SERVICE"
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/$TUNNEL_SERVICE" <<EOF
[Unit]
Description=Remind Me SSH tunnel to sync hub
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$(command -v ssh) -N $SSH_HOST_ALIAS
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_HOST_ALIAS" true 2>/dev/null; then
        systemctl --user enable --now "$TUNNEL_SERVICE"
        log "Tunnel service running"
    else
        systemctl --user enable "$TUNNEL_SERVICE" 2>/dev/null || true
        warn "cannot authenticate to $TUNNEL_USERHOST yet — authorize the key, then start the tunnel:"
        printf '    ssh-copy-id -i %s.pub -p %s %s\n' "$SSH_KEY" "$TUNNEL_PORT" "$TUNNEL_USERHOST"
        printf '    systemctl --user start %s\n' "$TUNNEL_SERVICE"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Hub connectivity
# ---------------------------------------------------------------------------

if curl -fsS --max-time 3 "$HUB_URL/health" >/dev/null 2>&1; then
    log "Hub reachable at $HUB_URL"
else
    warn "hub is NOT answering $HUB_URL/health yet — sync will retry every cycle once it is (tunnel up? server running?)"
fi

# ---------------------------------------------------------------------------
# 4. MCP config
# ---------------------------------------------------------------------------

MCP_DIR="$HOME/.remind-me"
DISTRO="${WSL_DISTRO_NAME:-}"

CODE_ENTRY=$(cat <<EOF
{
  "type": "stdio",
  "command": "$PYBIN",
  "args": ["-m", "remind_me_mcp"],
  "env": {
    "REMIND_ME_MCP_DIR": "$MCP_DIR",
    "REMIND_ME_NODE_ID": "$NODE_ID",
    "REMIND_ME_CLIENT": "claude-code",
    "REMIND_ME_HUB_URL": "$HUB_URL",
    "REMIND_ME_SYNC_SECRET": "$SECRET",
    "REMIND_ME_PEER_PORT": "$PEER_PORT",
    "REMIND_ME_SYNC_INTERVAL": "60",
    "REMIND_ME_STATIC_PEERS": "[]"
  }
}
EOF
)

INLINE_ENV="REMIND_ME_MCP_DIR=$MCP_DIR REMIND_ME_NODE_ID=$NODE_ID REMIND_ME_CLIENT=claude-desktop REMIND_ME_HUB_URL=$HUB_URL REMIND_ME_SYNC_SECRET=$SECRET REMIND_ME_PEER_PORT=$PEER_PORT REMIND_ME_SYNC_INTERVAL=60 REMIND_ME_STATIC_PEERS=[]"

echo
log "Claude Code — entry for ~/.claude.json under \"mcpServers\" -> \"remind-me\":"
printf '%s\n' "$CODE_ENTRY"

if (( APPLY_CODE )); then
    log "Merging into ~/.claude.json"
    python3 - "$CODE_ENTRY" <<'PYEOF'
import json, os, shutil, sys, time

path = os.path.expanduser("~/.claude.json")
entry = json.loads(sys.argv[1])
cfg = {}
if os.path.exists(path):
    shutil.copy2(path, f"{path}.bak.{int(time.time())}")
    with open(path) as f:
        cfg = json.load(f)
cfg.setdefault("mcpServers", {})["remind-me"] = entry
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"    merged (backup written next to it)")
PYEOF
fi

echo
log "Claude Desktop (Windows) — claude_desktop_config.json (%APPDATA%\\Claude\\):"
cat <<EOF
{
  "mcpServers": {
    "remind-me": {
      "command": "wsl.exe",
      "args": [
        "--distribution", "${DISTRO:-<your-wsl-distro>}",
        "--exec", "bash", "-c",
        "$INLINE_ENV $PYBIN -m remind_me_mcp"
      ]
    }
  }
}
EOF
if [ -z "$DISTRO" ]; then
    warn "not running under WSL — replace <your-wsl-distro> with the distro name (wsl.exe -l on Windows)"
fi

# ---------------------------------------------------------------------------
# 5. Memory-usage instructions (search-first + auto-capture)
# ---------------------------------------------------------------------------

INSTRUCTIONS=$(cat <<'EOF'
## Persistent memory (remind-me)

You have persistent, cross-machine memory via the remind-me MCP tools.

**Search before answering.** When a question concerns the user, their
projects, preferences, decisions, or anything that may have come up in a
past conversation, call `remind_me_search` FIRST and ground your answer in
what it returns. Use `entity:"Name"`, `subject:`, or `predicate:` filters
when a specific person, project, or tool is named.

**Capture conversations.** At the end of every substantive conversation,
call `remind_me_auto_capture` to save the full dialog plus a concise
summary covering: topics discussed, decisions made, facts learned,
preferences expressed, and action items. Use a descriptive title and
relevant tags. Do this automatically, without being asked.

**Save durable facts immediately.** When the user states a lasting fact or
preference mid-conversation, store it right away with `remind_me_add`
(category `preference` or `fact`, with tags) instead of waiting for the
end of the conversation.
EOF
)

echo
if (( APPLY_INSTRUCTIONS )); then
    log "Installing memory-usage instructions into ~/.claude/CLAUDE.md (Claude Code)"
    mkdir -p "$HOME/.claude"
    INSTRUCTIONS="$INSTRUCTIONS" python3 - <<'PYEOF'
import os, re

path = os.path.expanduser("~/.claude/CLAUDE.md")
begin = "<!-- >>> remind-me memory instructions >>> -->"
end = "<!-- <<< remind-me memory instructions <<< -->"
block = f"{begin}\n{os.environ['INSTRUCTIONS']}\n{end}\n"

text = ""
if os.path.exists(path):
    with open(path) as f:
        text = f.read()

pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
if pattern.search(text):
    text = pattern.sub(block, text)
    print("    replaced existing block")
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += ("\n" if text else "") + block
    print(f"    added to {path}")
with open(path, "w") as f:
    f.write(text)
PYEOF
else
    log "Claude Code — to make Claude search memory and capture conversations,"
    echo "    re-run with --apply-instructions (writes ~/.claude/CLAUDE.md), or add the block below yourself."
fi

echo
log "Claude Desktop / claude.ai — paste this into Settings -> Profile ->"
echo "    'personal preferences' (it cannot be set by a script):"
echo
printf '%s\n' "$INSTRUCTIONS"

echo
log "Done. Restart Claude Code / Claude Desktop, then verify on the server:"
echo "    ./setup.sh status     # this node should appear within ~60s of its first write"
