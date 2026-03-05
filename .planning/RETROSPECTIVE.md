# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.2 — Intelligent Retrieval

**Shipped:** 2026-03-05
**Phases:** 5 | **Plans:** 11

### What Was Built
- Precision retrieval pipeline with RRF rank fusion, token budgets, and 4-signal ranking
- ACT-R memory decay model with per-category rates, bridge protection, and dormant exclusion
- Memory classification system (7 types) with batch reclassification tools
- Claude-driven atomic fact decomposition with parent-child linking
- Structured memory columns (subject/predicate/object) with indexed query routing
- Vault hygiene consolidation with semantic clustering and dry-run mode
- Search transparency with debug signals and tier breakdown

### What Worked
- Phase dependency ordering (10 -> 11 -> 12 -> 13 -> 14) kept each phase building cleanly on the last
- TDD approach in consolidation module (phase 14) caught edge cases early
- Pure-function modules (retrieval.py, vitality.py, consolidation.py) made testing fast and isolated
- Nyquist validation strategy ensured all 31 requirements had automated test coverage
- Schema migration chain (v0-v7) remained gapless through 3 milestones

### What Was Inefficient
- SUMMARY frontmatter missing requirements_completed for phases 10-11 (metadata-only gap, but triggered re-audit)
- Phase 10-11 phase directories use `phase-10`/`phase-11` naming while 12-14 use `12-atomic-decomposition` style — inconsistent naming

### Patterns Established
- Filters applied BEFORE ranking (not after) — consistent pattern across category, tag, and dormant filters
- Response-embedded workflow hints (decomposition_pending in auto_capture) guide Claude tool chaining
- Deferred imports (numpy in consolidation) avoid top-level dependency loading in unrelated code paths
- Fire-and-forget asyncio.create_task for non-blocking side effects (record_access during search)

### Key Lessons
1. Combining related concerns (decay + classification in phase 11) reduces cross-phase wiring and catches design interactions early
2. Claude-driven extraction (decomposition, classification) keeps the server lightweight — store results, not intelligence
3. Union-Find is the right data structure for transitive similarity clustering
4. Structured query routing should bypass the full ranking pipeline, not feed into it

### Cost Observations
- Model mix: balanced profile (opus for planning/execution, sonnet for agents)
- Timeline: 2 days for 5 phases, 11 plans, 31 requirements
- Notable: All phases completed in single day of execution (2026-03-05), planning on 2026-03-04

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 3 | 12 | Initial refactor — established module structure and test patterns |
| v1.1 | 6 | 11 | Tech debt cleanup — CI/CD, security, performance |
| v1.2 | 5 | 11 | Feature milestone — retrieval pipeline, decay, decomposition, consolidation |

### Cumulative Quality

| Milestone | Tests | LOC | New Modules |
|-----------|-------|-----|-------------|
| v1.0 | 172 | 7,215 | 10 (full package) |
| v1.1 | 234 | 8,216 | 0 (quality improvements) |
| v1.2 | 308 | 13,867 | 3 (retrieval, vitality, consolidation) |

### Top Lessons (Verified Across Milestones)

1. Pure-function modules with clear interfaces make testing fast and isolated (v1.0 module split, v1.2 retrieval/vitality/consolidation)
2. Schema migration via PRAGMA user_version scales cleanly — 7 versions across 3 milestones with zero data loss
3. Single-day execution is achievable when planning is thorough and phase dependencies are clear
