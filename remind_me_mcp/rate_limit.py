"""
remind_me_mcp.rate_limit — in-memory, dependency-free rate limiting (issue #183).

Protects the two surfaces this server can expose over a public tunnel from
being flooded: the push/webhook ingestion endpoint (``webhook_server.py``'s
``POST /ingest``) and the remote MCP connector (``remote.py``'s Streamable
HTTP endpoint). Neither issue #141 (webhook_server's ingest handler lacked an
exception guard) nor issue #142 (no socket timeout on peer_server.py/
webhook_server.py) is addressed here -- both are open, adjacent, already-filed
gaps; this module is deliberately scoped to rate limiting only.

Single-process, single-node only, matching this whole codebase's
(ARCHITECTURE.md) non-goals -- no distributed rate limiting, no shared
counters across machines. A plain in-memory dict guarded by a
``threading.Lock`` is sufficient and needs no new dependency (no Redis, no
extra pip package).

**Why a threading.Lock, not asyncio.Lock:** ``RateLimiter.hit()`` must be
callable from two different concurrency models -- ``webhook_server.py``'s
``ThreadingHTTPServer`` (one OS thread per connection, fully synchronous) and
``remote.py``'s Starlette/ASGI app (coroutines on a single asyncio event-loop
thread). A ``threading.Lock`` is correct in *both*: CPython's GIL means the
critical section (a handful of dict reads/writes, no I/O, nothing ever
``await``-ed while the lock is held) can't be preempted by another thread
mid-update, and taking a plain (non-async) lock from inside a coroutine never
blocks the event loop for longer than that same tiny critical section takes
to run -- there is nothing to `await` inside it. That means one
implementation serves both call sites, the same way ``api.BearerAuthMiddleware``
is already shared between a threaded and an async caller (SE-05).

**Keying:** prefer a verified, statically-known credential over the caller's
IP wherever one can be checked cheaply and synchronously -- the webhook
secret (``REMIND_ME_WEBHOOK_SECRET``), or the remote connector's static/
legacy connector token (checked against either the secret-path segment or
the ``Authorization`` header, whichever the caller used). A request that
presents *that exact* credential lands in one dedicated, generous bucket, so
retries from the server's own legitimate high-volume caller are never
throttled by unrelated traffic sharing the caller's apparent IP -- which
matters a lot for these two routes specifically, since both live behind an
HTTPS tunnel by default, and every remote request typically arrives at the
bind address as the *tunnel's own loopback forwarding connection* -- i.e.
every external caller, well-behaved or not, can appear to share one IP.
Everything else (no credential, a wrong one, or -- in ``remote.py``'s OAuth
mode -- a dynamically-issued per-client access token this module does not
itself verify) falls back to an IP-keyed bucket.

That fallback is deliberate, not an oversight: this module never mints a
fresh bucket for an *unverified* presented credential. If it did, an
attacker could send a different random ``Authorization`` value on every
request and get a brand-new quota each time -- defeating the limiter
entirely. The one real cost of that choice: several legitimate OAuth clients
(e.g. two devices under the same single-user owner, see ``oauth.py``) that
don't present the legacy static token end up sharing the IP-keyed bucket
with each other and with anonymous traffic, rather than each getting a
dedicated bucket. Verifying an OAuth access token here would mean
duplicating ``SingleUserOAuthProvider.load_access_token``'s async,
file-backed lookup ahead of the SDK's own auth stack purely for rate-limit
bookkeeping -- not worth the added coupling and I/O for a single-owner
server where that shared bucket's default (60 req/60s) is already generous
for a person's own handful of devices.
"""

from __future__ import annotations

import hmac
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from remind_me_mcp import metrics

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_LIMIT = 60
"""Default REMIND_ME_RATE_LIMIT_REQUESTS -- generous enough that normal
MCP/webhook traffic from a single owner's devices never trips it (sync
traffic uses its own hub/peer protocol -- peer_server.py -- entirely
separate from the two routes this module guards, so SYNC_INTERVAL's cadence
is irrelevant here)."""

DEFAULT_WINDOW_SECONDS = 60
"""Default REMIND_ME_RATE_LIMIT_WINDOW_SECONDS."""


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of one :meth:`RateLimiter.hit` call."""

    allowed: bool
    retry_after: float
    """Seconds until the caller's current window resets. 0 when allowed."""


class RateLimiter:
    """Fixed-window, in-memory, thread-safe rate limiter.

    Not distributed: each process keeps its own counters entirely in memory
    (see module docstring). A single ``threading.Lock`` guards the shared
    dict; the critical section never performs I/O or blocks, so it is cheap
    enough to hold from either a worker thread or an asyncio coroutine
    without becoming a bottleneck.

    Stale buckets (whose window has fully elapsed) are pruned lazily, on an
    ordinary call to :meth:`hit`, every ``prune_every`` calls -- there is no
    separate cleanup thread. This keeps the dict from growing unboundedly
    across a long-running server that sees many distinct IPs, at the cost of
    an occasional slightly-more-expensive call instead of a background
    thread with its own lifecycle to start/stop/join.

    Args:
        limit: Maximum hits allowed per key within one window.
        window_seconds: Window length in seconds.
        clock: Zero-arg callable returning the current time as a float
            (monotonically non-decreasing). Defaults to ``time.monotonic``.
            Exposed as the public ``self.clock`` attribute so tests can
            freeze it deterministically instead of sleeping real time --
            mirrors the "clock override" convention already used elsewhere
            in this codebase (e.g. ``vitality.build_vitality_report``'s
            ``now`` parameter).
        prune_every: Sweep for fully-expired keys once every this many
            ``hit()`` calls.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] | None = None,
        prune_every: int = 128,
    ) -> None:
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.clock: Callable[[], float] = clock or time.monotonic
        self.prune_every = prune_every
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._hits_since_prune = 0

    def hit(self, key: str) -> RateLimitResult:
        """Record one hit for *key*, returning whether it's within the limit.

        A call that is itself rejected (over the limit) does NOT count
        towards a future window -- the stored count is left unchanged, so a
        client that backs off and retries after ``retry_after`` seconds sees
        a clean new window rather than one already primed by its own
        rejected attempts.
        """
        now = self.clock()
        with self._lock:
            self._maybe_prune_locked(now)
            window_start, count = self._buckets.get(key, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start, count = now, 0
            if count >= self.limit:
                self._buckets[key] = (window_start, count)
                retry_after = max(0.0, self.window_seconds - (now - window_start))
                # Issue #197: the single choke point both rate-limited routes
                # (webhook_server.py's POST /ingest, remote.py's Streamable
                # HTTP endpoint) already flow through -- see metrics.py's
                # record_rate_limit_rejection docstring. No-op unless
                # REMIND_ME_METRICS_ENABLED is set.
                metrics.record_rate_limit_rejection()
                return RateLimitResult(allowed=False, retry_after=retry_after)
            self._buckets[key] = (window_start, count + 1)
            return RateLimitResult(allowed=True, retry_after=0.0)

    def _maybe_prune_locked(self, now: float) -> None:
        """Drop fully-expired buckets every ``prune_every`` calls (caller holds ``self._lock``)."""
        self._hits_since_prune += 1
        if self._hits_since_prune < self.prune_every:
            return
        self._hits_since_prune = 0
        stale = [k for k, (window_start, _count) in self._buckets.items() if now - window_start >= self.window_seconds]
        for k in stale:
            del self._buckets[k]

    def reset(self) -> None:
        """Discard all buckets (test helper / operator hook)."""
        with self._lock:
            self._buckets.clear()
            self._hits_since_prune = 0


def resolve_key(presented: str, remote_addr: str, known_secret: str | None) -> str:
    """Resolve the rate-limit bucket key for one request (see module docstring).

    Args:
        presented: The credential the caller actually presented for this
            request -- e.g. the raw ``Authorization`` header value, or (for
            ``remote.py``'s secret-path mode) the path segment that would
            otherwise be compared against the connector token. May be empty.
        remote_addr: The caller's IP address (Starlette's
            ``request.client.host`` / ``BaseHTTPRequestHandler``'s
            ``self.client_address[0]``), used as the fallback key. May be
            empty/unknown (e.g. a Unix socket) -- callers should pass
            ``""`` in that case, which resolves to a shared ``ip:unknown``
            bucket rather than raising.
        known_secret: The single well-known secret this route accepts
            (``WEBHOOK_SECRET`` / the connector token), or ``None`` when
            there isn't one this call site can cheaply verify (never the
            case for the two routes this module currently guards, but kept
            optional so a caller can explicitly opt out of the "known"
            bucket).

    Returns:
        ``"auth:known"`` when *presented* matches *known_secret* exactly
        (constant-time compare, SE-05 convention); otherwise
        ``"ip:<remote_addr>"``.
    """
    if known_secret and presented and hmac.compare_digest(
        presented.encode("utf-8"), known_secret.encode("utf-8")
    ):
        return "auth:known"
    return f"ip:{remote_addr or 'unknown'}"


def retry_after_seconds(retry_after: float) -> int:
    """Round a fractional retry-after duration up to a whole positive second.

    Used for the ``Retry-After`` response header, which is defined in whole
    seconds. Never returns 0 -- a client that retries immediately after
    seeing ``Retry-After: 0`` would likely hit the limiter again before the
    window has actually rolled over.
    """
    return max(1, math.ceil(retry_after))


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_WINDOW_SECONDS",
    "RateLimitResult",
    "RateLimiter",
    "resolve_key",
    "retry_after_seconds",
]
