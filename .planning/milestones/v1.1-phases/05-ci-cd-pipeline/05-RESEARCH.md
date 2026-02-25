# Phase 5: CI/CD Pipeline - Research

**Researched:** 2026-02-24
**Domain:** GitHub Actions, pytest-cov, ruff, Python matrix CI
**Confidence:** HIGH

---

## Summary

Phase 5 establishes a GitHub Actions workflow that validates every push and pull request with ruff lint, pytest across Python 3.11 and 3.12, and a coverage enforcement gate. The project currently has no `.github/` directory — this phase creates it from scratch.

The critical pre-planning finding is a **coverage threshold conflict**: CICD-02 requires an 80% minimum, but the current measured total coverage is 76% (190 tests, 1327 statements, 322 missed). The planner must decide: either the 80% threshold is aspirational and the gate is set at the measured baseline (~74-76%), or new tests are written as part of this phase to lift coverage to 80% before the gate is applied. STATE.md explicitly notes: "Measure actual coverage before setting `--cov-fail-under` threshold — set at (measured - 2%) to allow headroom for new code in Phases 6-8."

The project uses `pyproject.toml` with `hatchling` as the build backend, `uv` as the recommended installer (per README), but has no `uv.lock` file. The venv was created directly. For CI, the recommended approach is `uv pip install -e ".[semantic]" pytest-cov` — installing the package in editable mode with all extras plus test tooling.

**Primary recommendation:** Create `.github/workflows/ci.yml` using `astral-sh/setup-uv@v5`, Python matrix `["3.11", "3.12"]`, `uv pip install -e ".[semantic]" pytest-cov`, then `ruff check .` + `pytest --cov=remind_me_mcp --cov-fail-under=74`. Add the workflow badge to README.

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CICD-01 | GitHub Actions workflow runs ruff lint and pytest on push/PR for Python 3.11 and 3.12 | GitHub Actions matrix strategy with `astral-sh/setup-uv@v5` and `actions/checkout@v4`; ruff available via `uv pip install ruff`; pytest available via `uv pip install pytest` |
| CICD-02 | Coverage enforcement gate at 80% minimum via pytest-cov | `pytest-cov` v7.0.0 available; `--cov-fail-under=N` flag causes non-zero exit when coverage < N; **CONFLICT: current coverage is 76%, below the 80% target** |
</phase_requirements>

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `actions/checkout` | v4 | Check out repo in CI runner | Official GitHub action, stable major version |
| `astral-sh/setup-uv` | v5 | Install uv + Python in CI | Official Astral action; handles Python versioning, path, and optional caching |
| `ruff` | (from pyproject.toml) | Lint check in CI | Already configured in project; zero-config reuse |
| `pytest` | (from .venv — 9.0.2) | Test runner | Already used in project; `asyncio_mode = "auto"` configured in pyproject.toml |
| `pytest-cov` | 7.0.0 (latest) | Coverage measurement + threshold | Standard pytest plugin; `--cov-fail-under` provides gate behavior |
| `coverage` | 7.13.4 (installed as pytest-cov dep) | Underlying coverage engine | Installed automatically with pytest-cov |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| GitHub Workflow Status Badge | n/a (built-in) | Green CI badge in README | Always — success criterion requires it |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `astral-sh/setup-uv` | `actions/setup-python` + `pip` | setup-python is simpler but slower; project already recommends uv in README |
| `uv pip install -e .` | `uv sync --locked` | `uv sync` requires a `uv.lock` file — project has none; `uv pip install` works without lockfile |
| Single-job workflow | Separate lint + test jobs | Separate jobs provide independent status checks but add orchestration overhead; single job is simpler for a small project |

**Installation in CI:**
```bash
uv pip install -e ".[semantic]" pytest-cov ruff
```

---

## Architecture Patterns

### Recommended Project Structure

```
.github/
└── workflows/
    └── ci.yml        # Single workflow file: lint + test matrix
README.md             # Add workflow badge at top
```

### Pattern 1: Python Matrix with uv

**What:** Run the same job steps across multiple Python versions using a matrix strategy.

**When to use:** Always when the requirement is multi-version validation (CICD-01 explicitly requires 3.11 and 3.12).

**Example:**
```yaml
# Source: https://docs.astral.sh/uv/guides/integration/github/
jobs:
  ci:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: uv pip install -e ".[semantic]" pytest-cov ruff
      - name: Lint
        run: ruff check --output-format=github .
      - name: Test
        run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=74
```

### Pattern 2: Workflow Status Badge in README

**What:** A Markdown image that shows the current CI pass/fail state.

**When to use:** Always on public repos; success criterion 3 requires it.

**Example:**
```markdown
# Source: https://docs.github.com/en/actions/monitoring-and-troubleshooting-workflows/monitoring-workflows/adding-a-workflow-status-badge
![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)
```

### Pattern 3: ruff `--output-format=github` for Annotations

**What:** Tells ruff to emit GitHub Actions annotation format so lint errors appear as inline PR annotations.

**When to use:** Always inside GitHub Actions; the format renders directly in the PR diff view.

**Example:**
```yaml
- name: Lint
  run: ruff check --output-format=github .
```

### Anti-Patterns to Avoid

- **Pinning `astral-sh/setup-uv@main`**: Use a specific version tag (v5) — main is mutable and can break workflows silently.
- **Using `pip install` instead of `uv pip install`**: Inconsistent with the project's stated toolchain (README shows `uv pip install -e .`).
- **`--cov-fail-under=80` before coverage is at 80%**: The current measured coverage is 76% — setting the gate at 80% causes every CI run to fail immediately on the existing test suite. Set at measured baseline with headroom.
- **Setting only `--cov` without `--cov=remind_me_mcp`**: Without scoping to the package, coverage can include test files themselves, inflating the reported number.
- **Workflow triggered only on push**: The requirement (CICD-01) explicitly requires both `push` and `pull_request` triggers.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Coverage threshold enforcement | Custom shell script checking coverage output | `pytest --cov-fail-under=N` | Built-in; exits non-zero automatically; no parsing needed |
| Python version matrix | Multiple separate jobs | `strategy.matrix.python-version` | Native GitHub Actions feature; handles fan-out automatically |
| Badge hosting | Custom badge service or separate server | GitHub's built-in `badge.svg` endpoint | No external service needed; always reflects live workflow state |
| ruff output formatting for CI | Parsing ruff JSON output | `ruff check --output-format=github` | Built-in; produces GitHub Actions annotations without custom code |

**Key insight:** GitHub Actions and the existing tools (pytest-cov, ruff) already handle everything Phase 5 needs. The entire phase is file creation, not code changes.

---

## Common Pitfalls

### Pitfall 1: Coverage Threshold Below Current Reality

**What goes wrong:** Setting `--cov-fail-under=80` causes CI to fail immediately because the existing test suite achieves 76%.

**Why it happens:** The requirement was written aspirationally; measured coverage was not checked before writing CICD-02.

**How to avoid:** Set `--cov-fail-under=74` (measured 76% minus 2% headroom) to make CI green on the existing suite while still blocking significant regressions. Document the gap in the workflow file.

**Warning signs:** Every CI run exits with "FAIL Required test coverage of 80% not reached. Total coverage: 76%".

**Recommended resolution for planner:** Set gate at 74% now (makes CI immediately green); add a follow-up note that reaching 80% is a stretch goal addressable in Phase 6-8 when more tests are added.

### Pitfall 2: Missing `[semantic]` extras in CI

**What goes wrong:** Tests import from `remind_me_mcp.embeddings` which depends on `onnxruntime`, `tokenizers`, `sqlite-vec`, etc. Without the `[semantic]` extras, test collection fails.

**Why it happens:** The package has optional `[semantic]` dependencies that are not installed by default.

**How to avoid:** Install with `uv pip install -e ".[semantic]" pytest-cov ruff` — include the extras.

**Warning signs:** `ImportError: No module named 'onnxruntime'` or `ModuleNotFoundError` at pytest collection time.

### Pitfall 3: `uv.lock` Not Present

**What goes wrong:** Attempting `uv sync --locked` in CI fails because no `uv.lock` file exists in the repo.

**Why it happens:** The project was set up with direct `uv pip install -e .`, not `uv init` / `uv add`.

**How to avoid:** Use `uv pip install -e ".[semantic]"` rather than `uv sync`. Alternatively, generate and commit a `uv.lock` (out of scope for this phase).

**Warning signs:** CI error: "No lockfile found. Run `uv lock` to generate a lockfile."

### Pitfall 4: `asyncio_mode = "auto"` requires pytest-asyncio

**What goes wrong:** `pyproject.toml` sets `asyncio_mode = "auto"` which requires `pytest-asyncio`. If not installed in CI, all async tests fail.

**Why it happens:** `pytest-asyncio` is a pytest plugin — it's not installed automatically with pytest.

**How to avoid:** Check what's in the project `.venv` and include `pytest-asyncio` in the CI install command.

**Warning signs:** `PytestUnraisableExceptionWarning` or `fixture 'event_loop' not found` errors.

### Pitfall 5: Workflow File Naming Affects Badge URL

**What goes wrong:** The badge URL in README references the exact workflow filename. If the file is renamed, the badge breaks.

**Why it happens:** The badge URL is `https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg` — it encodes the filename.

**How to avoid:** Decide the filename (`ci.yml`) before adding it to README, and don't rename it later.

---

## Code Examples

Verified patterns from official sources:

### Complete ci.yml Workflow

```yaml
# Source: https://docs.astral.sh/uv/guides/integration/github/
# Source: https://docs.github.com/en/actions/tutorials/build-and-test-code/python
name: CI

on:
  push:
  pull_request:

jobs:
  ci:
    name: Python ${{ matrix.python-version }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Install uv and Python ${{ matrix.python-version }}
        uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install dependencies
        run: uv pip install -e ".[semantic]" pytest-cov ruff pytest-asyncio

      - name: Lint (ruff)
        run: ruff check --output-format=github .

      - name: Test with coverage
        run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=74
```

### README Badge (once OWNER/REPO is known)

```markdown
[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
```

The badge URL format:
```
https://github.com/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_FILE}/badge.svg
```

### Checking pytest-asyncio is needed

```bash
# Command to verify what pytest plugins are installed in the project venv:
/home/baileyrd/projects/remind_me/.venv/bin/pytest --co -q 2>&1 | head -5
# Result: 190 tests collected — pytest-asyncio must be present in .venv already
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `actions/setup-python` + `pip install -r requirements.txt` | `astral-sh/setup-uv` + `uv pip install` | 2024-2025 | Faster installs, consistent with uv-based local dev |
| `flake8` + `isort` + `black` | `ruff check` + `ruff format` | 2023-2024 | Single tool replaces three; already configured in this project |
| Separate coverage service (Codecov, Coveralls) | Built-in `--cov-fail-under` gate | Always available | No external service token needed for simple threshold enforcement |
| `actions/checkout@v3` | `actions/checkout@v4` (v6 beta exists) | 2023 (v4), 2025 (v6 beta) | v4 is stable standard; v6 requires runner v2.329.0+ |

**Deprecated/outdated:**

- `setup-python@v4`: v5 is current stable; always specify `python-version` in matrix rather than relying on runner default.
- `pip install -r requirements.txt`: Project has no `requirements.txt` — use `pip install -e .` or `uv pip install -e .`.

---

## Coverage Threshold Decision

This is the most important planning decision for Phase 5.

**Measured state (2026-02-24):**

| Module | Coverage |
|--------|----------|
| `__main__.py` | 0% (entry point, not unit-testable in CI without server startup) |
| `pid.py` | 33% |
| `server.py` | 65% |
| `db.py` | 61% |
| **TOTAL** | **76%** |

**Options for `--cov-fail-under`:**

| Option | Value | Tradeoff |
|--------|-------|----------|
| A (STATE.md guidance) | 74% | Immediately green; headroom for new code; honest about current state |
| B (requirement literal) | 80% | Fails CI immediately on existing suite; requires adding tests first |
| C (stretch) | 76% | Exact current coverage; any coverage drop fails; no headroom |

**Recommendation to planner:** Option A (74%) — consistent with STATE.md guidance: "set at (measured - 2%) to allow headroom for new code in Phases 6-8." The requirement says 80% is the *goal* but the test note in STATE.md reflects the team's intent that CI should be green immediately after Phase 5. Document the gap.

---

## Open Questions

1. **Repository owner/name for badge URL**
   - What we know: The badge URL requires `OWNER/REPO` which is the GitHub remote URL.
   - What's unclear: The remote URL is not visible in the local repo config without running `git remote -v`.
   - Recommendation: The planner's task should include a step to retrieve the remote URL (`git remote get-url origin`) and substitute it into the README badge.

2. **Is pytest-asyncio explicitly in the install list?**
   - What we know: 190 tests pass locally, `asyncio_mode = "auto"` is configured, so pytest-asyncio must be available in the current .venv.
   - What's unclear: Whether pytest-asyncio is a declared dependency in `pyproject.toml` or just installed in the venv.
   - Recommendation: The executor should verify with `grep pytest-asyncio pyproject.toml` and add it to the CI install command explicitly if needed.

3. **`fail-fast: false` vs `fail-fast: true` (default)**
   - What we know: `fail-fast: true` (default) cancels remaining matrix jobs when one fails.
   - What's unclear: Whether the team wants 3.11 failure to cancel 3.12 run (saves minutes) or wants both results always (costs minutes, more info).
   - Recommendation: Use `fail-fast: false` so both Python versions always complete and report independently — more useful for diagnosing version-specific failures.

---

## Sources

### Primary (HIGH confidence)

- [uv GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/) — astral-sh/setup-uv@v5 workflow pattern, `uv pip install` vs `uv sync` guidance
- [GitHub Docs: Building and testing Python](https://docs.github.com/en/actions/tutorials/build-and-test-code/python) — matrix strategy, actions/checkout, setup-python patterns
- [GitHub Docs: Adding a workflow status badge](https://docs.github.com/en/actions/monitoring-and-troubleshooting-workflows/monitoring-workflows/adding-a-workflow-status-badge) — badge URL format, query parameters
- [pytest-cov PyPI](https://pypi.org/project/pytest-cov/) — `--cov-fail-under` flag behavior

### Secondary (MEDIUM confidence)

- [A GitHub Actions setup for Python projects in 2025](https://ber2.github.io/posts/2025_github_actions_python/) — 2025 pattern with separate jobs, uv
- [Automate Python Linting with Ruff and GitHub Actions](https://dev.to/ken_mwaura1/automate-python-linting-and-code-style-enforcement-with-ruff-and-github-actions-2kk1) — `ruff check --output-format=github` annotation pattern

### Tertiary (LOW confidence)

- [Coverage Badge · GitHub Marketplace](https://github.com/marketplace/actions/coverage-badge) — `we-cli/coverage-badge-action` for SVG badges on gh-pages (out of scope for this phase — workflow badge is sufficient)

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — verified via official uv docs, GitHub docs, and local venv inspection
- Architecture: HIGH — single workflow file with matrix is a well-documented, stable pattern
- Pitfalls: HIGH for coverage threshold (measured directly); MEDIUM for asyncio/semantic extras (inferred from test suite behavior)
- Coverage threshold: HIGH — measured directly at 76% on 2026-02-24

**Research date:** 2026-02-24
**Valid until:** 2026-03-24 (stable tooling — 30 days)
