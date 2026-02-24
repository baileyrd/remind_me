# Requirements: Remind Me MCP

**Defined:** 2026-02-24
**Core Value:** Persistent, searchable memory across all Claude interfaces — modular, tested, maintainable

## v1.1 Requirements

Requirements for tech debt milestone. Each maps to roadmap phases.

### Security

- [ ] **SEC-01**: Dashboard API restricts CORS to localhost origins only (both `127.0.0.1` and `localhost`)
- [ ] **SEC-02**: Import API restricts file paths to within user's home directory (configurable via `REMIND_ME_IMPORT_ROOTS` env var)
- [ ] **SEC-03**: Optional API auth via `REMIND_ME_API_KEY` env var — Bearer token on all `/api/*` routes when set, no-op when unset

### CI/CD

- [ ] **CICD-01**: GitHub Actions workflow runs ruff lint and pytest on push/PR for Python 3.11 and 3.12
- [ ] **CICD-02**: Coverage enforcement gate at 80% minimum via pytest-cov

### Code Quality

- [ ] **QUAL-01**: All ruff warnings resolved (26 auto-fix + 4 manual)
- [ ] **QUAL-02**: Broad `except Exception` narrowed to specific types in embeddings.py, pid.py, and updater.py
- [ ] **QUAL-03**: Original monolith file (`remind_me_mcp_original.py`) removed from repository

### Embedding Parity

- [ ] **EMBD-01**: REST API `POST /api/memories` generates semantic embeddings on create (matching MCP tool behavior)
- [ ] **EMBD-02**: REST API `PUT /api/memories/{id}` regenerates semantic embeddings on content update (matching MCP tool behavior)

### Performance

- [ ] **PERF-01**: Reindex tool processes embeddings in batches of 32 using `embedder.embed()` list API
- [ ] **PERF-02**: Directory import processes files concurrently with semaphore-bounded parallelism

## Future Requirements

Deferred to future release. Tracked but not in current roadmap.

### Security

- **SEC-04**: HTTPS/TLS support for dashboard
- **SEC-05**: Rate limiting on API endpoints

### Performance

- **PERF-03**: REST API semantic search endpoint (`/api/memories/semantic-search`)

### Code Quality

- **QUAL-04**: mypy strict mode enforcement
- **QUAL-05**: REST API semantic search parity (FTS5 only currently)

## Out of Scope

| Feature | Reason |
|---------|--------|
| PostgreSQL migration | SQLite WAL sufficient for personal use (per PROJECT.md constraint) |
| Vite/esbuild build step | Babel standalone preserved (per PROJECT.md constraint) |
| Split into separate packages | Single pip install preserved (per PROJECT.md constraint) |
| Full OAuth2/JWT auth | Static bearer token sufficient for personal localhost tool |
| Rate limiting | Single-user personal tool; no multi-tenant scenario |
| HTTPS/TLS | Localhost traffic; self-signed certs add complexity with no benefit |
| pre-commit hooks | High commit churn during refactor; CI gates provide equivalent protection |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SEC-01 | — | Pending |
| SEC-02 | — | Pending |
| SEC-03 | — | Pending |
| CICD-01 | — | Pending |
| CICD-02 | — | Pending |
| QUAL-01 | — | Pending |
| QUAL-02 | — | Pending |
| QUAL-03 | — | Pending |
| EMBD-01 | — | Pending |
| EMBD-02 | — | Pending |
| PERF-01 | — | Pending |
| PERF-02 | — | Pending |

**Coverage:**
- v1.1 requirements: 12 total
- Mapped to phases: 0
- Unmapped: 12 ⚠️

---
*Requirements defined: 2026-02-24*
*Last updated: 2026-02-24 after initial definition*
