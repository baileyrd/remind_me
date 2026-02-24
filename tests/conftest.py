"""
Shared pytest fixtures for all remind_me_mcp test modules.

All fixtures use tmp_path or in-memory resources — they never touch ~/.remind-me/.
Importing remind_me_mcp.tools or remind_me_mcp.server is intentionally avoided
here to prevent MCP registration side effects at collection time.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import numpy as np
import pytest

from remind_me_mcp.db import _ensure_schema, _make_id, _now_iso

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Config path isolation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def tmp_memory_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped fixture that redirects all config paths to a temp directory.

    Ensures no test ever touches the real ~/.remind-me/ directory.

    Uses pytest.MonkeyPatch() directly (not the function-scoped monkeypatch
    fixture) because session-scoped fixtures cannot depend on function-scoped ones.
    The patches are undone at session teardown.
    """
    tmp_dir = tmp_path_factory.mktemp("remind_me_session")
    tmp_db = tmp_dir / "memory.db"
    tmp_pid = tmp_dir / "server.pid"
    tmp_import_log = tmp_dir / "import_log.json"

    mp = pytest.MonkeyPatch()

    import remind_me_mcp.config as _cfg

    mp.setattr(_cfg, "MEMORY_DIR", tmp_dir)
    mp.setattr(_cfg, "DB_PATH", tmp_db)
    mp.setattr(_cfg, "PID_FILE", tmp_pid)
    mp.setattr(_cfg, "IMPORT_LOG", tmp_import_log)

    # Patch direct imports in sibling modules
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.pid as _pid_mod
    import remind_me_mcp.server as _srv_mod

    mp.setattr(_db_mod, "DB_PATH", tmp_db)
    mp.setattr(_api_mod, "DB_PATH", tmp_db)
    mp.setattr(_pid_mod, "DB_PATH", tmp_db)
    mp.setattr(_pid_mod, "PID_FILE", tmp_pid)
    mp.setattr(_srv_mod, "DB_PATH", tmp_db)

    yield tmp_dir

    mp.undo()


# ---------------------------------------------------------------------------
# In-memory database
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Function-scoped fixture providing an isolated in-memory SQLite database.

    Creates the full schema (memories, chat_imports, FTS5 triggers, indexes)
    on a fresh ':memory:' connection. Monkeypatches _get_db in all modules
    that call it so application code automatically uses this test database.

    Yields the connection; closes it in teardown.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)

    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.importer as _importer_mod
    import remind_me_mcp.tools as _tools_mod

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_api_mod, "_get_db", lambda: db)
    # tools.py and importer.py use `from remind_me_mcp.db import _get_db` which
    # creates separate bindings — patch those local references directly so tool
    # handlers route through the test in-memory database.
    monkeypatch.setattr(_tools_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)

    yield db
    db.close()


# ---------------------------------------------------------------------------
# Mock embedder
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic fake embedder that never loads any ML model.

    Returns L2-normalised 384-dimensional float32 vectors.
    Identical texts produce identical vectors (seeded on content hash).
    """

    @property
    def available(self) -> bool:
        """Always returns True — no model needed."""
        return True

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return a deterministic (len(texts), 384) float32 array, L2-normalised."""
        rows: list[np.ndarray] = []
        for text in texts:
            seed = hash(text) & 0xFFFFFFFF  # positive 32-bit seed
            rng = np.random.default_rng(seed=seed)
            vec = rng.standard_normal(384).astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 1e-9:
                vec /= norm
            rows.append(vec)
        return np.stack(rows, axis=0)

    def embed_one(self, text: str) -> bytes:
        """Embed a single text and return raw bytes for sqlite-vec storage."""
        return self.embed([text])[0].tobytes()


@pytest.fixture()
def mock_embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    """Function-scoped fixture providing a FakeEmbedder.

    Monkeypatches _get_embedder and the module-level singleton in
    remind_me_mcp.db and remind_me_mcp.embeddings so all code that
    requests an embedder receives the fake without loading any model.
    """
    fake = FakeEmbedder()

    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.embeddings as _emb_mod

    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: fake)
    monkeypatch.setattr(_emb_mod, "_get_embedder", lambda: fake)
    monkeypatch.setattr(_emb_mod, "_embedder", fake)

    return fake


# ---------------------------------------------------------------------------
# In-memory database with sqlite-vec extension
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn_with_vec(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Function-scoped fixture providing an isolated in-memory SQLite database with sqlite-vec loaded.

    Extends db_conn by loading the sqlite-vec extension before _ensure_schema so
    that the memories_vec virtual table is created. Monkeypatches _get_db in all
    modules that call it so application code automatically uses this test database.

    Yields the connection; closes it in teardown.
    """
    import sqlite_vec

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    _ensure_schema(db)

    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.importer as _importer_mod
    import remind_me_mcp.tools as _tools_mod

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_api_mod, "_get_db", lambda: db)
    # tools.py and importer.py use `from remind_me_mcp.db import _get_db` which
    # creates separate bindings — patch those local references directly so tool
    # handlers route through the test in-memory database.
    monkeypatch.setattr(_tools_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)

    yield db
    db.close()


# ---------------------------------------------------------------------------
# Memory factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_factory(db_conn: sqlite3.Connection):
    """Function-scoped fixture that returns a callable for creating test memory rows.

    Usage::

        def test_example(memory_factory):
            mem = memory_factory(content="Hello", category="notes")
            assert mem["id"] is not None

    Accepts keyword overrides for any memory field. Inserts the row into the
    in-memory database and returns the full memory dict.
    """

    def create_memory(**overrides) -> dict:
        """Insert a memory row with sensible defaults, returning the full dict."""
        content = overrides.get("content", "Test memory content")
        now = _now_iso()
        defaults = {
            "id": _make_id(content),
            "content": content,
            "category": "general",
            "tags": [],
            "source": "manual",
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        defaults.update(overrides)

        # Serialise JSON fields for storage
        tags_json = json.dumps(defaults["tags"]) if isinstance(defaults["tags"], list) else defaults["tags"]
        metadata_json = json.dumps(defaults["metadata"]) if isinstance(defaults["metadata"], dict) else defaults["metadata"]

        db_conn.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                defaults["id"],
                defaults["content"],
                defaults["category"],
                tags_json,
                defaults["source"],
                metadata_json,
                defaults["created_at"],
                defaults["updated_at"],
            ),
        )
        db_conn.commit()

        # Return with deserialised fields
        result = dict(defaults)
        result["tags"] = defaults["tags"] if isinstance(defaults["tags"], list) else json.loads(defaults["tags"])
        result["metadata"] = defaults["metadata"] if isinstance(defaults["metadata"], dict) else json.loads(defaults["metadata"])
        return result

    return create_memory


# ---------------------------------------------------------------------------
# Sample chat files
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_chat_json(tmp_path: Path) -> Path:
    """Function-scoped fixture providing a temporary Claude JSON export file.

    Returns the Path to the file.
    """
    data = {
        "chat_messages": [
            {"sender": "human", "content": [{"type": "text", "text": "What is Python?"}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "Python is a programming language."}]},
        ]
    }
    p = tmp_path / "chat_export.json"
    p.write_text(json.dumps(data))
    return p


@pytest.fixture()
def sample_chat_md(tmp_path: Path) -> Path:
    """Function-scoped fixture providing a temporary Markdown chat export file.

    Returns the Path to the file.
    """
    content = "## Human\nWhat is Python?\n\n## Assistant\nPython is a programming language.\n"
    p = tmp_path / "chat_export.md"
    p.write_text(content)
    return p
