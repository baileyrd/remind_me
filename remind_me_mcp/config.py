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
EMBEDDING_DIM = int(os.environ.get("REMIND_ME_EMBEDDING_DIM", "384"))
"""Embedding vector dimension. MUST match the chosen model (all-MiniLM-L6-v2=384,
nomic-embed-text=768, bge-m3/mxbai-embed-large=1024). Changing this on an existing
database requires recreating the memories_vec table and running remind_me_reindex."""
MODEL_DIR = MEMORY_DIR / "models"

# Embedding backend selection: "onnx" (default, in-process ONNX Runtime) or
# "ollama" (a local Ollama daemon serving an embedding model).
EMBEDDING_BACKEND = os.environ.get("REMIND_ME_EMBEDDING_BACKEND", "onnx").lower()
OLLAMA_URL = os.environ.get("REMIND_ME_OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("REMIND_ME_OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Sliding-window chunking for embedding. Long content is split into overlapping
# character windows, each embedded as its own vector linked to the parent memory,
# so the whole text is searchable instead of only the first ~256 tokens. Short
# content (<= CHUNK_CHARS) yields a single chunk — identical to the old behavior.
EMBED_CHUNK_CHARS = int(os.environ.get("REMIND_ME_EMBED_CHUNK_CHARS", "1600"))
EMBED_CHUNK_OVERLAP = int(os.environ.get("REMIND_ME_EMBED_CHUNK_OVERLAP", "200"))
EMBED_MAX_CHUNKS = int(os.environ.get("REMIND_ME_EMBED_MAX_CHUNKS", "16"))

# ---------------------------------------------------------------------------
# UI / dashboard
# ---------------------------------------------------------------------------

SERVE_UI = os.environ.get("REMIND_ME_MCP_SERVE_UI", "").lower() in ("true", "1", "yes")
UI_PORT = int(os.environ.get("REMIND_ME_MCP_UI_PORT", "5199"))

# MCP HTTP transport
SERVE_MCP: bool = os.environ.get("REMIND_ME_MCP_SERVE_HTTP", "").lower() in ("true", "1", "yes")
MCP_HTTP_PORT: int = int(os.environ.get("REMIND_ME_MCP_HTTP_PORT", "8767"))
MCP_HTTP_HOST: str = os.environ.get("REMIND_ME_MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_SECRET: str | None = os.environ.get("REMIND_ME_MCP_HTTP_SECRET") or None

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
    "EMBEDDING_BACKEND",
    "OLLAMA_URL",
    "OLLAMA_EMBED_MODEL",
    "EMBED_CHUNK_CHARS",
    "EMBED_CHUNK_OVERLAP",
    "EMBED_MAX_CHUNKS",
    "MODEL_DIR",
    "SERVE_UI",
    "UI_PORT",
    "SERVE_MCP",
    "MCP_HTTP_PORT",
    "MCP_HTTP_HOST",
    "MCP_HTTP_SECRET",
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
PEER_BIND = os.environ.get("REMIND_ME_PEER_BIND", "0.0.0.0")  # noqa: S104
"""Bind address for the peer sync server. Defaults to all interfaces so
Tailscale peers can reach it (their addresses are not known in advance);
set REMIND_ME_PEER_BIND to a specific address (e.g. this node's Tailscale
IP, or 127.0.0.1 to disable remote access) to narrow exposure. Every
request requires the SYNC_SECRET bearer token regardless of bind address."""
OUTBOX_RETENTION_DAYS = int(os.environ.get("REMIND_ME_OUTBOX_RETENTION_DAYS", "30"))
"""Sync outbox rows older than this many days are pruned each sync cycle."""
SYNC_ENABLED = bool(NODE_ID and HUB_URL and SYNC_SECRET)
STATIC_PEERS: list[dict] = json.loads(
    os.environ.get("REMIND_ME_STATIC_PEERS", "[]")
)
TAILSCALE_SOCKET = os.environ.get("REMIND_ME_TAILSCALE_SOCKET", "")
