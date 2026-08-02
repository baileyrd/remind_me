"""
remind_me_mcp.api_keys — named, scope-limited dashboard API keys (issue #185).

Extends the single flat dashboard API key (``config.resolve_api_key`` /
``REMIND_ME_API_KEY``, SE-01) with additional named credentials the owner can
issue for a lower-trust purpose -- e.g. sharing a read-only dashboard view, or
embedding in a client that shouldn't be able to mutate the vault -- without
handing out the full read-write key. This is explicitly NOT multi-tenancy
(ARCHITECTURE.md non-goal): every key still reads and writes the same single
vault: only the *scope* (read vs. read-write) differs per key, there is no
per-key data partitioning.

Storage mirrors ``oauth.OAuthStateStore``'s conventions: a small JSON file
under ``MEMORY_DIR`` (0600 perms, atomic ``os.replace`` writes, re-read on
every operation so a change from another process is picked up immediately),
holding only a SHA-256 hash of each key -- never the plaintext -- exactly
like ``oauth.py``'s "no plaintext secrets at rest" discipline. Verification
hashes the presented key and compares with ``hmac.compare_digest`` (SE-05).

The backward-compat single key (``config.resolve_api_key()``) is NOT stored
here at all -- it stays config-managed (env var or its own auto-generated
file) and is always implicitly read-write, exactly as before this feature
existed. This store only ever holds *additional*, explicitly-created keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import restrict_to_owner

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("remind_me_mcp.api_keys")

SCOPES = ("read", "read-write")
"""Valid scope values. 'read' may only reach GET routes; 'read-write' has
the same full access as the backward-compat default key."""

DEFAULT_KEY_NAME = "default"
"""Synthetic name reported (list only -- never stored) for the backward-
compat REMIND_ME_API_KEY / auto-generated key. Reserved: a caller cannot
create or revoke a key under this name through this store."""

# secrets.token_urlsafe(32) matches every other auto-generated credential in
# this codebase (dashboard API key, connector token, ICS token, OAuth
# access/refresh tokens) -- same entropy, same encoding.
_KEY_ENTROPY_BYTES = 32


def _hash_key(key: str) -> str:
    """SHA-256 hex digest of a key -- what the store persists instead of the plaintext.

    Mirrors ``oauth._hash_token`` exactly (same algorithm, same "hash at
    rest" contract); kept as a small local copy rather than importing
    oauth's private helper, since oauth.py additionally pulls in the
    ``mcp.server.auth`` SDK -- a needless heavier import for this module's
    stdio-mode callers (the MCP tool, and BearerAuthMiddleware in api.py).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """UTC timestamp for a key's created_at -- display-only, no ordering guarantee needed."""
    return datetime.now(UTC).isoformat()


class ApiKeyStore:
    """JSON-file persistence for named, scoped dashboard API keys.

    Layout: ``{"keys": [{"name", "key_hash", "scope", "created_at"}, ...]}``.
    The file is created with 0600 permissions and re-read on every operation
    (mirrors ``oauth.OAuthStateStore``), so a key created or revoked from
    another process (the MCP tool running in stdio mode) takes effect on a
    running dashboard server immediately. The in-process lock only
    serialises read-modify-write cycles; cross-process locking is out of
    scope, matching every other single-owner state file in this codebase.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        """Load the key list from disk, tolerating a missing or corrupt file."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read API key store at %s (%s); treating as empty", self.path, exc)
            return []
        keys = raw.get("keys") if isinstance(raw, dict) else None
        return list(keys) if isinstance(keys, list) else []

    def _write(self, keys: list[dict[str, Any]]) -> None:
        """Persist the key list with 0600 permissions via an atomic replace.

        Writes to a sibling temp file and ``os.replace``s it into place
        (mirrors ``oauth.OAuthStateStore._write``) instead of truncating
        ``self.path`` in place, so a crash or power loss mid-write can never
        leave a corrupt/partial file that a subsequent read+write would
        otherwise treat as empty and overwrite every existing key with.

        Raises:
            OSError: if the write genuinely fails -- propagated rather than
                swallowed, so ``create_key``/``revoke_key`` cannot report
                success for a write that never landed.
        """
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(json.dumps({"keys": keys}, indent=2) + "\n", encoding="utf-8")
            restrict_to_owner(tmp_path)
            os.replace(tmp_path, self.path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            log.error("Could not persist API key store at %s", self.path, exc_info=True)
            raise

    def list_keys(self) -> list[dict[str, Any]]:
        """Return every stored key's name/scope/created_at -- NEVER the hash or plaintext."""
        return [
            {"name": k.get("name"), "scope": k.get("scope"), "created_at": k.get("created_at")}
            for k in self._read()
        ]

    def create_key(self, name: str, scope: str) -> str:
        """Generate, hash, and persist a new key; return the plaintext (shown exactly once).

        Args:
            name: A unique, non-empty name for the key. Cannot be
                :data:`DEFAULT_KEY_NAME` (reserved for the backward-compat key)
                or collide with an existing name.
            scope: One of :data:`SCOPES`.

        Returns:
            The plaintext key. This is the ONLY time it is ever available --
            only its SHA-256 hash is persisted, so it cannot be recovered
            afterward, only revoked and replaced with a new one.

        Raises:
            ValueError: invalid name/scope, reserved name, or a name collision.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        if name == DEFAULT_KEY_NAME:
            raise ValueError(
                f"{DEFAULT_KEY_NAME!r} is reserved for the backward-compat "
                "REMIND_ME_API_KEY / auto-generated key"
            )
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")

        with self._lock:
            keys = self._read()
            if any(k.get("name") == name for k in keys):
                raise ValueError(f"a key named {name!r} already exists")
            plaintext = token_urlsafe(_KEY_ENTROPY_BYTES)
            keys.append({
                "name": name,
                "key_hash": _hash_key(plaintext),
                "scope": scope,
                "created_at": _now_iso(),
            })
            self._write(keys)
        log.info("Created dashboard API key %r (scope=%s)", name, scope)
        return plaintext

    def revoke_key(self, name: str) -> bool:
        """Remove a named key. Returns False when the name is unknown.

        Raises:
            ValueError: attempting to revoke :data:`DEFAULT_KEY_NAME` -- that
                key is config-managed (env var / its own auto-generated
                file), not app-managed, and was never stored here anyway.
        """
        if name == DEFAULT_KEY_NAME:
            raise ValueError(
                f"{DEFAULT_KEY_NAME!r} is the backward-compat REMIND_ME_API_KEY / "
                "auto-generated key -- it is config-managed, not revocable through "
                "this store. Set REMIND_ME_API_KEY=disabled, or rotate it by "
                "deleting the persisted api_key file, instead."
            )
        with self._lock:
            keys = self._read()
            remaining = [k for k in keys if k.get("name") != name]
            if len(remaining) == len(keys):
                return False
            self._write(remaining)
        log.info("Revoked dashboard API key %r", name)
        return True

    def verify(self, presented: str) -> dict[str, Any] | None:
        """Constant-time-verify a presented plaintext key against every stored hash.

        Args:
            presented: The plaintext key from an Authorization header.

        Returns:
            ``{"name": ..., "scope": ...}`` for the first matching key, or
            None when no stored key's hash matches. Uses
            ``hmac.compare_digest`` per key (SE-05) -- comparing hex digests
            of equal, fixed length, so timing leaks no information about
            which key (if any) came close to matching.
        """
        presented_hash = _hash_key(presented)
        for k in self._read():
            stored_hash = k.get("key_hash")
            if isinstance(stored_hash, str) and hmac.compare_digest(stored_hash, presented_hash):
                return {"name": k.get("name"), "scope": k.get("scope")}
        return None


__all__ = [
    "DEFAULT_KEY_NAME",
    "SCOPES",
    "ApiKeyStore",
]
