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
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Module logger only — root logging setup (logging.basicConfig) lives in the
# __main__ entrypoint so importing this package never hijacks the host
# application's logging configuration (HY-06).
log = logging.getLogger("remind_me_mcp.config")


def restrict_to_owner(path: Path) -> None:
    """Best-effort restrict a secret file to the current user only.

    ``chmod(0o600)`` is sufficient on POSIX, but on Windows os.chmod only
    toggles the read-only attribute -- it does not touch NTFS ACLs -- so a
    file "protected" that way is still readable by any other local account
    with filesystem access to the containing directory. On Windows, also
    strip inherited ACEs and grant Full Control only to the current user via
    the built-in ``icacls`` CLI. Best-effort: ACL failures are logged, not
    raised, since the secret file itself was already written successfully.
    """
    path.chmod(0o600)
    if os.name != "nt":
        return
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    if not user:
        log.warning(
            "Could not determine current user to restrict ACLs on %s "
            "(USERNAME/USERDOMAIN unset); file may be readable by other "
            "local accounts",
            path,
        )
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "Could not restrict ACLs on %s via icacls (%s); file may be "
            "readable by other local accounts",
            path,
            exc,
        )


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


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable, falling back to *default* when unset.

    Recognizes ``false``/``0``/``no``/``off`` (case-insensitive, surrounding
    whitespace stripped) as False. Deliberately unlike :func:`_env_int`'s
    "blank means unset" rule: an explicit *empty string* is also treated as
    False here regardless of *default* -- ``REMIND_ME_X=""`` is a meaningful,
    explicit opt-out for a boolean flag (mirroring how
    ``REMIND_ME_RERANK=""`` disables reranking), not the same as the
    variable being unset at all. Anything else (including unset) resolves
    to *default*.

    Args:
        name: The environment variable name.
        default: Value returned when the variable is unset.

    Returns:
        The parsed boolean.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")

# ---------------------------------------------------------------------------
# Directory / file paths
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(os.environ.get("REMIND_ME_MCP_DIR", "~/.remind-me")).expanduser()
DB_PATH = MEMORY_DIR / "memory.db"
IMPORT_LOG = MEMORY_DIR / "import_log.json"
PID_FILE = MEMORY_DIR / "server.pid"
# Separate from PID_FILE (which only ever tracked the UI dashboard server):
# nothing previously stopped two `--serve-mcp` processes from running
# against the same DB file concurrently — each with its own sync thread,
# watcher, etc. racing the other (issue #126).
MCP_PID_FILE = MEMORY_DIR / "mcp_server.pid"

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
# MemPalace importer
# ---------------------------------------------------------------------------

MEMPALACE_PATH = Path(
    os.environ.get("REMIND_ME_MEMPALACE_PATH", "~/.mempalace/palace")
).expanduser()
"""Path to a MemPalace ChromaDB persistent store, read directly (read-only)
by remind_me_import_mempalace. Default matches MemPalace's own default
palace location; only used if the optional ``mempalace`` extra (chromadb)
is installed."""

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

BACKUP_DIR = MEMORY_DIR / "backups"
BACKUP_RETENTION_COUNT = _env_int("REMIND_ME_BACKUP_RETENTION_COUNT", 10)
"""Number of backup files (manual + pre-migration) to keep in BACKUP_DIR.
Oldest backups beyond this count are pruned after each new backup is created."""

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

ANN_MIN_CHUNKS = _env_int("REMIND_ME_ANN_MIN_CHUNKS", 5000)
"""Minimum chunk-vector count (rows in memories_vec) before _semantic_search
consults the optional HNSW ANN index (remind_me_mcp.ann_index) instead of
sqlite-vec's exact brute-force scan. Below this, brute force is already fast
enough that an approximate index only adds overhead and approximation error
for no benefit — a typical single-user store never crosses it. Has no effect
if the optional `usearch` package (the `ann` extra) isn't installed; the
brute-force scan is always the fallback."""

ANN_AUTOSAVE_EVERY = _env_int("REMIND_ME_ANN_AUTOSAVE_EVERY", 200)
"""Persist the in-memory ANN index to disk after this many add/remove
mutations, in addition to the always-on save at clean shutdown. Without this,
an abnormal exit (crash, taskkill /F, power loss) between clean shutdowns
silently loses every incremental ANN update since the last save, forcing a
full rebuild from memories_vec on next start with no warning it happened.
Set to 0 to disable periodic autosave (shutdown-only, the old behavior)."""

CONSOLIDATE_MAX_CANDIDATES = _env_int("REMIND_ME_CONSOLIDATE_MAX_CANDIDATES", 1500)
"""Hard cap on how many memories consolidation.find_clusters pairwise-compares
in one call. remind_me_consolidate's own `limit` (default 500, max 5000)
already bounds the candidate pool, but find_clusters' clustering step is
O(n^2) — at the pool's own max, an all-near-duplicate vault could still
produce a huge edge list even with the vectorized similarity comparison.
Excess candidates are dropped (oldest-considered-first, i.e. the tail of
whatever order the caller passed in) and the truncation is reported back to
the caller rather than happening silently."""

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
"""Bearer token gating /mcp in combined mode (--serve-mcp --serve-ui), from
the REMIND_ME_MCP_HTTP_SECRET env var.

When unset, a secret is auto-generated on first use and persisted under
MEMORY_DIR (see resolve_mcp_http_secret) -- mirroring resolve_connector_token,
not resolve_api_key: there is no 'disabled' opt-out, since /mcp is the full
MCP tool-call surface (read/write memory access, including destructive tools
and remind_me_self_update), at least as sensitive as the remote connector.
Standalone MCP HTTP mode (--serve-mcp without --serve-ui) is unaffected by
this and stays unauthenticated by design, relying on its localhost-only
default bind -- same posture as the peer/webhook servers."""

MCP_HTTP_SECRET_FILE = MEMORY_DIR / "mcp_http_secret"
"""Location of the auto-generated combined-mode MCP secret (0600 perms).
Delete the file to rotate: a fresh secret is generated on next resolution."""


def resolve_mcp_http_secret() -> str:
    """Return the effective combined-mode /mcp bearer secret.

    Resolution order mirrors :func:`resolve_connector_token`:
      1. ``REMIND_ME_MCP_HTTP_SECRET`` env var — always wins when set.
      2. The secret persisted at ``MEMORY_DIR/mcp_http_secret``.
      3. First use: generate a new secret, persist it with 0600 permissions,
         and log it once (the only time the full secret is logged).

    If the secret file can be neither read nor written, an ephemeral secret
    is generated for this process (and logged) so /mcp never falls open.

    Reads module attributes at call time so tests can monkeypatch
    ``MCP_HTTP_SECRET`` / ``MEMORY_DIR``.
    """
    if MCP_HTTP_SECRET is not None:
        return MCP_HTTP_SECRET.strip()
    secret_file = MEMORY_DIR / "mcp_http_secret"
    try:
        if secret_file.is_file():
            existing = secret_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        secret = secrets.token_urlsafe(32)
        secret_file.touch(mode=0o600, exist_ok=True)
        restrict_to_owner(secret_file)
        secret_file.write_text(secret + "\n", encoding="utf-8")
        log.info(
            "Generated combined-mode MCP bearer secret — stored at %s. "
            "Clients must send 'Authorization: Bearer <secret>' to reach "
            "/mcp: %s",
            secret_file,
            secret,
        )
        return secret
    except OSError as exc:
        secret = secrets.token_urlsafe(32)
        log.warning(
            "Could not persist MCP HTTP secret at %s (%s); using an "
            "ephemeral secret for this run: %s",
            secret_file,
            exc,
            secret,
        )
        return secret

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def _resolve_or_generate_secret(
    explicit: str | None,
    filename: str,
    *,
    label: str,
    noun: str,
    log_generated: Callable[[Path, str], None],
) -> str:
    """Shared "read-existing-file-or-generate-and-persist" secret resolution.

    Factors out the body that :func:`resolve_api_key`, :func:`resolve_connector_token`,
    and :func:`resolve_ics_token` each repeated verbatim (issue #185 is the
    third consumer of this exact pattern; this is the resulting factoring):
    trust an explicit value if the caller already resolved one; else read an
    existing 0600 secret file; else generate a fresh one, persist it with
    0600 permissions, and log the generation exactly once.

    Deliberately does NOT own the "which env var wins, and any special
    sentinel values" step -- that varies per caller (only ``resolve_api_key``
    treats the literal ``"disabled"`` specially) and stays in each caller,
    which passes in its own already-resolved ``explicit`` value. It also
    doesn't own the log message shown at generation time -- callers log a
    different level of detail (``resolve_api_key`` never logs the key itself,
    only its file path; the others log the file path *and* the secret) --
    passed in as ``log_generated`` instead of a single templated string.

    Reads ``MEMORY_DIR`` at call time (not a captured default) so tests can
    monkeypatch it, matching every caller's own "reads module attributes at
    call time" contract.

    Args:
        explicit: The caller's already-resolved env var value, or None when
            unset. Returned as-is when not None -- no file I/O happens.
        filename: File name under ``MEMORY_DIR`` to read/persist the secret at.
        label: Human name for the secret, used in the ephemeral-fallback
            warning's first clause (e.g. ``"connector token"``).
        noun: Short noun for the same warning's second clause (``"key"`` or
            ``"token"``), matching each call site's original wording.
        log_generated: Called with ``(secret_file, secret)`` exactly once,
            immediately after a fresh secret is generated and persisted --
            never on a cache hit (an existing file, or an explicit value).

    Returns:
        The effective secret.
    """
    if explicit is not None:
        return explicit
    secret_file = MEMORY_DIR / filename
    try:
        if secret_file.is_file():
            existing = secret_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        secret = secrets.token_urlsafe(32)
        secret_file.touch(mode=0o600, exist_ok=True)
        restrict_to_owner(secret_file)
        secret_file.write_text(secret + "\n", encoding="utf-8")
        log_generated(secret_file, secret)
        return secret
    except OSError as exc:
        secret = secrets.token_urlsafe(32)
        log.warning(
            "Could not persist %s at %s (%s); using an ephemeral %s for this run: %s",
            label,
            secret_file,
            exc,
            noun,
            secret,
        )
        return secret


API_KEY: str | None = os.environ.get("REMIND_ME_API_KEY") or None
"""Bearer token for /api/* routes, from the REMIND_ME_API_KEY env var.

When unset, a key is auto-generated on first run and persisted under
MEMORY_DIR (see resolve_api_key). The special value ``disabled``
(case-insensitive) turns dashboard auth off for users who explicitly
want an open localhost API."""

API_KEY_FILE = MEMORY_DIR / "api_key"
"""Location of the auto-generated dashboard API key (created with 0600 perms)."""

API_KEYS_FILE = MEMORY_DIR / "api_keys.json"
"""Location of the named, scoped API key store (issue #185; 0600 perms) --
see ``remind_me_mcp.api_keys.ApiKeyStore``. Additive to ``API_KEY``/
``API_KEY_FILE`` above, which remains the implicit backward-compat
read-write key and is never stored in this file."""


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

    def _log_generated(key_file: Path, key: str) -> None:
        log.info(
            "Generated dashboard API key — stored at %s. Clients must send "
            "'Authorization: Bearer <key>'. Set REMIND_ME_API_KEY=disabled to "
            "opt out of dashboard auth.",
            key_file,
        )

    return _resolve_or_generate_secret(
        None, "api_key", label="dashboard API key", noun="key", log_generated=_log_generated
    )


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
    explicit = REMOTE_MCP_TOKEN.strip() if REMOTE_MCP_TOKEN is not None else None

    def _log_generated(token_file: Path, token: str) -> None:
        log.info(
            "Generated remote MCP connector token — stored at %s. Connector "
            "URL path: /mcp/%s (treat the URL like a password; rotate by "
            "deleting the file).",
            token_file,
            token,
        )

    return _resolve_or_generate_secret(
        explicit, "connector_token", label="connector token", noun="token", log_generated=_log_generated
    )


# ---------------------------------------------------------------------------
# Reminders calendar feed (issue #190)
# ---------------------------------------------------------------------------

ICS_TOKEN: str | None = os.environ.get("REMIND_ME_ICS_TOKEN") or None
"""Secret path token for the ``GET /api/reminders/{token}.ics`` calendar feed,
from the REMIND_ME_ICS_TOKEN env var.

When unset, a token is auto-generated on first use and persisted under
MEMORY_DIR (see resolve_ics_token) -- mirroring resolve_connector_token, not
resolve_api_key: there is no 'disabled' opt-out, because the token doubles as
the URL path itself (the feed cannot use the Authorization-header bearer
scheme the rest of /api/* uses -- a calendar app's "subscribe by URL" feature
polls the URL from the provider's own servers on a schedule the user doesn't
control, with no way to attach custom headers), so the endpoint must never
fall open."""

ICS_TOKEN_FILE = MEMORY_DIR / "ics_token"
"""Location of the auto-generated reminders-feed token (0600 perms). Delete
the file to rotate: a fresh token is generated on next resolution, which
also changes the subscribe URL every calendar app must be re-pointed at."""


def resolve_ics_token() -> str:
    """Return the effective reminders-feed secret-path token (issue #190).

    Resolution order mirrors :func:`resolve_connector_token` (FT-05):
      1. ``REMIND_ME_ICS_TOKEN`` env var — always wins when set.
      2. The token persisted at ``MEMORY_DIR/ics_token``.
      3. First use: generate a new token, persist it with 0600 permissions,
         and log the resulting feed path once (the only time the full token
         is logged).

    If the token file can be neither read nor written, an ephemeral token is
    generated for this process (and logged) so the feed never falls open.

    Reads module attributes at call time so tests can monkeypatch
    ``ICS_TOKEN`` / ``MEMORY_DIR``.
    """
    explicit = ICS_TOKEN.strip() if ICS_TOKEN is not None else None

    def _log_generated(token_file: Path, token: str) -> None:
        log.info(
            "Generated reminders calendar feed token — stored at %s. Feed "
            "path: /api/reminders/%s.ics (treat this URL like a password; "
            "rotate by deleting the file).",
            token_file,
            token,
        )

    return _resolve_or_generate_secret(
        explicit, "ics_token", label="reminders feed token", noun="token", log_generated=_log_generated
    )


_import_roots_env: str | None = os.environ.get("REMIND_ME_IMPORT_ROOTS")
IMPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _import_roots_env.split(os.pathsep) if r.strip()]
    if _import_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for import operations. os.pathsep-separated paths (';' on Windows, ':' elsewhere). Default: user home directory."""


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
    [Path(r.strip()).expanduser().resolve() for r in _export_roots_env.split(os.pathsep) if r.strip()]
    if _export_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for export destinations. os.pathsep-separated paths (';' on Windows, ':' elsewhere). Default: user home directory."""


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
    [Path(r.strip()).expanduser().resolve() for r in _watch_dirs_env.split(os.pathsep) if r.strip()]
    if _watch_dirs_env
    else []
)
"""Directories polled by the folder watcher (FT-03). os.pathsep-separated paths (';' on Windows, ':' elsewhere).
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
# Reminders (issue #179)
# ---------------------------------------------------------------------------

REMINDER_POLL_INTERVAL = _env_int("REMIND_ME_REMINDER_POLL_INTERVAL", 60)
"""Seconds between remind_me_mcp.scheduler poll passes for due reminders
(memories.remind_at <= now, not yet in reminder_deliveries). Unlike the
folder watcher, the scheduler always runs -- this only tunes how often it
checks, not whether it's enabled."""

# ---------------------------------------------------------------------------
# Edit history (issue #187)
# ---------------------------------------------------------------------------

REVISION_RETENTION_DAYS = _env_int("REMIND_ME_REVISION_RETENTION_DAYS", 90)
"""How many days a pre-edit memory_revisions snapshot (issue #187) is kept
before db._compact_revisions hard-deletes it. Purely time-based, mirroring
TOMBSTONE_RETENTION_DAYS -- no cross-device acknowledgment tracking, since
memory_revisions is a local-only audit table that is never synced (see the
v23->v24 migration docstring). Deliberately shorter than
TOMBSTONE_RETENTION_DAYS's 180-day default: losing an old revision snapshot
only narrows how far back remind_me_revert can reach, not the same class of
risk as resurrecting a deleted memory that TOMBSTONE_RETENTION_DAYS's longer
window guards against."""

# ---------------------------------------------------------------------------
# Analytics trend snapshots (issue #186)
# ---------------------------------------------------------------------------

ANALYTICS_RETENTION_DAYS = _env_int("REMIND_ME_ANALYTICS_RETENTION_DAYS", 730)
"""How many days a daily analytics_snapshots row (issue #186) is kept before
db._compact_analytics_snapshots hard-deletes it. Purely time-based, mirroring
REVISION_RETENTION_DAYS/TOMBSTONE_RETENTION_DAYS -- no cross-device
acknowledgment tracking, since analytics_snapshots is a local-only
observability table that is never synced (see the v24->v25 migration
docstring). Deliberately an order of magnitude more generous than
REVISION_RETENTION_DAYS's 90-day default (and well beyond
TOMBSTONE_RETENTION_DAYS's 180): each row is one tiny daily rollup (a couple
of small JSON blobs, not full content), and the whole point of the trend
panel is long-range "is my vault healthy over time" viewing, not short-range
audit -- 730 days keeps two full years of daily points, which is cheap
(roughly 730 rows regardless of vault size) and still leaves a bounded
retention window rather than growing forever."""

# ---------------------------------------------------------------------------
# Notifications (issue #180)
# ---------------------------------------------------------------------------

NOTIFY_WEBHOOK_URL = os.environ.get("REMIND_ME_NOTIFY_WEBHOOK_URL", "")
"""Webhook URL that receives a generic JSON POST
(``{"subject": ..., "body": ..., "source": "remind-me"}``) for each
notification -- one config covers ntfy/Slack/Discord/Mattermost/
Pushover-via-webhook uniformly, deliberately without per-service payload
formatting (point this at a small relay/transform if you want native
formatting on one of those services). Empty (default) disables the webhook
notifier entirely -- gated on config presence, mirroring how the
embedder/reranker decide availability from configuration alone rather than a
separate on/off flag."""

NOTIFY_WEBHOOK_TIMEOUT = _env_int("REMIND_ME_NOTIFY_WEBHOOK_TIMEOUT", 5)
"""Seconds to wait for the webhook POST before giving up, so a hung endpoint
can never block the reminder scheduler or sync thread that triggered the
notification."""

NOTIFY_SMTP_HOST = os.environ.get("REMIND_ME_NOTIFY_SMTP_HOST", "")
"""SMTP server host. Empty (default) disables the email notifier -- gated on
config presence, mirroring NOTIFY_WEBHOOK_URL."""

NOTIFY_SMTP_PORT = _env_int("REMIND_ME_NOTIFY_SMTP_PORT", 587)
"""SMTP server port. Port 465 always uses implicit TLS (smtplib.SMTP_SSL)
regardless of NOTIFY_SMTP_USE_TLS; any other port uses plain smtplib.SMTP
with STARTTLS applied when NOTIFY_SMTP_USE_TLS is true."""

NOTIFY_SMTP_USER = os.environ.get("REMIND_ME_NOTIFY_SMTP_USER", "")
"""SMTP AUTH username. Empty skips SMTP AUTH entirely (some internal relays
allow unauthenticated submission)."""

NOTIFY_SMTP_PASSWORD = os.environ.get("REMIND_ME_NOTIFY_SMTP_PASSWORD", "")
"""SMTP AUTH password."""

NOTIFY_SMTP_FROM = os.environ.get("REMIND_ME_NOTIFY_SMTP_FROM", "")
"""Envelope/header From address. Falls back to NOTIFY_SMTP_USER when unset,
since most providers require From to match the authenticated account anyway."""

NOTIFY_SMTP_TO = os.environ.get("REMIND_ME_NOTIFY_SMTP_TO", "")
"""Comma-separated recipient address(es). Required (with NOTIFY_SMTP_HOST)
for the email notifier to be considered configured."""

NOTIFY_SMTP_USE_TLS: bool = os.environ.get(
    "REMIND_ME_NOTIFY_SMTP_USE_TLS", "true"
).strip().lower() not in ("false", "0", "no", "off")
"""Whether to STARTTLS a plaintext SMTP connection before authenticating
(default true, matching the port 587 default). Has no effect on port 465,
which always uses implicit TLS. Set false only against a trusted local relay
with no TLS support."""

NOTIFY_SYNC_FAULT_INTERVAL = _env_int("REMIND_ME_NOTIFY_SYNC_FAULT_INTERVAL", 1800)
"""Minimum seconds between sync-fault notifications. remind_me_sync_reconcile
can be called repeatedly (e.g. by an external monitor) while a fault verdict
persists; alerting on every call would be exactly the alert-fatigue failure
BACKLOG Wave 4 documents, so this throttles to one notification per window
per persisting fault rather than firing on every poll."""

# ---------------------------------------------------------------------------
# Automation event stream (issue #198)
# ---------------------------------------------------------------------------
#
# Deliberately a separate config/delivery path from NOTIFY_WEBHOOK_URL above,
# not a second consumer of it: NOTIFY_WEBHOOK_URL is for human-facing,
# throttled alerts (a fired reminder, a faulted sync verdict) meant to be
# read by a person on a phone/Slack/ntfy; this is a raw, unthrottled
# create/update/delete event stream meant to be consumed by automation (a
# webhook relay, a second indexer, an audit log) that wants to know about
# every memory mutation, not a curated subset of human-relevant ones. See
# remind_me_mcp.events' module docstring for the delivery mechanics.

EVENT_WEBHOOK_URL = os.environ.get("REMIND_ME_EVENT_WEBHOOK_URL", "")
"""Webhook URL that receives one JSON POST per memory create/update/delete —
``{"event": "created"|"updated"|"deleted", "memory_id": ..., "category": ...,
"timestamp": ...}``. Metadata only, deliberately: memory content is never
included in the payload (scope limit, not an oversight — this is an
automation event stream, not a content-sync mechanism). Empty (default)
disables it entirely, mirroring how NOTIFY_WEBHOOK_URL/the embedder/reranker
decide availability from configuration alone rather than a separate on/off
flag. Unlike NOTIFY_WEBHOOK_URL's sync-fault throttling, there is no
throttling here — every qualifying event fires, since a consumer of a raw
event stream needs completeness, not alert-fatigue protection."""

EVENT_WEBHOOK_TIMEOUT = _env_int("REMIND_ME_EVENT_WEBHOOK_TIMEOUT", 5)
"""Seconds to wait for the event webhook POST before giving up, mirroring
NOTIFY_WEBHOOK_TIMEOUT's role for the human-alert channel -- a hung endpoint
must never block the memory_add/update/delete call that triggered the event
(the POST itself runs as a held-reference fire-and-forget background task,
see remind_me_mcp.events._spawn_task, so this bounds the task's own runtime,
not the caller's)."""

# ---------------------------------------------------------------------------
# Saved searches (issue #194)
# ---------------------------------------------------------------------------

SAVED_SEARCH_POLL_INTERVAL = _env_int("REMIND_ME_SAVED_SEARCH_POLL_INTERVAL", 300)
"""Seconds between remind_me_mcp.scheduler poll passes that check watch=true
saved searches for new matches. Deliberately coarser than
REMINDER_POLL_INTERVAL's 60s default -- a saved search's underlying content
changes far less often than a reminder's due time -- but implemented as a
persisted-watermark due-check inside the *same* poll loop, exactly mirroring
DIGEST_INTERVAL_SECONDS's shape (a `sync_flags` watermark, not a second
thread), rather than a separate poll interval mechanism. Unlike the digest
interval, this is not itself an opt-in switch: whether polling actually does
anything is gated per-search by `saved_searches.watch`, not by this value
being set -- this only tunes how often the (usually empty) check runs."""

# ---------------------------------------------------------------------------
# Digest (issue #188)
# ---------------------------------------------------------------------------

DIGEST_INTERVAL = os.environ.get("REMIND_ME_DIGEST_INTERVAL", "").strip().lower()
"""'daily' / 'weekly' / '' (default, disabled). Unlike REMINDER_POLL_INTERVAL,
scheduled digest delivery is genuinely opt-in -- a digest is a standing
summary, not core reminder functionality, so it stays off until configured.
The on-demand `remind_me_digest` tool call is unaffected by this either way;
it always works standalone."""

_DIGEST_INTERVAL_SECONDS: dict[str, int] = {"daily": 86400, "weekly": 604800}

DIGEST_INTERVAL_SECONDS: int | None = _DIGEST_INTERVAL_SECONDS.get(DIGEST_INTERVAL)
"""Resolved seconds for DIGEST_INTERVAL, or None when disabled or an
unrecognized value was given (treated the same as disabled -- a typo'd
interval should not silently pick some other cadence)."""

if DIGEST_INTERVAL and DIGEST_INTERVAL_SECONDS is None:
    log.warning(
        "REMIND_ME_DIGEST_INTERVAL=%r is not 'daily' or 'weekly' -- "
        "scheduled digest delivery stays disabled",
        DIGEST_INTERVAL,
    )

# ---------------------------------------------------------------------------
# Push/webhook ingestion (FT-09, Phase 5a)
# ---------------------------------------------------------------------------

WEBHOOK_PORT = _env_int("REMIND_ME_WEBHOOK_PORT", 8769)
WEBHOOK_BIND = os.environ.get("REMIND_ME_WEBHOOK_BIND", "127.0.0.1")
"""Bind address for the webhook ingestion server. Defaults to localhost-only
(unlike the Tailscale-oriented peer sync server) since a push endpoint writes
arbitrary content directly into memory — widen it deliberately (e.g. to a
Tailscale IP or 0.0.0.0 behind a reverse proxy) via REMIND_ME_WEBHOOK_BIND."""

WEBHOOK_SECRET = os.environ.get("REMIND_ME_WEBHOOK_SECRET", "")
"""Bearer token required on every /ingest request. The webhook server
refuses to start when this is unset — an unsecured push endpoint would be
worse than useless."""

# ---------------------------------------------------------------------------
# Rate limiting (issue #183)
# ---------------------------------------------------------------------------

RATE_LIMIT_ENABLED: bool = _env_bool("REMIND_ME_RATE_LIMIT_ENABLED", True)
"""Whether the webhook ingest endpoint and remote MCP connector enforce a
request-rate limit. Default on. REMIND_ME_RATE_LIMIT_ENABLED="" disables it
entirely, mirroring how REMIND_ME_RERANK="" disables reranking -- an
explicit empty value is as much an opt-out as any of the recognized false
spellings."""

RATE_LIMIT_REQUESTS = _env_int("REMIND_ME_RATE_LIMIT_REQUESTS", 60)
"""Max requests per REMIND_ME_RATE_LIMIT_WINDOW_SECONDS per rate-limit key
(remind_me_mcp.rate_limit.RateLimiter). Sync traffic uses its own hub/peer
protocol (peer_server.py), entirely separate from the two routes this
limits, so SYNC_INTERVAL's cadence never factors into this default."""

RATE_LIMIT_WINDOW_SECONDS = _env_int("REMIND_ME_RATE_LIMIT_WINDOW_SECONDS", 60)
"""Window length in seconds for REMIND_ME_RATE_LIMIT_REQUESTS."""

# ---------------------------------------------------------------------------
# Metrics (issue #197)
# ---------------------------------------------------------------------------

METRICS_ENABLED: bool = _env_bool("REMIND_ME_METRICS_ENABLED", False)
"""Whether GET /metrics (remind_me_mcp.metrics) is served. Default off,
matching the OTel tracing opt-in precedent (see README's "Observability"
section) -- this is instrumentation surface, not a core feature, and an
operator who wants it firewalls/gates the port themselves same as any other
scrape target. REMIND_ME_METRICS_ENABLED="" disables it explicitly, mirroring
RATE_LIMIT_ENABLED's empty-string opt-out convention. When off, every
remind_me_mcp.metrics.record_* call is a no-op (mirrors telemetry.maybe_span)
-- no counter state accumulates, and GET /metrics returns 404."""

# ---------------------------------------------------------------------------
# OCR (image import, issue #181/#202)
# ---------------------------------------------------------------------------
#
# image_import.py's default RapidOCR() call uses the models bundled inside
# the rapidocr-onnxruntime wheel (ch_PP-OCRv4 detection/recognition +
# ch_ppocr_mobile_v2.0 orientation classification) -- a combined Chinese +
# English/Latin+digits charset baked into the recognition model's ONNX
# metadata. It does not recognize other scripts (Japanese, Korean, Arabic,
# Cyrillic, Devanagari, ...). RapidOCR's own constructor already accepts
# det_model_path/cls_model_path/rec_model_path kwargs to swap in a
# different script's model (downloaded separately -- RapidOCR's model zoo
# publishes per-language recognition models, but only the Chinese+English
# one ships in the pip package itself). These three env vars are a thin,
# optional passthrough to that existing RapidOCR capability, unset (None)
# by default so behavior is byte-for-byte identical to plain RapidOCR()
# until a user opts in.

OCR_DET_MODEL_PATH = os.environ.get("REMIND_ME_OCR_DET_MODEL_PATH") or None
"""Optional path to an alternate ONNX text-detection model, passed through
to RapidOCR(det_model_path=...). Unset by default."""

OCR_CLS_MODEL_PATH = os.environ.get("REMIND_ME_OCR_CLS_MODEL_PATH") or None
"""Optional path to an alternate ONNX text-orientation-classification model,
passed through to RapidOCR(cls_model_path=...). Unset by default."""

OCR_REC_MODEL_PATH = os.environ.get("REMIND_ME_OCR_REC_MODEL_PATH") or None
"""Optional path to an alternate ONNX text-recognition model, passed through
to RapidOCR(rec_model_path=...). This is the model whose baked-in character
set actually determines which script(s) OCR can read -- set it (typically
paired with a matching REMIND_ME_OCR_DET_MODEL_PATH, since detection
geometry can differ by script) to a recognition model downloaded from
RapidOCR's model zoo (https://github.com/RapidAI/RapidOCR) for a script the
bundled ch_PP-OCRv4 model doesn't cover. Unset by default."""

# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

AUTO_UPDATE_CHECK: bool = os.environ.get(
    "REMIND_ME_AUTO_UPDATE_CHECK", "true"
).strip().lower() not in ("false", "0", "no", "off")
"""Set REMIND_ME_AUTO_UPDATE_CHECK=false to skip the background `git fetch`
update check at server startup (SE-06). The manual `remind_me_check_update`
and `remind_me_self_update` tools keep working regardless."""

UPDATE_EXPECTED_ORIGIN: str | None = os.environ.get("REMIND_ME_UPDATE_EXPECTED_ORIGIN") or None
"""Optional trust pin for `remind_me_self_update` (SEC-05). remind_me_self_update
always does `git pull --ff-only origin main` -- nothing verifies `origin`
actually points where you expect, so a repointed remote (compromise, a stray
`git remote set-url`) would otherwise be pulled and pip-installed without
question. When set, perform_update() refuses to proceed unless the local
`origin` remote's URL matches this value exactly. Unset by default since
there's no single correct value for every fork of this package."""

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "restrict_to_owner",
    "MEMORY_DIR",
    "DB_PATH",
    "IMPORT_LOG",
    "PID_FILE",
    "MCP_PID_FILE",
    "WIKI_DIR",
    "WIKI_LOAD_TOKEN_BUDGET",
    "MEMPALACE_PATH",
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
    "ANN_MIN_CHUNKS",
    "ANN_AUTOSAVE_EVERY",
    "CONSOLIDATE_MAX_CANDIDATES",
    "MODEL_DIR",
    "BACKUP_DIR",
    "BACKUP_RETENTION_COUNT",
    "SERVE_UI",
    "UI_PORT",
    "SERVE_MCP",
    "MCP_HTTP_PORT",
    "MCP_HTTP_HOST",
    "MCP_HTTP_SECRET",
    "MCP_HTTP_SECRET_FILE",
    "resolve_mcp_http_secret",
    "API_KEY",
    "API_KEY_FILE",
    "API_KEYS_FILE",
    "resolve_api_key",
    "REMOTE_MCP",
    "REMOTE_MCP_HOST",
    "REMOTE_MCP_PORT",
    "REMOTE_MCP_TOKEN",
    "CONNECTOR_TOKEN_FILE",
    "REMOTE_MCP_ISSUER",
    "OAUTH_STATE_FILE",
    "resolve_connector_token",
    "ICS_TOKEN",
    "ICS_TOKEN_FILE",
    "resolve_ics_token",
    "IMPORT_ROOTS",
    "is_in_import_roots",
    "EXPORT_ROOTS",
    "is_in_export_roots",
    "WATCH_DIRS",
    "WATCH_INTERVAL",
    "WATCH_GRACE",
    "REMINDER_POLL_INTERVAL",
    "REVISION_RETENTION_DAYS",
    "ANALYTICS_RETENTION_DAYS",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_WEBHOOK_TIMEOUT",
    "NOTIFY_SMTP_HOST",
    "NOTIFY_SMTP_PORT",
    "NOTIFY_SMTP_USER",
    "NOTIFY_SMTP_PASSWORD",
    "NOTIFY_SMTP_FROM",
    "NOTIFY_SMTP_TO",
    "NOTIFY_SMTP_USE_TLS",
    "NOTIFY_SYNC_FAULT_INTERVAL",
    "EVENT_WEBHOOK_URL",
    "EVENT_WEBHOOK_TIMEOUT",
    "DIGEST_INTERVAL",
    "DIGEST_INTERVAL_SECONDS",
    "WEBHOOK_PORT",
    "WEBHOOK_BIND",
    "WEBHOOK_SECRET",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "METRICS_ENABLED",
    "AUTO_UPDATE_CHECK",
    "UPDATE_EXPECTED_ORIGIN",
    "DB_ENCRYPTION_KEY",
    "OCR_DET_MODEL_PATH",
    "OCR_CLS_MODEL_PATH",
    "OCR_REC_MODEL_PATH",
]

# ---------------------------------------------------------------------------
# Encryption at rest (issue #184)
# ---------------------------------------------------------------------------

DB_ENCRYPTION_KEY: str | None = os.environ.get("REMIND_ME_DB_ENCRYPTION_KEY") or None
"""SQLCipher passphrase, opt-in and off by default. See ARCHITECTURE.md's
"Encryption at rest" design note for the full rationale, coverage, and known
limitations.

When unset (the default), this changes nothing: `remind_me_mcp.db` and
`backup.py` open the database exactly as before #184, via the stdlib
`sqlite3` module -- the encrypted code path is never imported, never
reached.

When set, `db._open_db_connection` (the single choke point shared by the
live connection and `backup.py`'s backup-destination/restore-validation
connections) opens through the optional `sqlcipher3` package instead and
issues `PRAGMA key = '<key>'` as the very first statement on the
connection, before any other pragma or query -- SQLCipher's required
activation sequence. Requires the `encryption` extra
(`pip install remind-me-mcp[encryption]`); if the key is set but the
package isn't installed, connection opening raises a clear `RuntimeError`
rather than silently falling back to plaintext.

Deliberately never logged -- not even the "generated a secret, here it is
once" pattern `resolve_api_key`/`resolve_connector_token`/`resolve_ics_token`
use elsewhere in this module, because those generate and persist a fresh
secret (so logging it once is the only way the operator ever sees it); this
key is always user-supplied and never auto-generated or persisted by this
module, so there is nothing here that needs announcing, only a value to
read once per connection and pass straight to SQLCipher."""

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
TOMBSTONE_RETENTION_DAYS = _env_int("REMIND_ME_TOMBSTONE_RETENTION_DAYS", 180)
"""A deleted memory (deleted_at set) is only hard-deleted this many days
after the delete, purely time-based like OUTBOX_RETENTION_DAYS (no per-peer
acknowledgment tracking — this is a single-owner, LWW sync model, not a
general-purpose replicated database). Deliberately generous and longer than
OUTBOX_RETENTION_DAYS: hard-deleting a tombstone too early risks a genuinely
offline device later pushing a stale copy of the "deleted" memory and
resurrecting it, which is a worse failure mode than a slower-to-compact
tombstone table. Only ever runs while sync is enabled (config.SYNC_ENABLED)
— a single, never-synced device just hard-deletes immediately instead."""
SYNC_ENABLED = bool(NODE_ID and HUB_URL and SYNC_SECRET)
STATIC_PEERS: list[dict] = json.loads(
    os.environ.get("REMIND_ME_STATIC_PEERS", "[]")
)
TAILSCALE_SOCKET = os.environ.get("REMIND_ME_TAILSCALE_SOCKET", "")
