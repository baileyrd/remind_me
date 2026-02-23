# Testing Patterns

**Analysis Date:** 2026-02-22

## Test Framework

**Runner:**
- None detected — no test framework configured in `pyproject.toml` or project root
- No `pytest`, `unittest`, `jest`, or similar runner found

**Assertion Library:**
- Not applicable — no tests exist

**Run Commands:**
```bash
# No test commands defined
# pyproject.toml has no [tool.pytest] section or test scripts
```

## Test File Organization

**Location:**
- No test files found — zero `*.test.*`, `*_test.*`, or `test_*.py` files in the project
- No `tests/` or `test/` directory exists

**Naming:**
- No established pattern — no tests to observe

**Structure:**
```
remind_me/
  remind_me_mcp.py     # All source — no co-located tests
  remind_me_dashboard.jsx
  pyproject.toml
  README.md
  # No tests/
```

## Test Structure

**Suite Organization:**
- No existing tests to reference

**Recommended pattern for new tests** (based on Python conventions and project style):
```python
import pytest
from unittest.mock import patch, MagicMock


def test_make_id_deterministic_within_same_call():
    """Test that _make_id returns a 12-char hex string."""
    from remind_me_mcp import _make_id
    result = _make_id("some content")
    assert isinstance(result, str)
    assert len(result) == 12


def test_chunk_text_respects_max_length():
    """Test that _chunk_text never produces chunks longer than max_len."""
    from remind_me_mcp import _chunk_text
    text = "word " * 1000
    chunks = _chunk_text(text, 100)
    for chunk in chunks:
        assert len(chunk) <= 100
```

## Mocking

**Framework:**
- Not established — no mocks in codebase
- Recommend `unittest.mock` (stdlib) or `pytest-mock` for new tests

**Recommended patterns for this codebase:**

```python
# Mock SQLite database
import sqlite3
from unittest.mock import patch, MagicMock

def test_memory_add_stores_to_db():
    mock_db = MagicMock(spec=sqlite3.Connection)
    with patch("remind_me_mcp._get_db", return_value=mock_db):
        with patch("remind_me_mcp._embed_and_store", return_value=False):
            # test tool handler
            pass

# Mock environment variables
def test_config_reads_from_env(monkeypatch):
    monkeypatch.setenv("REMIND_ME_MCP_DIR", "/tmp/test-remind-me")
    # re-import or reload module to pick up env changes
```

**What to Mock:**
- SQLite connections (`_get_db`) — avoid real filesystem I/O in unit tests
- `_embed_and_store` — avoid downloading ML models in tests
- `os.kill` — avoid sending signals to processes in PID-check tests
- `urllib.request.urlopen` — avoid real HTTP in UI health-check tests
- `Path.exists`, `Path.read_text` — avoid filesystem in import tests

**What NOT to Mock:**
- Pure functions: `_now_iso`, `_make_id`, `_chunk_text`, `_filter_messages`, `_row_to_dict`
- Pydantic model validation (test validators directly)
- `_extract_messages_from_json` — pure parsing logic, no I/O

## Fixtures and Factories

**Test Data:**
- No fixtures established; recommended pattern for new tests:

```python
# Factory functions for test data
def make_memory_row(overrides: dict | None = None) -> dict:
    base = {
        "id": "abc123def456",
        "content": "Test memory content",
        "category": "general",
        "tags": "[]",
        "source": "manual",
        "metadata": "{}",
        "created_at": "2026-02-22T00:00:00+00:00",
        "updated_at": "2026-02-22T00:00:00+00:00",
    }
    return {**base, **(overrides or {})}

# Sample chat export formats for import tests
SAMPLE_CLAUDE_EXPORT = {
    "chat_messages": [
        {"sender": "human", "content": [{"type": "text", "text": "Hello"}]},
        {"sender": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
    ]
}
```

**Location:**
- No `tests/fixtures/` directory exists
- For new tests: create `tests/fixtures/` for JSON export files used in import testing
- Factory functions: define in test file near usage for now

## Coverage

**Requirements:**
- No enforced coverage target — no coverage configuration exists
- No CI pipeline configured

**Configuration:**
- Not configured

**View Coverage:**
```bash
# If pytest and pytest-cov are installed:
pytest --cov=remind_me_mcp --cov-report=html
open htmlcov/index.html
```

## Test Types

**Unit Tests:**
- Not yet written
- High-value targets: `_chunk_text`, `_extract_messages_from_json`, `_filter_messages`, `_parse_markdown_chat`, `_row_to_dict`, Pydantic validators on `ChatImportInput` and `BulkImportDirInput`

**Integration Tests:**
- Not yet written
- High-value targets: `import_chat_file` with a real temp SQLite DB, `memory_add`/`memory_search` tool round-trips

**E2E Tests:**
- Not used

## Common Patterns

**Async Testing** (for MCP tool handlers, which are `async def`):
```python
import pytest

@pytest.mark.asyncio
async def test_memory_add_returns_confirmation():
    from remind_me_mcp import memory_add, MemoryAddInput
    params = MemoryAddInput(content="Test content", category="test")
    # Patch DB and embed to avoid I/O
    with patch("remind_me_mcp._get_db") as mock_db, \
         patch("remind_me_mcp._embed_and_store", return_value=False):
        mock_db.return_value.execute.return_value = MagicMock()
        result = await memory_add(params)
    assert "Memory stored" in result
```

**Error Testing** (Pydantic validators):
```python
import pytest
from pydantic import ValidationError
from remind_me_mcp import ChatImportInput

def test_chat_import_rejects_missing_file():
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path="/nonexistent/path/file.json")
    assert "File not found" in str(exc_info.value)

def test_chat_import_rejects_unsupported_format(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("col1,col2")
    with pytest.raises(ValidationError):
        ChatImportInput(file_path=str(f))
```

**Snapshot Testing:**
- Not used

---

*Testing analysis: 2026-02-22*
*Update when test patterns change*
