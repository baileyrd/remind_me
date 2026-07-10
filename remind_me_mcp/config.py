"""
remind_me_mcp.config — Module-level constants and environment configuration.

All configuration is read from environment variables at import time, with
sensible defaults. No magic globals; every constant is exported via __all__.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

# Module logger only — root logging setup (logging.basicConfig) lives in the
# __main__ entrypoint so importing this package never hijacks the host
# application's logging configuration (HY-06).
log = logging.getLogger("remind_me_mcp.config")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default* (HY-06).

    A malformed value (e.g. ``REMIND_ME_UI_PORT=abc``) logs a warning and
    returns the default instead of raising ValueError at import time.

    Args:
        name: The environment variable name.
        default: Value returned when the variable is unset, blank, or invalid.

    Returns:
        The parsed integer or the default.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "Invalid integer for environment variable %s=%r; using default %d",
            name,
            raw,
            default,
        )
        return default

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
# LLM Wiki (FT-08)
# ---------------------------------------------------------------------------

WIKI_DIR = Path(
    os.environ.get("REMIND_ME_WIKI_DIR", str(MEMORY_DIR / "wiki"))
).expanduser()
"""Root of the LLM Wiki (FT-08). Plain markdown files on disk are the source
of truth; the database only indexes them for search. Default: ``wiki`` under
the memory dir. The directory is created lazily on first wiki use."""

WIKI_LOAD_TOKEN_BUDGET = _env_int("REMIND_ME_WIKI_LOAD_TOKEN_BUDGET", 12000)
"""Default ceiling (estimated tokens, len//4) for ``remind_me_wiki_load`` —
the whole-wiki-into-context tool. 0 means unlimited."""

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = os.environ.get(
    "REMIND_ME_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
EMBEDDING_DIM = _env_int("REMIND_ME_EMBEDDING_DIM", 384)
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
EMBED_CHUNK_CHARS = _env_int("REMIND_ME_EMBED_CHUNK_CHARS", 1600)
EMBED_CHUNK_OVERLAP = _env_int("REMIND_ME_EMBED_CHUNK_OVERLAP", 200)
EMBED_MAX_CHUNKS = _env_int("REMIND_ME_EMBED_MAX_CHUNKS", 16)

EMBED_BATCH_SIZE = _env_int("REMIND_ME_EMBED_BATCH_SIZE", 32)
"""Memories embedded per batched _embed_and_store_rows call (reindex and chat
import). Larger batches amortise model overhead; smaller ones bound memory."""

EMBED_FORWARD_BATCH = _env_int("REMIND_ME_EMBED_FORWARD_BATCH", 32)
"""Chunks per ONNX forward pass inside _Embedder.embed(). This is the hard
ceiling on embedding memory: the model materialises a (batch, seq_len, dim)
tensor plus transformer activations, so an unbounded batch (e.g. the initial
bulk hub sync flattening thousands of chunks into one call) can allocate tens
of GB and OOM the process. Callers may pass any number of texts; embed()
processes them in slices of this size and concatenates. Keep it small."""

# ---------------------------------------------------------------------------
# UI / dashboard
# ---------------------------------------------------------------------------

SERVE_UI = os.environ.get("REMIND_ME_MCP_SERVE_UI", "").lower() in ("true", "1", "yes")
UI_PORT = _env_int("REMIND_ME_MCP_UI_PORT", 5199)

# MCP HTTP transport
SERVE_MCP: bool = os.environ.get("REMIND_ME_MCP_SERVE_HTTP", "").lower() in ("true", "1", "yes")
MCP_HTTP_PORT: int = _env_int("REMIND_ME_MCP_HTTP_PORT", 8767)
MCP_HTTP_HOST: str = os.environ.get("REMIND_ME_MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_SECRET: str | None = os.environ.get("REMIND_ME_MCP_HTTP_SECRET") or None

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

API_KEY: str | None = os.environ.get("REMIND_ME_API_KEY") or None
"""Bearer token for /api/* routes, from the REMIND_ME_API_KEY env var.

When unset, a key is auto-generated on first run and persisted under
MEMORY_DIR (see resolve_api_key). The special value ``disabled``
(case-insensitive) turns dashboard auth off for users who explicitly
want an open localhost API."""

API_KEY_FILE = MEMORY_DIR / "api_key"
"""Location of the auto-generated dashboard API key (created with 0600 perms)."""


def resolve_api_key() -> str | None:
    """Return the effective dashboard API key (SE-01).

    Resolution order:
      1. ``REMIND_ME_API_KEY`` env var — always wins when set. The special
         value ``disabled`` (case-insensitive) turns dashboard auth off.
      2. The key persisted at ``MEMORY_DIR/api_key``.
      3. First run: generate a new key, persist it with 0600 permissions,
         and log where it lives.

    If the key file can be neither read nor written, an ephemeral key is
    generated for this process (and logged) so the API never falls open.

    Reads module attributes at call time so tests can monkeypatch
    ``API_KEY`` / ``MEMORY_DIR``.
    """
    if API_KEY is not None:
        if API_KEY.strip().lower() == "disabled":
            log.warning(
                "Dashboard API authentication is DISABLED (REMIND_ME_API_KEY=disabled)"
            )
            return None
        return API_KEY
    key_file = MEMORY_DIR / "api_key"
    try:
        if key_file.is_file():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        key = secrets.token_urlsafe(32)
        key_file.touch(mode=0o600, exist_ok=True)
        key_file.chmod(0o600)
        key_file.write_text(key + "\n", encoding="utf-8")
        log.info(
            "Generated dashboard API key — stored at %s. Clients must send "
            "'Authorization: Bearer <key>'. Set REMIND_ME_API_KEY=disabled to "
            "opt out of dashboard auth.",
            key_file,
        )
        return key
    except OSError as exc:
        key = secrets.token_urlsafe(32)
        log.warning(
            "Could not persist dashboard API key at %s (%s); using an "
            "ephemeral key for this run: %s",
            key_file,
            exc,
            key,
        )
        return key


# ---------------------------------------------------------------------------
# Remote MCP connector (FT-05)
# ---------------------------------------------------------------------------

REMOTE_MCP: bool = os.environ.get("REMIND_ME_REMOTE_MCP", "").lower() in ("true", "1", "yes")
"""Set REMIND_ME_REMOTE_MCP=1 (or pass --serve-remote) to expose the MCP
server as a remote connector: Streamable HTTP under a secret URL path,
suitable for tunnelling (e.g. Tailscale Funnel) and attaching from claude.ai
as a custom connector. Default OFF."""

REMOTE_MCP_HOST: str = os.environ.get("REMIND_ME_REMOTE_HOST", "127.0.0.1")
REMOTE_MCP_PORT: int = _env_int("REMIND_ME_REMOTE_PORT", 8768)

REMOTE_MCP_TOKEN: str | None = os.environ.get("REMIND_ME_REMOTE_TOKEN") or None
"""Connector token for the remote MCP endpoint. When unset, a token is
auto-generated on first use and persisted under MEMORY_DIR (see
resolve_connector_token). Unlike REMIND_ME_API_KEY there is no 'disabled'
opt-out — the token doubles as the secret URL path and the endpoint must
never be open."""

CONNECTOR_TOKEN_FILE = MEMORY_DIR / "connector_token"
"""Location of the auto-generated remote-MCP connector token (0600 perms).
Delete the file to rotate: a fresh token is generated on next startup."""

REMOTE_MCP_ISSUER: str | None = os.environ.get("REMIND_ME_REMOTE_ISSUER") or None
"""Public base URL of the remote connector (FT-07) — the HTTPS tunnel origin,
e.g. ``https://machine.tailnet.ts.net``. Setting it activates the single-user
OAuth 2.1 authorization server on the remote MCP mode (claude.ai discovers it
via the well-known metadata and connects with per-client, revocable tokens).
When unset, the connector falls back to the FT-05 secret-path/bearer mode and
logs a warning. The value must be an origin only (https, no path/query) — it
is deliberately NOT derived from the request Host header, which is
attacker-influenced while DNS-rebinding protection is disabled."""

OAUTH_STATE_FILE = MEMORY_DIR / "oauth.json"
"""Persisted OAuth state (FT-07): registered clients plus SHA-256 hashes of
issued access/refresh tokens (0600 perms). Delete the file to revoke every
client at once; per-client revocation via the remind_me_revoke_clients tool."""


def resolve_connector_token() -> str:
    """Return the effective remote-MCP connector token (FT-05).

    Resolution order mirrors :func:`resolve_api_key` (SE-01):
      1. ``REMIND_ME_REMOTE_TOKEN`` env var — always wins when set.
      2. The token persisted at ``MEMORY_DIR/connector_token``.
      3. First use: generate a new token, persist it with 0600 permissions,
         and log the connector URL path once (the only time the full token
         is logged — later startups log it redacted).

    If the token file can be neither read nor written, an ephemeral token is
    generated for this process (and logged) so the endpoint never falls open.

    Reads module attributes at call time so tests can monkeypatch
    ``REMOTE_MCP_TOKEN`` / ``MEMORY_DIR``.
    """
    if REMOTE_MCP_TOKEN is not None:
        return REMOTE_MCP_TOKEN.strip()
    token_file = MEMORY_DIR / "connector_token"
    try:
        if token_file.is_file():
            existing = token_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(32)
        token_file.touch(mode=0o600, exist_ok=True)
        token_file.chmod(0o600)
        token_file.write_text(token + "\n", encoding="utf-8")
        log.info(
            "Generated remote MCP connector token — stored at %s. Connector "
            "URL path: /mcp/%s (treat the URL like a password; rotate by "
            "deleting the file).",
            token_file,
            token,
        )
        return token
    except OSError as exc:
        token = secrets.token_urlsafe(32)
        log.warning(
            "Could not persist connector token at %s (%s); using an "
            "ephemeral token for this run: %s",
            token_file,
            exc,
            token,
        )
        return token


_import_roots_env: str | None = os.environ.get("REMIND_ME_IMPORT_ROOTS")
IMPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _import_roots_env.split(":") if r.strip()]
    if _import_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for import operations. Colon-separated paths. Default: user home directory."""


def is_in_import_roots(path: Path) -> bool:
    """Return True when the resolved ``path`` is contained in IMPORT_ROOTS (SEC-02).

    Shared containment check used by both the HTTP /api/import route and the
    MCP import tool input models (SE-02). Callers must pass an already
    ``expanduser().resolve()``-ed path. Reads IMPORT_ROOTS at call time so
    tests can monkeypatch it.
    """
    return any(path == root or root in path.parents for root in IMPORT_ROOTS)


_export_roots_env: str | None = os.environ.get("REMIND_ME_EXPORT_ROOTS")
EXPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _export_roots_env.split(":") if r.strip()]
    if _export_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for export destinations. Colon-separated paths. Default: user home directory."""


def is_in_export_roots(path: Path) -> bool:
    """Return True when the resolved ``path`` is contained in EXPORT_ROOTS (FT-01).

    Mirrors :func:`is_in_import_roots` (SE-02) for export destinations: shared
    by the HTTP /api/export route and the ExportInput MCP input model. Callers
    must pass an already ``expanduser().resolve()``-ed path. Reads EXPORT_ROOTS
    at call time so tests can monkeypatch it.
    """
    return any(path == root or root in path.parents for root in EXPORT_ROOTS)

# ---------------------------------------------------------------------------
# Folder watcher (FT-03)
# ---------------------------------------------------------------------------

_watch_dirs_env: str | None = os.environ.get("REMIND_ME_WATCH_DIRS")
WATCH_DIRS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _watch_dirs_env.split(":") if r.strip()]
    if _watch_dirs_env
    else []
)
"""Directories polled by the folder watcher (FT-03). Colon-separated paths.
Default: empty — the watcher is disabled. Every directory must lie inside
IMPORT_ROOTS (the SE-02 containment rule shared with the import tools);
non-contained entries are rejected at startup."""

WATCH_INTERVAL = _env_int("REMIND_ME_WATCH_INTERVAL", 60)
"""Seconds between folder watcher scan passes."""

WATCH_GRACE = _env_int("REMIND_ME_WATCH_GRACE", 5)
"""Debounce grace period in seconds. A file whose mtime is younger than this
is deferred until a later scan observes the same (mtime, size) signature, so
partially-written files are never ingested mid-write."""

# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

AUTO_UPDATE_CHECK: bool = os.environ.get(
    "REMIND_ME_AUTO_UPDATE_CHECK", "true"
).strip().lower() not in ("false", "0", "no", "off")
"""Set REMIND_ME_AUTO_UPDATE_CHECK=false to skip the background `git fetch`
update check at server startup (SE-06). The manual `remind_me_check_update`
and `remind_me_self_update` tools keep working regardless."""

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "MEMORY_DIR",
    "DB_PATH",
    "IMPORT_LOG",
    "PID_FILE",
    "WIKI_DIR",
    "WIKI_LOAD_TOKEN_BUDGET",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_BACKEND",
    "OLLAMA_URL",
    "OLLAMA_EMBED_MODEL",
    "EMBED_BATCH_SIZE",
    "EMBED_FORWARD_BATCH",
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
    "API_KEY_FILE",
    "resolve_api_key",
    "REMOTE_MCP",
    "REMOTE_MCP_HOST",
    "REMOTE_MCP_PORT",
    "REMOTE_MCP_TOKEN",
    "CONNECTOR_TOKEN_FILE",
    "REMOTE_MCP_ISSUER",
    "OAUTH_STATE_FILE",
    "resolve_connector_token",
    "IMPORT_ROOTS",
    "is_in_import_roots",
    "EXPORT_ROOTS",
    "is_in_export_roots",
    "WATCH_DIRS",
    "WATCH_INTERVAL",
    "WATCH_GRACE",
    "AUTO_UPDATE_CHECK",
]

# ---------------------------------------------------------------------------
# Sync configuration
# ---------------------------------------------------------------------------
NODE_ID = os.environ.get("REMIND_ME_NODE_ID", "")
CLIENT: str = os.getenv("REMIND_ME_CLIENT", "unknown")
HUB_URL = os.environ.get("REMIND_ME_HUB_URL", "")
SYNC_SECRET = os.environ.get("REMIND_ME_SYNC_SECRET", "")
SYNC_INTERVAL = _env_int("REMIND_ME_SYNC_INTERVAL", 60)
PEER_PORT = _env_int("REMIND_ME_PEER_PORT", 8766)
PEER_BIND = os.environ.get("REMIND_ME_PEER_BIND", "0.0.0.0")  # noqa: S104
"""Bind address for the peer sync server. Defaults to all interfaces so
Tailscale peers can reach it (their addresses are not known in advance);
set REMIND_ME_PEER_BIND to a specific address (e.g. this node's Tailscale
IP, or 127.0.0.1 to disable remote access) to narrow exposure. Every
request requires the SYNC_SECRET bearer token regardless of bind address."""
OUTBOX_RETENTION_DAYS = _env_int("REMIND_ME_OUTBOX_RETENTION_DAYS", 30)
"""Sync outbox rows older than this many days are pruned each sync cycle."""
SYNC_ENABLED = bool(NODE_ID and HUB_URL and SYNC_SECRET)
STATIC_PEERS: list[dict] = json.loads(
    os.environ.get("REMIND_ME_STATIC_PEERS", "[]")
)
TAILSCALE_SOCKET = os.environ.get("REMIND_ME_TAILSCALE_SOCKET", "")
