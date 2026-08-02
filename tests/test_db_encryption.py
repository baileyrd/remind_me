"""
Tests for opt-in SQLCipher-backed encryption at rest (issue #184).

`REMIND_ME_DB_ENCRYPTION_KEY` (config.DB_ENCRYPTION_KEY) is unset by default
across the whole test suite -- these tests explicitly enable it via the
`encryption_key` fixture below, scoped to each test via monkeypatch, and
never touch the module-level default any other test file relies on.

Everything past the `pytest.importorskip` below requires the optional
`sqlcipher3-wheels` package (the `encryption` extra) to be installed --
mirroring the `[semantic]`/PDF-OCR extras' `pytest.importorskip` pattern
elsewhere in this suite (see e.g. conftest.py's `pytest.importorskip
("sqlite_vec", ...)`), so this whole file cleanly *skips*, not fails, in an
environment without the extra. See ARCHITECTURE.md's "Encryption at rest"
section for the full design rationale, including the known limitation that
this integration was verified for the connection-open/backup/restore-
validation path, not swept across every `except sqlite3.*` handler in the
codebase.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import TYPE_CHECKING

import pytest

sqlcipher = pytest.importorskip(
    "sqlcipher3", reason="sqlcipher3-wheels (the 'encryption' extra) is not installed"
)

from remind_me_mcp import backup as backup_mod  # noqa: E402
from remind_me_mcp import db as db_mod  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Enable encryption for the duration of one test.

    Patches `db_mod.DB_ENCRYPTION_KEY` directly rather than `config.
    DB_ENCRYPTION_KEY` -- `db.py` does `from remind_me_mcp.config import
    (..., DB_ENCRYPTION_KEY, ...)`, which binds its own name in db.py's
    namespace at import time (ordinary Python `from X import Y` semantics),
    so patching the config module's attribute alone would not be observed
    by `_open_db_connection`/`_sqlite_driver_errors`, both of which read
    the name bound in db.py's own namespace.

    The key deliberately contains a single quote, so every test using this
    fixture also exercises `_quote_sql_string`'s escaping for free.
    """
    key = "correct-test-key-with-a-'-quote"
    monkeypatch.setattr(db_mod, "DB_ENCRYPTION_KEY", key)
    return key


# ---------------------------------------------------------------------------
# Default (unset key) behavior -- must be provably unchanged
# ---------------------------------------------------------------------------


def test_default_unset_key_opens_a_plain_sqlite_connection(tmp_path: Path) -> None:
    """With DB_ENCRYPTION_KEY unset, _open_db_connection returns a genuine
    sqlite3.Connection -- not merely something API-compatible with one --
    proving the encrypted branch is never reached by default."""
    assert db_mod.DB_ENCRYPTION_KEY is None  # sanity: this file's autouse state

    path = tmp_path / "plain.db"
    conn = db_mod._open_db_connection(str(path))
    try:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.commit()
    finally:
        conn.close()


def test_default_sqlite_driver_errors_is_just_sqlite3_error() -> None:
    """_sqlite_driver_errors() must be exactly (sqlite3.Error,) when the key
    is unset -- the exact pre-#184 exception-handling contract, unchanged."""
    assert db_mod.DB_ENCRYPTION_KEY is None
    assert db_mod._sqlite_driver_errors() == (sqlite3.Error,)


def test_default_row_factory_class_is_sqlite3_row() -> None:
    """_row_factory_class() must be exactly sqlite3.Row when the key is
    unset -- the exact pre-#184 row_factory, unchanged."""
    assert db_mod.DB_ENCRYPTION_KEY is None
    assert db_mod._row_factory_class() is sqlite3.Row


def test_unencrypted_end_to_end_regression(tmp_path: Path) -> None:
    """A meaningful functional slice, not just a boolean: full schema
    creation, insert, and read through _open_db_connection with the flag
    untouched -- exactly the pre-#184 behavior _get_db() itself relies on.
    (tests/test_db.py and tests/test_backup.py are the full regression
    check for this; this is a lightweight in-file guard.)"""
    path = tmp_path / "regress.db"
    conn = db_mod._open_db_connection(str(path), timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        db_mod._ensure_schema(conn)
        now = db_mod._now_iso()
        mem_id = db_mod._make_id("hello world")
        conn.execute(
            "INSERT INTO memories (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (mem_id, "hello world", now, now),
        )
        conn.commit()
        row = conn.execute("SELECT content FROM memories WHERE id = ?", (mem_id,)).fetchone()
        assert row["content"] == "hello world"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Encrypted round-trip
# ---------------------------------------------------------------------------


def test_encrypted_round_trip_correct_key_reads_data(
    encryption_key: str, tmp_path: Path
) -> None:
    """Data written through an encrypted connection reads back correctly
    through another connection opened with the same key."""
    path = tmp_path / "enc.db"

    conn = db_mod._open_db_connection(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('secret-value')")
    conn.commit()
    conn.close()

    reader = db_mod._open_db_connection(str(path))
    try:
        rows = reader.execute("SELECT x FROM t").fetchall()
        assert rows == [("secret-value",)]
    finally:
        reader.close()


def test_row_factory_class_matches_the_active_driver(
    encryption_key: str, tmp_path: Path
) -> None:
    """Regression guard: sqlite3.Row's constructor requires an actual
    sqlite3.Cursor, so assigning it as row_factory on an sqlcipher3
    connection raises TypeError on first dict-style access -- discovered
    while writing this file, not theoretical. `_row_factory_class()` must
    pick sqlcipher3's own Row class whenever DB_ENCRYPTION_KEY is set, so
    every `row["column"]`-style access elsewhere in this codebase keeps
    working under encryption."""
    assert db_mod._row_factory_class() is sqlcipher.dbapi2.Row

    path = tmp_path / "enc.db"
    conn = db_mod._open_db_connection(str(path))
    conn.row_factory = db_mod._row_factory_class()
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('dict-style-value')")
    conn.commit()
    row = conn.execute("SELECT * FROM t").fetchone()
    assert row["x"] == "dict-style-value"
    conn.close()


def test_encrypted_file_is_not_plaintext_readable(
    encryption_key: str, tmp_path: Path
) -> None:
    """The raw .db file on disk cannot be opened as plaintext SQLite --
    proof the file is actually encrypted, not merely gated in-process."""
    path = tmp_path / "enc.db"

    conn = db_mod._open_db_connection(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('secret-value')")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        plain = sqlite3.connect(str(path))
        try:
            plain.execute("SELECT * FROM t").fetchall()
        finally:
            plain.close()


def test_encrypted_wrong_key_fails_cleanly(
    encryption_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening with the wrong key raises a clean database error -- it must
    never silently return garbage rows instead."""
    path = tmp_path / "enc.db"

    conn = db_mod._open_db_connection(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('secret-value')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_mod, "DB_ENCRYPTION_KEY", "totally-wrong-key")
    wrong = db_mod._open_db_connection(str(path))
    try:
        with pytest.raises(sqlcipher.dbapi2.DatabaseError):
            wrong.execute("SELECT * FROM t").fetchall()
    finally:
        wrong.close()


def test_missing_dependency_raises_clear_runtime_error(
    encryption_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If DB_ENCRYPTION_KEY is set but sqlcipher3 can't be imported,
    _open_db_connection must fail loudly with an actionable message rather
    than silently falling back to plaintext."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlcipher3" or name.startswith("sqlcipher3."):
            raise ImportError("simulated: sqlcipher3 not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    with pytest.raises(RuntimeError, match=r"sqlcipher3-wheels.*not installed"):
        db_mod._open_db_connection(str(tmp_path / "enc.db"))


def _load_vec_extension_if_available(conn: sqlite3.Connection) -> None:
    """Mirror _get_db()'s optional sqlite-vec extension loading.

    Tests that call `_ensure_schema` directly on a connection opened via
    `_open_db_connection` (bypassing `_get_db`'s own thread-cache
    machinery) need this first, exactly like `_get_db` does it, so
    `_ensure_schema`'s `CREATE VIRTUAL TABLE ... vec0` step succeeds
    instead of raising "no such module: vec0" -- which, on an *encrypted*
    connection, is exactly the documented sqlcipher3-exception-hierarchy
    gap (ARCHITECTURE.md) and would fail these tests for an unrelated
    reason (a test setup gap, not a real code-path bug: production
    `_get_db()` always loads the extension before calling `_ensure_schema`).
    """
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        with contextlib.suppress(Exception):  # best-effort, mirrors _get_db's own tolerance
            sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Backup path
# ---------------------------------------------------------------------------


def test_backup_of_encrypted_db_is_itself_encrypted(
    encryption_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_backup on a live encrypted connection produces a backup file
    that also requires the key -- not a plaintext copy."""
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", tmp_path / "backups")

    live_path = tmp_path / "live.db"
    live = db_mod._open_db_connection(str(live_path))
    live.row_factory = db_mod._row_factory_class()
    live.execute("CREATE TABLE t (x TEXT)")
    live.execute("INSERT INTO t VALUES ('backed-up-secret')")
    live.commit()

    backup_path = backup_mod.create_backup(live, label="enc-test")
    live.close()

    assert backup_path.exists()

    # Not plaintext-readable.
    with pytest.raises(sqlite3.DatabaseError):
        plain = sqlite3.connect(str(backup_path))
        try:
            plain.execute("SELECT * FROM t").fetchall()
        finally:
            plain.close()

    # Readable with the same key.
    reader = db_mod._open_db_connection(str(backup_path))
    try:
        rows = reader.execute("SELECT x FROM t").fetchall()
        assert rows == [("backed-up-secret",)]
    finally:
        reader.close()


def test_validate_backup_file_accepts_encrypted_backup_with_correct_key(
    encryption_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restore's validation step opens and integrity-checks an encrypted
    backup successfully when the configured key is correct."""
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", tmp_path / "backups")

    live_path = tmp_path / "live.db"
    live = db_mod._open_db_connection(str(live_path))
    live.row_factory = db_mod._row_factory_class()
    _load_vec_extension_if_available(live)
    db_mod._ensure_schema(live)
    now = db_mod._now_iso()
    live.execute(
        "INSERT INTO memories (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("abc123", "hi", now, now),
    )
    live.commit()

    backup_path = backup_mod.create_backup(live, label="valid-test")
    live.close()

    # Must not raise.
    backup_mod._validate_backup_file(backup_path)


def test_validate_backup_file_rejects_wrong_key(
    encryption_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restore's validation step raises a clean RestoreError -- not a raw
    sqlcipher3 exception -- when the configured key can't decrypt the
    backup (issue #184's backup.py exception-hierarchy fix)."""
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", tmp_path / "backups")

    live_path = tmp_path / "live.db"
    live = db_mod._open_db_connection(str(live_path))
    live.row_factory = db_mod._row_factory_class()
    _load_vec_extension_if_available(live)
    db_mod._ensure_schema(live)
    live.commit()
    backup_path = backup_mod.create_backup(live, label="wrongkey-test")
    live.close()

    monkeypatch.setattr(db_mod, "DB_ENCRYPTION_KEY", "not-the-right-key")

    with pytest.raises(backup_mod.RestoreError):
        backup_mod._validate_backup_file(backup_path)


def test_validate_backup_file_rejects_plaintext_backup_when_key_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plaintext backup (e.g. taken before encryption was enabled) cannot
    be validated/restored while a key is configured -- the documented v1
    adoption-story limitation in ARCHITECTURE.md, not a crash."""
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", tmp_path / "backups")

    # Create a plaintext backup with encryption off.
    assert db_mod.DB_ENCRYPTION_KEY is None
    plain_path = tmp_path / "plain.db"
    plain = db_mod._open_db_connection(str(plain_path))
    plain.row_factory = sqlite3.Row
    db_mod._ensure_schema(plain)
    plain.commit()
    backup_path = backup_mod.create_backup(plain, label="plaintext-test")
    plain.close()

    # Now turn encryption on and try to validate that same (plaintext) file.
    monkeypatch.setattr(db_mod, "DB_ENCRYPTION_KEY", "some-key")

    with pytest.raises(backup_mod.RestoreError):
        backup_mod._validate_backup_file(backup_path)
