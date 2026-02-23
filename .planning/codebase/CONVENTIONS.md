# Coding Conventions

**Analysis Date:** 2026-02-22

## Naming Patterns

**Files:**
- `snake_case` for all Python files (`remind_me_mcp.py`)
- Single-file architecture — entire project in `remind_me_mcp.py`
- React dashboard in `remind_me_dashboard.jsx`

**Functions:**
- `snake_case` for all functions and methods
- `_leading_underscore` prefix for private/internal helpers (`_get_db`, `_make_id`, `_row_to_dict`, `_fmt_memory_md`)
- Public functions have no underscore prefix (`import_chat_file`, `get_server_status`)
- Async tool handler functions use descriptive verb-noun names (`memory_add`, `memory_search`, `memory_list`)

**Variables:**
- `snake_case` for all local variables and parameters
- `UPPER_SNAKE_CASE` for module-level constants (`MEMORY_DIR`, `DB_PATH`, `EMBEDDING_DIM`, `SERVE_UI`)
- `_leading_underscore` for private module globals (`_embedder`)

**Types/Classes:**
- `PascalCase` for Pydantic model classes (`MemoryAddInput`, `MemorySearchInput`, `AutoCaptureInput`)
- `PascalCase` for regular classes (`_Embedder` — private class gets underscore prefix)
- `PascalCase` for Enum classes (`ResponseFormat`)
- Type aliases inline using `|` union syntax from `__future__ import annotations`

## Code Style

**Formatting:**
- No formatter config file detected (no `.prettierrc`, no `ruff.toml`, no `pyproject.toml` `[tool.ruff]` section)
- 4-space indentation (standard Python)
- Double quotes for strings in most contexts; single quotes used in some f-strings
- Lines generally kept to ~100 characters
- Trailing commas used in multi-line function arguments and data structures

**Linting:**
- No linting config detected (no `.flake8`, `mypy.ini`, or lint tool in `pyproject.toml`)
- Type hints used throughout — `from __future__ import annotations` at top
- `typing.Any`, `typing.Optional` imported from `typing`; uses `X | Y` union syntax alongside

**Type Hints:**
- Required on all function signatures: parameters and return types
- `dict[str, Any]`, `list[str]`, `list[dict]` — use built-in generic types (Python 3.9+ style)
- `Optional[str]` for nullable Pydantic fields
- `X | None` for internal function return types (e.g., `_Embedder | None`)

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first)
2. Standard library imports (alphabetical)
3. Third-party imports (`numpy`, `mcp`, `pydantic`)
4. No internal module imports (single-file project)

**Pattern:**
- Lazy imports inside functions for optional/heavy dependencies (`import urllib.request`, `from huggingface_hub import ...`, `from starlette.applications import ...`)
- All core imports at module top level

**Path Aliases:**
- Not applicable — single-file project

## Error Handling

**Patterns:**
- Broad `except Exception` at boundaries to prevent server crashes
- Specific exception types caught for known cases (`json.JSONDecodeError`, `sqlite3.OperationalError`, `ImportError`, `OSError`, `KeyError`, `TypeError`)
- Errors logged with `log.debug` or `log.warning` before swallowing (not silent)
- Functions return sentinel values on failure (`None`, `False`, `[]`, `{}`) rather than raising
- Pydantic `field_validator` raises `ValueError` for invalid inputs — let Pydantic propagate

**Error Return Conventions:**
- MCP tool handlers return error strings to the caller (user-visible): `f"Memory \`{id}\` not found."`
- Internal helpers return `None` / `False` / empty list to signal failure
- Import functions return `{"status": "error", ...}` dicts for structured error reporting

**Logging:**
- `log.warning()` for recoverable failures where user action may be needed (missing deps)
- `log.debug()` for expected/silent failures (embed failure for individual memory)
- `log.info()` for startup events and significant state changes

## Logging

**Framework:**
- Standard `logging` module, configured at module level
- `logging.basicConfig(stream=sys.stderr, ...)` — stderr only (stdout reserved for MCP stdio transport)
- Named logger: `log = logging.getLogger("remind_me_mcp")`
- Format: `"%(levelname)s | %(message)s"`

**Patterns:**
- `log.info("message: %s", value)` — use `%`-style formatting (not f-strings) in log calls
- Log at startup with configuration paths
- Log at embedding load/failure (significant I/O)
- Do not log in tight loops (only on error in per-item loops)

## Comments

**When to Comment:**
- Section dividers use `# ---...--- #` banners with descriptive headers (e.g., `# Configuration`, `# Database helpers`, `# MCP Server`)
- Inline comments explain non-obvious behavior: `# signal 0 = just check existence`, `# WAL = safe for concurrent readers`
- Docstrings explain what and why for public functions
- Comment before SQL blocks when query intent needs clarification

**Docstrings:**
- Required on all public functions and classes
- Format: multi-line triple-quoted strings
- Include Args and Returns sections for MCP tool handlers (these are user-visible descriptions)
- Private helpers (`_prefixed`) may have shorter one-line docstrings
- Class docstrings explain overall purpose

**Inline Comments:**
- Used sparingly to clarify algorithm intent (`# Mean pooling over token embeddings`, `# L2 normalize`)
- `# truncate very long content for embedding` — explain magic numbers and truncation choices

## Function Design

**Size:**
- Tool handler functions are moderately long (30-80 lines) — acceptable for MCP tools that orchestrate multiple steps
- Helper functions kept short (5-25 lines): `_now_iso`, `_make_id`, `_row_to_dict`, `_fmt_memory_md`
- Complex logic extracted into private helpers

**Parameters:**
- Pydantic `BaseModel` subclasses used as single `params` parameter for all multi-field tool inputs
- Simple tools with 1-2 params accept them directly (`memory_get(memory_id: str)`, `remind_me_get_capture(capture_id: str)`)
- Internal helpers use explicit positional parameters

**Return Values:**
- All MCP tool handlers return `str` (MCP protocol requirement)
- Internal helpers return typed values or `None`/`False` for failure
- Early returns for guard clauses (not found → return error string immediately)

## Module Design

**Exports:**
- Single-file module — `pyproject.toml` entry point: `remind_me_mcp:mcp.run`
- No `__all__` defined
- Public API is the `mcp` FastMCP instance and `import_chat_file` function (used by tools)

**Structure Pattern:**
- Organized by logical section with `# ---` banners in this order:
  1. Configuration (constants, env vars)
  2. Server instance detection helpers
  3. Database helpers
  4. Pydantic input models
  5. Formatting helpers
  6. Chat import engine
  7. Lifespan / startup
  8. MCP Server + tools + resources
  9. HTTP API server (optional)
  10. CLI entry point (`if __name__ == "__main__"`)

---

*Convention analysis: 2026-02-22*
*Update when patterns change*
