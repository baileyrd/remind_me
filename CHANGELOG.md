# Changelog

All notable changes to this repo are documented here.
Format: Added / Changed / Deprecated / Removed / Fixed / Security, newest first.

## [Unreleased]
### Added
- Hub: `HUB_VERSION`, reported by `GET /health` (unauthenticated), `GET /count` and `GET /stats`.
- Hub: `GET /count` — bearer-authenticated scalar record counts with no `GROUP BY`, optionally narrowed with `?table=`; `memories` splits `live` from `tombstones`.
- MCP: the installed package version is reported by the dashboard's `GET /health`, the peer server's `GET /health`, `remind_me_sync_status`, and `remind_me_sync_reconcile` (which also reports `hub_version`).
### Changed
- MCP: `__version__` now resolves in `remind_me_mcp/version.py` so HTTP surfaces can import it without cycling through the package root; `from remind_me_mcp import __version__` still works.
### Fixed
### Security

<!-- ## [0.1.0] - YYYY-MM-DD
### Added
- Initial release -->
