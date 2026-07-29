# Migrating remind-me-mcp to MCP Python SDK 2.0

Status: Proposed
Date: 2026-07-29
Current pin: `mcp[cli]>=1.0.0` (resolves to `1.28.1` in `uv.lock`)
Target: `mcp` 2.0.0 (released 2026-07-28)

## TL;DR

`mcp.server.fastmcp` **no longer exists** in 2.0. Every module in this package
fails to import, because all of them transitively reach `server.py:18`. The
package is 100% non-functional on 2.0 today.

The good news: the break is concentrated. The auth stack — which is the most
intricate thing we build on — is **completely unchanged**. The real port is one
rename plus two configuration call-sites.

There is no deadline pressure to migrate. There *is* deadline pressure to
**pin**, because the unbounded `>=1.0.0` specifier means the next
`remind_me_self_update` silently installs 2.0 and bricks the server.

## How this was verified

Not from changelogs. Both SDK versions were installed into throwaway venvs, this
package was installed against 2.0.0, and every import site was probed directly.
All findings below are observed behavior, not inference.

## Blast radius

Every module fails at the same line:

```
FAIL  remind_me_mcp.server     ModuleNotFoundError: No module named 'mcp.server.fastmcp'
FAIL  remind_me_mcp.tools          at server.py:18  from mcp.server.fastmcp import FastMCP
FAIL  remind_me_mcp.oauth
FAIL  remind_me_mcp.remote
FAIL  remind_me_mcp.__main__
FAIL  remind_me_mcp.api
FAIL  remind_me_mcp.sync
```

Both entry points die: MCP stdio (`mcp.run()`, `__main__.py:454`) and the remote
connector (`build_remote_app`). So does the dashboard, since `api.py` imports the
same chain.

## What actually changed

### 1. `FastMCP` → `MCPServer` (the whole break)

| 1.28.1 | 2.0.0 |
| --- | --- |
| `mcp.server.fastmcp.FastMCP` | `mcp.server.mcpserver.MCPServer` |
| `mcp.server.fastmcp.server.StreamableHTTPASGIApp` | `mcp.server.streamable_http_manager.StreamableHTTPASGIApp` |

There is no compatibility alias — `import mcp.server.fastmcp` raises
`ModuleNotFoundError` outright.

`MCPServer` keeps the methods we depend on, with the same names: `tool`, `run`,
`streamable_http_app`, `session_manager`, `custom_route`, `call_tool`, and the
`lifespan=` constructor kwarg. So `app_lifespan` and all **44** `@mcp.tool()`
registrations across `remind_me_mcp/tools/` port unchanged — `tool()`'s
signature in 2.0 is a strict superset of 1.28's.

### 2. `settings` was gutted — config moved to explicit kwargs

This is the subtle one, and the part most likely to be missed by a
search-and-replace port. `mcp.settings` still exists in 2.0, so the attribute
access *looks* fine, but the fields we use are gone:

```
1.28.1 Settings fields: auth, debug, dependencies, host, json_response, lifespan,
                        log_level, message_path, mount_path, port, sse_path,
                        stateless_http, streamable_http_path, transport_security,
                        warn_on_duplicate_{prompts,resources,tools}

2.0.0  Settings fields: auth, debug, dependencies, lifespan, log_level,
                        warn_on_duplicate_{prompts,resources,tools}
```

Observed failure modes:

```
mcp.settings.host = "0.0.0.0"        -> ValueError: "Settings" object has no field "host"
mcp.settings.streamable_http_path    -> AttributeError: 'Settings' object has no attribute ...
```

Transport config is now passed per-call instead:

```python
streamable_http_app(*, streamable_http_path='/mcp', json_response=False,
                    stateless_http=False, event_store=None, retry_interval=None,
                    max_request_body_size=4194304,
                    transport_security=None, host='127.0.0.1') -> Starlette

run_streamable_http_async(*, host='127.0.0.1', port=8000, streamable_http_path='/mcp',
                          ..., transport_security=None) -> None
```

Affected call-sites:

- `__main__.py:412-414` — `mcp.settings.host` / `.port` assignment, then
  `mcp.run(transport="streamable-http")`. Both assignments now raise.
- `remote.py:219-228` — `mcp.settings.transport_security = ...` (raises), and
  `mcp.settings.streamable_http_path` read (raises).

Note that `remote.py:219`'s comment ("Must be set before the first
`streamable_http_app()` call") describes an ordering hazard that **stops
existing** in 2.0 — config is now an argument to that call, so it can't be set
too late. That comment should be deleted, not ported.

### 3. `call_tool` changed shape — breaks the OTEL tracing subclass

```python
# 1.28.1
async def call_tool(self, name, arguments) -> Sequence[ContentBlock] | dict[str, Any]

# 2.0.0
async def call_tool(self, name, arguments, context=None) -> CallToolResult | InputRequiredResult
```

`_TracedFastMCP` (`server.py:32-52`) overrides this to wrap every tool call in an
OTEL span. The override needs a new signature *and* a new return annotation. The
docstring's reasoning — that subclassing is the only reliable interception point,
because `__init__` binds `self.call_tool` as the protocol handler during
construction — still holds in 2.0 and should be preserved.

`InputRequiredResult` in the return union is new: 2.0 has an elicitation flow
that can return mid-call. The span wrapper is agnostic to which variant comes
back, so this costs nothing today, but it's worth knowing the union widened.

### 4. Dependency shifts

- **`httpx` → `httpx2`.** The SDK now requires `httpx2>=2.5.0`. This sounds
  alarming and isn't: it's a **separate distribution with a separate import
  name**, so our own `httpx>=0.25.0` (used throughout `sync.py`) is untouched.
  Verified coexisting in one venv: `httpx 0.28.1` + `httpx2 2.9.1`. No code
  change needed — just a fatter venv.
- **`mcp-types` is now its own pinned distribution** (`mcp-types==2.0.0`).
  `from mcp.types import ContentBlock` (`server.py:27`) still resolves.
- **Starlette 1.3.1** gets pulled in (a major bump from the 0.4x line). All
  **11** distinct `starlette.*` imports across this package were probed against
  1.3.1 and every one resolves, including
  `starlette.middleware.authentication.AuthenticationMiddleware`. Not a blocker,
  but it lands in the same upgrade and deserves its own test pass.
- Removed from the SDK: `websocket.py`, `experimental/`. We use neither.

### 5. What did NOT change — the auth stack

Every OAuth 2.1 / bearer-auth import in `oauth.py` and `remote.py` resolves
identically under 2.0:

```
ok  mcp.shared.auth                          OAuthClientInformationFull, OAuthToken
ok  mcp.server.auth.provider                 ProviderTokenVerifier, OAuthAuthorizationServerProvider,
                                             AuthorizationParams, AuthorizationCode, RefreshToken, AccessToken
ok  mcp.server.auth.settings                 ClientRegistrationOptions, RevocationOptions, AuthSettings
ok  mcp.server.auth.routes                   create_auth_routes, cors_middleware, create_protected_resource_routes
ok  mcp.server.auth.middleware.auth_context  AuthContextMiddleware
ok  mcp.server.auth.middleware.bearer_auth   BearerAuthBackend, RequireAuthMiddleware
ok  mcp.server.transport_security            TransportSecuritySettings
ok  mcp.types                                ContentBlock
```

`SingleUserOAuthProvider` and the whole FT-07 flow — RFC 8414 / 9728 / 7591,
PKCE S256, RFC 7009 revocation — should port with zero changes. This is what
makes the migration tractable.

## Phased plan

### Phase 0 — Pin now (urgent, independent of migrating)

The live risk. `pyproject.toml:9` declares `mcp[cli]>=1.0.0` with no upper
bound, and both the install path (README:79) and `self_update`
(`updater.py:420`) run `pip install -e .`, which **ignores `uv.lock`** and
resolves freely. The next self-update installs 2.0.0 and the server stops
importing.

```toml
"mcp[cli]>=1.28,<2",
```

Ship this on its own, ahead of any migration work. It is the difference between
migrating on purpose and migrating at 2am because the server died.

While here, audit the other unbounded specifiers — `starlette>=0.40.0` has the
same shape of exposure and silently accepted a major bump to 1.3.1 during
testing.

### Phase 1 — Decide whether to migrate at all

The 1.x line is still maintained: **1.29.0 shipped the same day as 2.0.0**
(2026-07-28). Pinned to `<2`, we can sit on 1.x indefinitely and keep taking
patches.

Migrate when there's a reason to — a 2.0-only feature we want, or 1.x going into
maintenance-only. Two things in 2.0 look genuinely relevant to this project and
are worth evaluating before deciding:

- `mcp.server.apps` — an app/UI extension surface, potentially interesting for
  the dashboard.
- `caching.py`, `subscriptions.py`, `request_state.py` — new server-side
  primitives; `subscriptions.py` in particular may bear on the sync/watcher path.

Recommendation: **pin now, migrate deliberately.** Nothing about 2.0 is urgent.

### Phase 2 — Port the core (when Phase 1 says go)

1. `server.py:18` — swap the import, rename `_TracedFastMCP` → `_TracedMCPServer`
   (or keep the name and drop the "FastMCP" wording from the docstring).
2. `server.py:32-52` — update the `call_tool` override's signature and return
   type. Keep the subclassing rationale in the docstring.
3. `server.py:138` — `mcp = _TracedMCPServer("remind_me_mcp", lifespan=app_lifespan)`
   is unchanged apart from the class name.
4. `remote.py:268` — repoint `StreamableHTTPASGIApp` to
   `mcp.server.streamable_http_manager`.
5. Type-only import at `server.py:27` (`ContentBlock`) may need widening to the
   new `CallToolResult` union.

### Phase 3 — Port the transport config

1. `__main__.py:411-414` — replace `mcp.settings.host/.port` assignment with
   `mcp.run_streamable_http_async(host=..., port=...)`, or pass through
   `mcp.run(transport="streamable-http", host=..., port=...)` via `**kwargs`.
   Confirm which the 2.0 `run()` actually forwards; prefer the explicit async
   form.
2. `remote.py:219-228` — pass `transport_security=` and read the path from our
   own config rather than `mcp.settings`. `mcp_path` currently derives from
   `mcp.settings.streamable_http_path`; in 2.0 we own that value and pass it in,
   so it should become a module constant used for both the `streamable_http_app()`
   kwarg and the route mounting. Delete the now-obsolete ordering comment.
3. Re-verify the SE-03 lifespan-delegation pattern still holds — `remote.py`
   delegates its lifespan to the MCP sub-app so the DB/sync/watcher lifecycle
   matches stdio mode. This is the highest-risk behavioral area of the port and
   `CODE_REVIEW.md:82-86` already flags the double-lifespan hazard.
4. `session_manager` is now **lazy** and raises
   `RuntimeError: Session manager can only be accessed after calling streamable_http_app()`.
   `remote.py` already calls `streamable_http_app()` (line 227) before touching
   `session_manager` (line 314), so current ordering is safe — but this is now
   load-bearing and should get an explicit test.

### Phase 4 — Test and verify

- `tests/test_main.py:242-270` asserts `mcp.run(transport="streamable-http")` is
  called with exactly `{"transport": "streamable-http"}`. That assertion changes
  shape once host/port move into the call.
- `tests/test_main.py:547-561` and `tests/test_remote.py:175` exercise a real
  StreamableHTTP session and session-id assignment — these are the tests that
  will actually catch a botched transport-config port. Run them first.
- Add a CI job pinned to the 2.0 line so drift surfaces in CI, not in
  production.
- Verify both entry points by hand: stdio against Claude Code, and the remote
  connector against claude.ai (OAuth mode *and* secret-path fallback, since they
  share one app).

### Phase 5 — Harden self-update

The root cause of the exposure isn't the version — it's that `self_update` can
change the dependency graph without a gate. Worth considering:

- Have `updater.py` install against `uv.lock` (`uv sync`) rather than resolving
  fresh, so the lockfile stops being decorative.
- Or add a post-install import smoke-check that rolls back on `ImportError`.
  `updater.py:493` already has rollback machinery for the failed-install case;
  this extends it to the installed-but-broken case.

## Effort estimate

| Phase | Scope | Risk |
| --- | --- | --- |
| 0 — Pin | 1 line | None. Do it today. |
| 2 — Core port | ~4 sites in 2 files | Low — mechanical rename |
| 3 — Transport config | ~2 sites in 2 files | **Medium — the real work** |
| 4 — Tests | 3 test files + CI | Medium |
| 5 — Updater hardening | `updater.py` | Low, optional |

The 44 tool registrations, the entire OAuth stack, and the dashboard need no
changes at all. Phase 3 is where the actual thinking is: transport
configuration moved from mutable global state to call-time arguments, and the
lifespan delegation in `remote.py` is the part most likely to break quietly.
