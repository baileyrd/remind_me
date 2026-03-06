"""
remind_me_mcp.config — Module-level constants and environment configuration.

All configuration is read from environment variables at import time, with
sensible defaults. No magic globals; every constant is exported via __all__.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Directory / file paths
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(os.environ.get("REMIND_ME_MCP_DIR", "~/.remind-me")).expanduser()
DB_PATH = MEMORY_DIR / "memory.db"
IMPORT_LOG = MEMORY_DIR / "import_log.json"
PID_FILE = MEMORY_DIR / "server.pid"

# Ensure the memory directory exists on import
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = os.environ.get(
    "REMIND_ME_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension
MODEL_DIR = MEMORY_DIR / "models"

# ---------------------------------------------------------------------------
# UI / dashboard
# ---------------------------------------------------------------------------

SERVE_UI = os.environ.get("REMIND_ME_MCP_SERVE_UI", "").lower() in ("true", "1", "yes")
UI_PORT = int(os.environ.get("REMIND_ME_MCP_UI_PORT", "5199"))

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

API_KEY: str | None = os.environ.get("REMIND_ME_API_KEY") or None
"""Bearer token for /api/* routes. None when unset — auth disabled (backward-compatible)."""

_import_roots_env: str | None = os.environ.get("REMIND_ME_IMPORT_ROOTS")
IMPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _import_roots_env.split(":") if r.strip()]
    if _import_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for import operations. Colon-separated paths. Default: user home directory."""

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout reserved for MCP stdio transport)
# ---------------------------------------------------------------------------

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("remind_me_mcp.config")

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "MEMORY_DIR",
    "DB_PATH",
    "IMPORT_LOG",
    "PID_FILE",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "MODEL_DIR",
    "SERVE_UI",
    "UI_PORT",
    "API_KEY",
    "IMPORT_ROOTS",
]

# ---------------------------------------------------------------------------
# Sync configuration
# ---------------------------------------------------------------------------
NODE_ID = os.environ.get("REMIND_ME_NODE_ID", "")
CLIENT: str = os.getenv("REMIND_ME_CLIENT", "unknown")
HUB_URL = os.environ.get("REMIND_ME_HUB_URL", "")
SYNC_SECRET = os.environ.get("REMIND_ME_SYNC_SECRET", "")
SYNC_INTERVAL = int(os.environ.get("REMIND_ME_SYNC_INTERVAL", "60"))
PEER_PORT = int(os.environ.get("REMIND_ME_PEER_PORT", "8766"))
SYNC_ENABLED = bool(NODE_ID and HUB_URL and SYNC_SECRET)
STATIC_PEERS: list[dict] = json.loads(
    os.environ.get("REMIND_ME_STATIC_PEERS", "[]")
)
TAILSCALE_SOCKET = os.environ.get("REMIND_ME_TAILSCALE_SOCKET", "")
