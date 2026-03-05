# Milestones

## v1.2 Intelligent Retrieval (Shipped: 2026-03-05)

**Phases completed:** 5 phases, 11 plans
**Lines of code:** 13,867 Python (package + tests)
**Timeline:** 2 days (2026-03-04 to 2026-03-05)
**Git range:** 6fb8cef..31f101d (49 files, 10,242 insertions)
**Tests:** 308 passing
**Requirements:** 31/31 satisfied

**Key accomplishments:**
- Replaced naive linear score blending with RRF ranking (k=60) fusing keyword, semantic, recency, and vitality signals with token budget cap (RETR-01 to RETR-04)
- ACT-R vitality/decay model with per-category decay rates, bridge protection, and dormant memory exclusion (DECAY-01 to DECAY-06)
- Memory classification system with 7 types, batch reclassification tools, and vitality report (CLSF-01 to CLSF-05)
- Claude-driven atomic fact decomposition with parent-child linking via source_capture_id and batch processing (ATOM-01 to ATOM-05)
- Structured memory columns (subject/predicate/object) with indexed query routing and supersession tracking (STRC-01 to STRC-04)
- Vault hygiene consolidation with semantic clustering, dry-run mode, and auto-merge into highest-vitality canonical (HYGN-01 to HYGN-05)
- Search transparency with debug signals, tier breakdown, and dormant exclusion count (TRNS-01 to TRNS-02)

**Tech debt (non-blocking):**
- SUMMARY frontmatter missing requirements_completed for phases 10-11 (metadata only; verified in VERIFICATION.md)
- formatting.py does not surface vitality, memory_type, structured columns in markdown output (cosmetic)

**Archives:**
- milestones/v1.2-ROADMAP.md
- milestones/v1.2-REQUIREMENTS.md
- milestones/v1.2-MILESTONE-AUDIT.md

---

## v1.0 Full Refactor (Shipped: 2026-02-24)

**Phases completed:** 3 phases, 12 plans
**Lines of code:** 3,680 (package) + 3,535 (tests) = 7,215 total
**Timeline:** 2 days (2026-02-22 to 2026-02-24)
**Git range:** 2c29669..635502a (75 files, 17,578 insertions)

**Key accomplishments:**
- Split 2,500-line monolith into 10-module package with zero circular imports and identical MCP tool behavior
- Built 172-test suite (unit + integration) with in-memory SQLite, mock embedders, and full config isolation
- Fixed import embedding ID mismatch (BUGF-01) and fragile capture_id LIKE scan (BUGF-02)
- Added async safety: WAL mode, busy_timeout, singleton connection, asyncio.to_thread for embeddings
- Implemented schema migration system (PRAGMA user_version) with memory_tags junction table
- Achieved full docstring and type hint coverage across all modules

**Known tech debt:**
- Broad `except Exception` in embeddings.py/pid.py (justified for external probes)
- API path doesn't embed memories (inherited from monolith)
- 30 ruff warnings (unused imports, style)
- Original monolith file still in repo root

**Archives:**
- milestones/v1.0-ROADMAP.md
- milestones/v1.0-REQUIREMENTS.md
- milestones/v1.0-MILESTONE-AUDIT.md

---


## v1.1 Address 1.0 Tech Debt (Shipped: 2026-02-25)

**Phases completed:** 6 phases, 11 plans
**Lines of code:** 8,216 Python (package + tests)
**Timeline:** 1 day (2026-02-24 to 2026-02-25)
**Git range:** f2e6bd8..04f8101 (23 files, 1,163 insertions, 2,627 deletions)
**Tests:** 234 passing, 80.19% line coverage

**Key accomplishments:**
- Resolved all 30 ruff lint warnings, narrowed exception handlers, deleted dead monolith file (QUAL-01/02/03)
- GitHub Actions CI pipeline with lint + test + coverage gates across Python 3.11/3.12 matrix (CICD-01/02)
- Security hardening: CORS locked to localhost, import path traversal guard, optional Bearer token auth (SEC-01/02/03)
- REST API embedding parity: POST/PUT now generate semantic embeddings matching MCP tool behavior (EMBD-01/02)
- Batch reindex (32-at-a-time) and concurrent directory import with semaphore-bounded parallelism (PERF-01/02)
- Coverage gate raised from 74% to 80% with 18 new branch-coverage tests (234 total)

**All v1.0 tech debt resolved:**
- Broad `except Exception` → narrowed to `except OSError` (pid.py), documented at ONNX boundaries
- API path embedding gap → REST API memories now generate embeddings on create/update
- 30 ruff warnings → zero warnings
- Monolith file → deleted

**Archives:**
- milestones/v1.1-ROADMAP.md
- milestones/v1.1-REQUIREMENTS.md
- milestones/v1.1-MILESTONE-AUDIT.md

---

