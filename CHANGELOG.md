# Changelog

All notable changes to this repo are documented here.
Format: Added / Changed / Deprecated / Removed / Fixed / Security, newest first.

## [Unreleased]
### Added
- Hub: `HUB_VERSION`, reported by `GET /health` (unauthenticated), `GET /count` and `GET /stats`.
- Hub: `GET /count` — bearer-authenticated scalar record counts with no `GROUP BY`, optionally narrowed with `?table=`; `memories` splits `live` from `tombstones`.
- MCP: the installed package version is reported by the dashboard's `GET /health`, the peer server's `GET /health`, `remind_me_sync_status`, and `remind_me_sync_reconcile` (which also reports `hub_version`).
- Hub: `GET /metrics` (Prometheus, bearer-gated, `REMIND_ME_HUB_METRICS_ENABLED`), `X-Hub-Version` on every response, `?approx=1` planner-estimate counts, and `?since=`/`?by=origin_node` filters on `/count`.
- Hub: OCI image labels and version-tagged images, so a stopped container can be identified and a rollback is a retag.
- MCP: `remind_me_build_info{version=...}` metric; version in `remind_me_server_status` and `--status`; version shown in the dashboard header.
- MCP: `GET /count` on the peer server and a `remind_me_sync_reconcile_peer` tool, so peer drift is observable; `remind_me_sync_reconcile` gains an opt-in `quick` pre-check.
- CI: `hub-e2e` job runs `hub/e2e_test.py` against a live Postgres — the hub's first automatic runtime coverage — plus a guard requiring a `HUB_VERSION` bump when `hub/main.py` changes.
### Changed
- MCP: `__version__` now resolves in `remind_me_mcp/version.py` so HTTP surfaces can import it without cycling through the package root; `from remind_me_mcp import __version__` still works.
### Fixed
- Hub: `setup.sh update` reported success without checking that the new build was actually serving.
- Hub e2e: two assertions expected exactly one entity, unsatisfiable since entity relations were added.
### Security
- Hub: FastAPI's `/docs`, `/redoc` and `/openapi.json` were served unauthenticated, advertising every route including `POST /admin/compact_tombstones`. Disabled.

<!-- ## [0.1.0] - YYYY-MM-DD
### Added
- Initial release -->
