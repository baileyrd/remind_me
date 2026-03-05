# Remind Me MCP

## What This Is

A personal memory server for Claude that persists facts, preferences, and conversations across sessions. Works with Claude.ai, Claude Code, and Claude Desktop via MCP (Model Context Protocol). Features precision retrieval with RRF rank fusion, token budgets, ACT-R memory decay, atomic fact decomposition, structured triples, vault consolidation, and search transparency. Structured as a well-tested 10-module Python package with security hardening, CI/CD, and 308 tests.

## Core Value

Persistent, searchable memory that works seamlessly across all Claude interfaces — with a codebase that's modular, tested, and maintainable.

## Requirements

### Validated

- ✓ MCP tool-based memory CRUD (add, search, list, get, update, delete) — v1.0
- ✓ Hybrid search (FTS5 keyword + semantic vector similarity) — v1.0
- ✓ Chat import from JSON, JSONL, Markdown formats — v1.0
- ✓ Auto-capture of conversations (dialog + summary linked by capture_id) — v1.0
- ✓ Optional HTTP dashboard with React UI for browsing/editing memories — v1.0
- ✓ Optional semantic search via ONNX embeddings + sqlite-vec — v1.0
- ✓ Pydantic input validation on all MCP tools — v1.0
- ✓ Environment-based configuration (no magic globals) — v1.0
- ✓ Graceful degradation when optional dependencies are missing — v1.0
- ✓ Modular project structure with clear separation of concerns — v1.0
- ✓ Comprehensive test suite (172 unit + integration tests) — v1.0
- ✓ Robust error handling with specific exception types — v1.0
- ✓ DRY shared import_directory() function — v1.0
- ✓ Async-first: asyncio.to_thread for embeddings, WAL + busy_timeout for concurrency — v1.0
- ✓ Full docstring and type hint coverage — v1.0
- ✓ Schema migration system via PRAGMA user_version — v1.0
- ✓ Tag filtering in SQL via memory_tags junction table — v1.0
- ✓ DB singleton connection (lazy, lifespan-scoped) — v1.0
- ✓ Both known bugs fixed (import embedding ID mismatch, capture_id LIKE scan) — v1.0
- ✓ Dashboard JSX extracted to separate file — v1.0
- ✓ Self-update feature with background check and CLI flags — post-v1.0
- ✓ Zero ruff lint warnings, narrowed exception handlers — v1.1
- ✓ Monolith file removed from repository — v1.1
- ✓ GitHub Actions CI with lint + test + coverage gates (Python 3.11/3.12) — v1.1
- ✓ Coverage enforcement at 80% minimum — v1.1
- ✓ CORS restricted to localhost origins — v1.1
- ✓ Import path traversal guard with configurable IMPORT_ROOTS — v1.1
- ✓ Optional Bearer token auth for API routes — v1.1
- ✓ REST API embedding parity (POST/PUT generate embeddings) — v1.1
- ✓ Batch reindex (32 at a time) — v1.1
- ✓ Concurrent directory import with semaphore-bounded parallelism — v1.1
- ✓ Token budget cap on retrieval (800-token default) — v1.2
- ✓ RRF rank fusion (k=60) with recency as third signal — v1.2
- ✓ ACT-R decay/vitality model with per-category decay rates and bridge protection — v1.2
- ✓ Dormant memory exclusion from default search — v1.2
- ✓ Memory classification with 7 types and batch reclassification tools — v1.2
- ✓ Vitality report tool — v1.2
- ✓ Claude-driven atomic fact decomposition with parent-child linking — v1.2
- ✓ Batch decomposition and decomposition_pending hints — v1.2
- ✓ Structured memory columns (subject/predicate/object) with indexed query routing — v1.2
- ✓ Supersession tracking (superseded_by column) — v1.2
- ✓ Search transparency: debug signals, tier breakdown, dormant exclusion count — v1.2
- ✓ Vault hygiene: semantic clustering, consolidation, dry-run mode — v1.2

### Active

(None yet — define in next milestone)

### Out of Scope

- Build tooling for dashboard (Vite/esbuild) — keeping Babel standalone for simplicity
- Splitting into separate installable packages — single package install preserved
- PostgreSQL migration — SQLite with WAL is sufficient for personal use
- Node.js build tooling — unnecessary complexity for simple dashboard
- Full OAuth2/JWT auth — static bearer token sufficient for personal localhost tool
- Rate limiting — single-user personal tool; no multi-tenant scenario
- HTTPS/TLS — localhost traffic; self-signed certs add complexity with no benefit
- Server-side LLM calls — decomposition and classification are Claude's job, server stores results
- REST API semantic search endpoint — deferred; MCP tool covers this
- mypy strict mode — deferred; not retrieval-related
- Automatic consolidation — requires human review; dry_run + manual approval by design

## Current State

**Last shipped:** v1.2 Intelligent Retrieval (2026-03-05)
**Next milestone:** TBD — run `/gsd:new-milestone` to define

## Context

Shipped v1.2 with 13,867 lines of Python (package + tests).
Tech stack: Python 3.11+, FastMCP, SQLite (WAL), Pydantic, Starlette, ONNX Runtime (optional), React/Babel (dashboard).
10-module package: config, db, embeddings, models, formatting, importer, pid, server, tools, api, plus dashboard/ subpackage.
New modules: retrieval.py (RRF/token budget), vitality.py (ACT-R decay), consolidation.py (semantic clustering).
308 tests passing.
20 MCP tools + 2 resource handlers registered.
Schema at version 7 (PRAGMA user_version) with gapless migration chain v0-v7.
GitHub Actions CI validates every push/PR with lint + test + coverage gates.
Security: CORS localhost-only, import path guard, optional Bearer token auth.
Search pipeline: RRF fusion (keyword + semantic + recency + vitality), token budget cap, dormant exclusion, structured query routing, debug signals.

## Constraints

- **Packaging**: Must remain a single `pip install`-able package — internal module split only
- **Compatibility**: All existing MCP tool names and parameters must remain unchanged (clients depend on them)
- **Dashboard**: Keep Babel standalone transpilation (no build step)
- **Data**: Must be compatible with existing `~/.remind-me/memory.db` databases (migration, not recreation)
- **Python**: Requires Python 3.11+

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Refactor and test in parallel | Building tests on the new clean structure avoids writing tests for code that will change | ✓ Good — clean interfaces made test writing fast |
| Single package, multiple modules | Preserves simple install while enabling separation of concerns | ✓ Good — 10 modules, zero circular imports |
| Keep Babel standalone for dashboard | Avoids adding Node.js build tooling dependency for a simple dashboard | ✓ Good — dashboard works, no build step needed |
| Fix bugs during refactor | Bugs surface naturally when restructuring the affected code | ✓ Good — both bugs found and fixed with tests |
| Security hardening as separate milestone | Mixing security changes with structural refactor increases risk | ✓ Good — clean baseline made security changes reviewable |
| SQLite WAL mode over PostgreSQL | WAL fixes multi-process concurrency without adding dependencies | ✓ Good — concurrent access verified by tests |
| Entry point via __main__:main | CLI flags (--version, --check-update, --update) need argparse | ✓ Good — replaced mcp.run entry point |
| Coverage gate at 74% initially, raised to 80% | Start below measured coverage, raise as tests accumulate | ✓ Good — avoided CI red while building toward target |
| CORS regex over origin list | Handles both localhost and 127.0.0.1 with any port | ✓ Good — no subdomain bypass possible |
| BearerAuthMiddleware inside _build_api_app() | Preserves lazy Starlette import for MCP stdio mode | ✓ Good — MCP mode never loads Starlette |
| hmac.compare_digest for token comparison | Timing-safe comparison prevents side-channel attacks | ✓ Good — stdlib, no extra dependencies |
| threading.Lock for concurrent SQLite writes | Serializes DB writes while allowing concurrent file I/O | ✓ Good — prevents InterfaceError under 8-worker concurrency |
| sqlite-vec knn fix (AND mv.k = ?) | LIMIT doesn't push through JOIN in sqlite-vec 0.1.6 | ✓ Good — fixed semantic search for all code paths |
| RRF over linear blending | RRF is rank-based, not score-based — more robust to signal scale differences | ✓ Good — 4-signal fusion without normalization |
| Token budget via len//4 estimation | Avoids tokenizer dependency; close enough for retrieval trimming | ✓ Good — no extra dependency |
| ACT-R vitality formula | Cognitive science model for memory strength; natural decay with access reinforcement | ✓ Good — intuitive behavior, bridge protection works |
| Claude-driven decomposition | Server stores results; Claude does extraction — no server-side LLM dependency | ✓ Good — keeps server lightweight |
| Columns on existing table (not separate) | subject/predicate/object as nullable columns avoids dual-table complexity | ✓ Good — zero migration issues |
| Union-Find for transitive clustering | A~B and B~C implies single cluster; correct graph semantics | ✓ Good — handles chains properly |
| Filters before RRF ranking | Category/dormant filters narrow candidate set before expensive ranking | ✓ Good — consistent, predictable filtering |

---
*Last updated: 2026-03-05 after v1.2 milestone completed*
