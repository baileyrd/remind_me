"""
remind_me_mcp.oauth — single-user OAuth 2.1 authorization server (FT-07).

Implements the installed MCP SDK's ``OAuthAuthorizationServerProvider``
protocol (``mcp.server.auth.provider``) so the remote connector (FT-05) can
serve a real, spec-shaped authorization server instead of only the
secret-path URL. The SDK supplies the HTTP machinery — RFC 8414 AS metadata,
RFC 7591 dynamic client registration, the PKCE (S256, enforced)
authorization-code flow, refresh grant, and RFC 7009 revocation — via
``create_auth_routes`` / ``create_protected_resource_routes``; this module
supplies the policy:

  - **Single-user consent.** There are no accounts or sessions. The
    ``/authorize`` endpoint redirects to a ``/consent`` page where the owner
    pastes the FT-05 connector token (repurposed as the "owner credential")
    to approve the requesting client. The comparison is constant-time
    (``hmac.compare_digest``, SE-05); a wrong credential or an explicit deny
    both produce the same ``access_denied`` redirect, so the form never
    leaks which part failed. Every authorization re-prompts.
  - **No plaintext secrets at rest.** SHA-256 hashes of issued access/refresh
    tokens persist in a small JSON state file (``~/.remind-me/oauth.json``,
    0600 — SE-01 conventions); raw tokens are never written to disk.
    Registered clients are forced to ``token_endpoint_auth_method="none"``
    at registration (``register_client``) rather than storing a
    ``client_secret`` at all -- PKCE (S256) already provides proof of
    possession, so there's no secret whose plaintext-at-rest exposure would
    need weighing against the SDK's own equality-based verification. The
    file is re-read on every lookup, so deleting a client from another
    process (the ``remind_me_revoke_clients`` MCP tool running in stdio
    mode) revokes it on the live remote server too.
  - **Short-lived access, revocable everything.** Access tokens live
    ``ACCESS_TOKEN_TTL`` (1 h), refresh tokens ``REFRESH_TOKEN_TTL`` (30 d),
    authorization codes ``AUTH_CODE_TTL`` (5 min, single-use). The refresh
    grant rotates the refresh token. Revoking any token (RFC 7009) drops
    every token of that client; ``revoke_client`` additionally deletes the
    registration.
  - **Legacy coexistence.** ``load_access_token`` also accepts the FT-05
    connector token itself (constant-time), so the secret-path URL and
    legacy bearer clients keep working through the same SDK bearer
    middleware while OAuth is active.

Expiry checks go through the module-level ``_now()`` so tests can freeze the
clock deterministically. Starlette imports are kept lazy (inside the consent
handlers) so importing this module from MCP stdio mode (the revoke tool)
never loads the web framework.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

log = logging.getLogger("remind_me_mcp.oauth")

# ---------------------------------------------------------------------------
# Lifetimes (seconds)
# ---------------------------------------------------------------------------

ACCESS_TOKEN_TTL = 3600
"""Access tokens are short-lived — refresh is cheap and revocation windows stay small."""

REFRESH_TOKEN_TTL = 30 * 24 * 3600
"""Refresh tokens live 30 days and rotate on every refresh grant."""

AUTH_CODE_TTL = 300
"""Authorization codes expire after 5 minutes and are single-use."""

CONSENT_TTL = 600
"""A pending consent page is valid for 10 minutes, then the txn expires."""

CONSENT_PATH = "/consent"
"""Where the owner-credential consent form lives (the /authorize redirect target)."""

OWNER_CLIENT_ID = "owner"
"""Synthetic client_id reported for requests authenticated with the legacy
connector token. Never collides with registered clients (those get UUIDs)."""


def _now() -> float:
    """Current UNIX time. Module-level so tests can freeze the clock."""
    return time.time()


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of a token — what the state file stores instead of the secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Persistent state (clients + token hashes)
# ---------------------------------------------------------------------------

_EMPTY_STATE: dict[str, dict[str, Any]] = {
    "clients": {},
    "access_tokens": {},
    "refresh_tokens": {},
}


class OAuthStateStore:
    """JSON-file persistence for OAuth state (FT-07).

    Layout: ``{"clients": {client_id: <RFC 7591 record>}, "access_tokens":
    {sha256: {client_id, scopes, expires_at, resource}}, "refresh_tokens":
    {sha256: {client_id, scopes, expires_at}}}``. The file is created with
    0600 permissions (SE-01) and re-read on every operation, so a mutation
    from another process (the revoke tool in stdio mode) takes effect on the
    running remote server immediately. The in-process lock only serialises
    read-modify-write cycles; cross-process locking is out of scope for a
    single-user store.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        """Load state from disk, tolerating a missing or corrupt file."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return json.loads(json.dumps(_EMPTY_STATE))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read OAuth state at %s (%s); treating as empty", self.path, exc)
            return json.loads(json.dumps(_EMPTY_STATE))
        if not isinstance(raw, dict):
            return json.loads(json.dumps(_EMPTY_STATE))
        return {key: dict(raw.get(key) or {}) for key in _EMPTY_STATE}

    def _write(self, state: dict[str, dict[str, Any]]) -> None:
        """Persist state with 0600 permissions (token-file conventions, SE-01)."""
        try:
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
            self.path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            log.error("Could not persist OAuth state at %s: %s", self.path, exc)

    # -- clients ------------------------------------------------------------

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        """Return the stored registration record for *client_id*, or None."""
        record = self._read()["clients"].get(client_id)
        return dict(record) if isinstance(record, dict) else None

    def put_client(self, client_id: str, record: dict[str, Any]) -> None:
        """Insert or replace a client registration record."""
        with self._lock:
            state = self._read()
            state["clients"][client_id] = record
            self._write(state)

    def list_clients(self) -> list[dict[str, Any]]:
        """Summarise registered clients with live token counts (for the revoke tool)."""
        state = self._read()
        out: list[dict[str, Any]] = []
        for client_id, record in state["clients"].items():
            out.append(
                {
                    "client_id": client_id,
                    "client_name": record.get("client_name"),
                    "client_id_issued_at": record.get("client_id_issued_at"),
                    "redirect_uris": record.get("redirect_uris"),
                    "access_tokens": sum(
                        1 for meta in state["access_tokens"].values() if meta.get("client_id") == client_id
                    ),
                    "refresh_tokens": sum(
                        1 for meta in state["refresh_tokens"].values() if meta.get("client_id") == client_id
                    ),
                }
            )
        return out

    def revoke_client(self, client_id: str) -> dict[str, Any] | None:
        """Delete a client registration and every token it holds.

        Returns:
            A summary dict (client_id, client_name, token counts revoked),
            or None when the client_id is unknown.
        """
        with self._lock:
            state = self._read()
            record = state["clients"].pop(client_id, None)
            if record is None:
                return None
            counts = self._drop_tokens(state, client_id)
            self._write(state)
        log.info(
            "Revoked OAuth client %s (%s): %d access / %d refresh token(s) deleted",
            client_id,
            record.get("client_name") or "unnamed",
            counts["access_tokens"],
            counts["refresh_tokens"],
        )
        return {"client_id": client_id, "client_name": record.get("client_name"), **counts}

    # -- tokens ---------------------------------------------------------------

    @staticmethod
    def _drop_tokens(state: dict[str, dict[str, Any]], client_id: str) -> dict[str, int]:
        """Remove all of *client_id*'s token hashes from *state* (mutates), returning counts."""
        counts: dict[str, int] = {}
        for kind in ("access_tokens", "refresh_tokens"):
            doomed = [h for h, meta in state[kind].items() if meta.get("client_id") == client_id]
            for h in doomed:
                del state[kind][h]
            counts[kind] = len(doomed)
        return counts

    def put_token(self, kind: str, token: str, meta: dict[str, Any]) -> None:
        """Store *token*'s hash (never the raw secret) under access/refresh *kind*."""
        with self._lock:
            state = self._read()
            state[kind][_hash_token(token)] = meta
            self._write(state)

    def get_token(self, kind: str, token: str) -> dict[str, Any] | None:
        """Look up a raw token by hash; None when unknown."""
        meta = self._read()[kind].get(_hash_token(token))
        return dict(meta) if isinstance(meta, dict) else None

    def delete_token(self, kind: str, token: str) -> None:
        """Forget a raw token (no-op when unknown)."""
        with self._lock:
            state = self._read()
            if state[kind].pop(_hash_token(token), None) is not None:
                self._write(state)

    def delete_tokens_for_client(self, client_id: str) -> dict[str, int]:
        """Drop every access/refresh token of *client_id*, keeping the registration."""
        with self._lock:
            state = self._read()
            counts = self._drop_tokens(state, client_id)
            if counts["access_tokens"] or counts["refresh_tokens"]:
                self._write(state)
        return counts


# ---------------------------------------------------------------------------
# Provider (implements mcp.server.auth.provider.OAuthAuthorizationServerProvider)
# ---------------------------------------------------------------------------


@dataclass
class _PendingConsent:
    """An /authorize request waiting for the owner's decision on /consent."""

    client_id: str
    params: AuthorizationParams
    expires_at: float


_CONSENT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>remind_me — authorize connector</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 26rem; margin: 4rem auto; padding: 0 1rem; color: #1a1a1a; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1.5rem; }}
  input[type=password] {{ width: 100%; padding: .5rem; margin: .75rem 0; box-sizing: border-box; }}
  button {{ padding: .5rem 1.25rem; border-radius: 6px; border: 1px solid #888; cursor: pointer; }}
  button.approve {{ background: #1a7f37; color: #fff; border-color: #1a7f37; }}
  code {{ background: #f4f4f4; padding: 0 .25rem; }}
</style></head><body>
<div class="card">
  <h2>Authorize connector</h2>
  <p><strong>{client}</strong> wants to access your remind_me memory store.</p>
  <p>Redirects to: <code>{redirect}</code></p>
  <form method="post" action="consent">
    <input type="hidden" name="txn" value="{txn}">
    <label for="owner_token">Owner token (from <code>~/.remind-me/connector_token</code>)</label>
    <input type="password" id="owner_token" name="owner_token" autocomplete="off" autofocus>
    <button class="approve" name="action" value="approve">Approve</button>
    <button name="action" value="deny">Deny</button>
  </form>
</div>
</body></html>
"""

_EXPIRED_HTML = (
    "<!doctype html><html><body><h2>Authorization request expired</h2>"
    "<p>This consent link is no longer valid. Retry the connection from your client.</p>"
    "</body></html>"
)


class SingleUserOAuthProvider:
    """Single-user OAuth 2.1 provider over :class:`OAuthStateStore` (FT-07).

    Satisfies the SDK's ``OAuthAuthorizationServerProvider`` protocol — the
    SDK handlers own request parsing, PKCE (S256) verification, client
    authentication, redirect_uri pinning, and code/refresh expiry checks;
    this class owns issuance, storage, the owner-credential consent step,
    and revocation semantics.
    """

    def __init__(self, owner_token: str, store: OAuthStateStore) -> None:
        self.owner_token = owner_token
        self.store = store
        # Authorization codes and pending consents are deliberately
        # process-local: both legs of each exchange hit the same process,
        # and losing them on restart only means re-running /authorize.
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, _PendingConsent] = {}

    # -- client registry (RFC 7591) -------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Load a registered client from the state file."""
        record = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a dynamically-registered client.

        Forces token_endpoint_auth_method="none" (no client_secret),
        overriding whatever the SDK's registration handler defaulted to --
        it defaults to client_secret_post and auto-generates a secret
        whenever the registering client doesn't explicitly request "none"
        (mcp.server.auth.handlers.register). That secret would otherwise be
        the one credential this module persists in plaintext: unlike access/
        refresh tokens (SHA-256 hashed below), the SDK's own client-auth
        middleware compares client.client_secret against the presented value
        with a direct equality check, so storing only a hash would make
        every subsequent client_secret_post/basic request fail -- there's no
        way to verify a plaintext-in, hash-at-rest secret without patching
        the SDK's comparison itself.

        Removing the secret isn't a downgrade here: PKCE (S256) is already
        mandatory for every authorization-code exchange, giving proof-of-
        possession without a client secret, and the real trust boundary for
        this single-user server is the owner-token consent step (SE-05),
        not client confidentiality. The mutated client_info is also what the
        registration handler returns to the caller, so the registering
        client is told "none" up front and never expects to present a
        secret it was never given.
        """
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None

        client_id = client_info.client_id or ""
        self.store.put_client(client_id, client_info.model_dump(mode="json"))
        log.info(
            "Registered OAuth client %s (%s) — redirect_uris=%s",
            client_id,
            client_info.client_name or "unnamed",
            [str(u) for u in client_info.redirect_uris or []],
        )

    # -- authorization + consent ----------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the user-agent to the owner-consent form."""
        self._prune_pending()
        txn = secrets.token_urlsafe(32)
        self._pending[txn] = _PendingConsent(
            client_id=client.client_id or "",
            params=params,
            expires_at=_now() + CONSENT_TTL,
        )
        return f"{CONSENT_PATH}?txn={txn}"

    def _prune_pending(self) -> None:
        """Drop expired consent transactions (lazy GC)."""
        cutoff = _now()
        for txn in [t for t, p in self._pending.items() if p.expires_at < cutoff]:
            del self._pending[txn]

    async def handle_consent_page(self, request: Request) -> Response:
        """GET /consent — render the owner-credential approval form."""
        from starlette.responses import HTMLResponse

        txn = request.query_params.get("txn", "")
        pending = self._pending.get(txn)
        if pending is None or pending.expires_at < _now():
            return HTMLResponse(_EXPIRED_HTML, status_code=400)
        client = await self.get_client(pending.client_id)
        name = (client.client_name if client else None) or pending.client_id
        return HTMLResponse(
            _CONSENT_HTML.format(
                client=html.escape(name),
                redirect=html.escape(str(pending.params.redirect_uri)),
                txn=html.escape(txn),
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def handle_consent_submit(self, request: Request) -> Response:
        """POST /consent — approve (owner token matches) or deny the pending request.

        The txn is single-use: it is consumed here whatever the outcome. A
        wrong owner credential and an explicit deny produce the identical
        ``access_denied`` redirect — the trust boundary never explains itself.
        """
        from starlette.responses import HTMLResponse, RedirectResponse

        form = await request.form()
        txn = str(form.get("txn") or "")
        pending = self._pending.pop(txn, None)
        if pending is None or pending.expires_at < _now():
            return HTMLResponse(_EXPIRED_HTML, status_code=400)

        params = pending.params
        supplied = str(form.get("owner_token") or "")
        approved = str(form.get("action") or "") == "approve" and hmac.compare_digest(
            supplied.encode("utf-8"), self.owner_token.encode("utf-8")
        )
        if not approved:
            log.warning("OAuth consent denied for client %s", pending.client_id)
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=_now() + AUTH_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        log.info("OAuth consent granted for client %s", pending.client_id)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # -- authorization-code exchange -------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Look up an issued code (the SDK handler verifies expiry, PKCE, client)."""
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Consume the (single-use) code and issue an access + refresh token pair."""
        self._codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # -- refresh grant ----------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Look up a refresh token by hash; expired or foreign tokens read as absent."""
        meta = self.store.get_token("refresh_tokens", refresh_token)
        if meta is None or meta.get("client_id") != client.client_id:
            return None
        expires_at = meta.get("expires_at")
        if expires_at is not None and expires_at < _now():
            self.store.delete_token("refresh_tokens", refresh_token)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=str(meta["client_id"]),
            scopes=list(meta.get("scopes") or []),
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate: retire the presented refresh token, issue a fresh pair."""
        self.store.delete_token("refresh_tokens", refresh_token.token)
        return self._issue_tokens(client_id=refresh_token.client_id, scopes=scopes, resource=None)

    # -- bearer verification -------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token: an issued OAuth access token OR the legacy connector token.

        The legacy acceptance is what keeps the FT-05 secret-path URL and
        header-capable bearer clients working while OAuth is active — both
        funnel through the same SDK bearer middleware.
        """
        if hmac.compare_digest(token.encode("utf-8"), self.owner_token.encode("utf-8")):
            return AccessToken(token=token, client_id=OWNER_CLIENT_ID, scopes=[], expires_at=None)
        meta = self.store.get_token("access_tokens", token)
        if meta is None:
            return None
        expires_at = meta.get("expires_at")
        if expires_at is not None and expires_at < _now():
            self.store.delete_token("access_tokens", token)
            return None
        return AccessToken(
            token=token,
            client_id=str(meta["client_id"]),
            scopes=list(meta.get("scopes") or []),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=meta.get("resource"),
        )

    # -- revocation (RFC 7009) -------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token — and, per the RFC's SHOULD, the client's whole session.

        Single-user pragmatics: presenting either half of a client's
        credential pair kills every token that client holds (the
        registration survives, so the client can re-authorize).
        """
        counts = self.store.delete_tokens_for_client(token.client_id)
        log.info(
            "Revoked tokens for OAuth client %s: %d access / %d refresh",
            token.client_id,
            counts["access_tokens"],
            counts["refresh_tokens"],
        )

    # -- issuance ----------------------------------------------------------------

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        """Mint an access + refresh token pair and persist their hashes."""
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = int(_now())
        self.store.put_token(
            "access_tokens",
            access_token,
            {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + ACCESS_TOKEN_TTL,
                "resource": resource,
            },
        )
        self.store.put_token(
            "refresh_tokens",
            refresh_token,
            {"client_id": client_id, "scopes": scopes, "expires_at": now + REFRESH_TOKEN_TTL},
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_token,
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "ACCESS_TOKEN_TTL",
    "AUTH_CODE_TTL",
    "CONSENT_PATH",
    "CONSENT_TTL",
    "OWNER_CLIENT_ID",
    "REFRESH_TOKEN_TTL",
    "OAuthStateStore",
    "SingleUserOAuthProvider",
]
