"""
Tests for optional cloud upload of backups (issue #196).

Everything past the `pytest.importorskip` below requires the optional
`boto3` package (the `cloud-backup` extra) to be installed -- mirroring the
`[pdf]`/`[image]`/`[encryption]` extras' own `pytest.importorskip` pattern
elsewhere in this suite, so this whole file cleanly *skips*, not fails, in
an environment without the extra.

`boto3.client` is monkeypatched to a stub factory in every test below --
no real network calls are ever made, and no real AWS/S3-compatible
credentials are required to run this file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

boto3 = pytest.importorskip("boto3", reason="boto3 (the 'cloud-backup' extra) is not installed")

from remind_me_mcp import backup as backup_mod  # noqa: E402
from remind_me_mcp import cloud_backup as cloud_backup_mod  # noqa: E402

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Stand-in for a real boto3 S3 client -- records every upload_file call."""

    def __init__(self, *, raise_on_upload: Exception | None = None) -> None:
        self.upload_calls: list[tuple[str, str, str]] = []
        self._raise_on_upload = raise_on_upload

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        if self._raise_on_upload is not None:
            raise self._raise_on_upload
        self.upload_calls.append((filename, bucket, key))


@pytest.fixture()
def fake_boto3_client(monkeypatch: pytest.MonkeyPatch):
    """Patch boto3.client to return a recording fake, and record how
    boto3.client itself was called (positional args + kwargs) so tests can
    assert endpoint_url/region_name are wired through correctly."""
    calls: list[dict[str, Any]] = []
    client = _FakeS3Client()

    def _fake_client(service_name: str, **kwargs: Any) -> _FakeS3Client:
        calls.append({"service_name": service_name, **kwargs})
        return client

    monkeypatch.setattr(boto3, "client", _fake_client)
    return calls, client


@pytest.fixture(autouse=True)
def _reset_cloud_backup_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from cloud backup fully unconfigured/off, exactly
    like the rest of the suite's default state -- explicit per-test opt-in
    below, mirroring test_db_encryption.py's `encryption_key` fixture
    pattern rather than relying on whatever the real environment has set."""
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_PREFIX", "")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_ENDPOINT_URL", None)
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_REGION", None)
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD", False)
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", None)


# ---------------------------------------------------------------------------
# No-op when unconfigured
# ---------------------------------------------------------------------------


def test_upload_backup_is_a_noop_when_bucket_unconfigured(
    tmp_path: Path, fake_boto3_client
) -> None:
    """With BACKUP_S3_BUCKET unset (the default), upload_backup does nothing
    -- boto3.client is never even called."""
    calls, _client = fake_boto3_client
    path = tmp_path / "manual-20260101T000000000000Z.db"
    path.write_bytes(b"irrelevant")

    cloud_backup_mod.upload_backup(path)

    assert calls == []


def test_create_backup_succeeds_when_boto3_client_absent(
    monkeypatch: pytest.MonkeyPatch, db_conn: sqlite3.Connection
) -> None:
    """A bucket left unconfigured must not affect create_backup's own
    success or return value -- the regression this hook must never cause."""
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "")
    path = backup_mod.create_backup(db_conn, label="manual")
    assert path.exists()


# ---------------------------------------------------------------------------
# Plaintext-upload gate
# ---------------------------------------------------------------------------


def test_plaintext_upload_refused_without_allow_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client, caplog
) -> None:
    """DB_ENCRYPTION_KEY unset and the allow-flag unset: upload is refused
    with a clear error, and boto3 is never actually called."""
    calls, _client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", None)
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD", False)

    path = tmp_path / "manual-plaintext.db"
    path.write_bytes(b"plaintext-backup-bytes")

    with caplog.at_level(logging.ERROR, logger="remind_me_mcp.cloud_backup"):
        cloud_backup_mod.upload_backup(path)  # must not raise

    assert calls == []
    assert "REMIND_ME_BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD" in caplog.text


def test_plaintext_upload_proceeds_with_allow_flag_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """DB_ENCRYPTION_KEY unset but the allow-flag explicitly set: upload
    proceeds, and boto3 IS called."""
    calls, client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", None)
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD", True)

    path = tmp_path / "manual-plaintext.db"
    path.write_bytes(b"plaintext-backup-bytes")

    cloud_backup_mod.upload_backup(path)

    assert len(calls) == 1
    assert client.upload_calls == [(str(path), "test-bucket", path.name)]


def test_encrypted_backup_uploads_without_allow_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """DB_ENCRYPTION_KEY set (issue #184): upload proceeds even though the
    plaintext-allow flag is left unset -- an already-ciphertext backup is
    safe to upload by default."""
    calls, client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "some-encryption-key")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD", False)

    path = tmp_path / "manual-encrypted.db"
    path.write_bytes(b"sqlcipher-ciphertext-bytes")

    cloud_backup_mod.upload_backup(path)

    assert len(calls) == 1
    assert client.upload_calls == [(str(path), "test-bucket", path.name)]


# ---------------------------------------------------------------------------
# Missing boto3 dependency
# ---------------------------------------------------------------------------


def test_missing_boto3_logs_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """If BACKUP_S3_BUCKET is set but boto3 can't be imported, upload_backup
    must fail loudly in the log but never raise out to its caller."""
    import builtins

    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError("simulated: boto3 not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    path = tmp_path / "manual-nodep.db"
    path.write_bytes(b"some-bytes")

    with caplog.at_level(logging.ERROR, logger="remind_me_mcp.cloud_backup"):
        cloud_backup_mod.upload_backup(path)  # must not raise

    assert "cloud-backup" in caplog.text


# ---------------------------------------------------------------------------
# Upload failure discipline
# ---------------------------------------------------------------------------


def test_upload_failure_does_not_propagate_out_of_create_backup(
    monkeypatch: pytest.MonkeyPatch, db_conn: sqlite3.Connection, caplog
) -> None:
    """A boto3/network failure during upload is logged, not raised -- and
    the local backup file create_backup already wrote stays exactly as
    valid as it would have been with cloud upload unconfigured."""

    def _raising_client(service_name: str, **kwargs: Any) -> _FakeS3Client:
        return _FakeS3Client(raise_on_upload=ConnectionError("simulated network failure"))

    monkeypatch.setattr(boto3, "client", _raising_client)
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    with caplog.at_level(logging.ERROR, logger="remind_me_mcp.cloud_backup"):
        path = backup_mod.create_backup(db_conn, label="manual")  # must not raise

    assert path.exists()
    # Still a genuine, independently-readable SQLite database.
    import sqlite3 as sqlite3_module

    conn = sqlite3_module.connect(str(path))
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    assert "simulated network failure" in caplog.text


# ---------------------------------------------------------------------------
# Key naming / prefix
# ---------------------------------------------------------------------------


def test_upload_key_uses_configured_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """The uploaded object key is `<prefix>/<local filename>` -- cloud and
    local backups correspond 1:1 by name."""
    calls, client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_PREFIX", "my-host/backups")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    path = tmp_path / "manual-20260101T000000000000Z.db"
    path.write_bytes(b"bytes")

    cloud_backup_mod.upload_backup(path)

    assert client.upload_calls == [
        (str(path), "my-bucket", f"my-host/backups/{path.name}")
    ]


def test_upload_key_has_no_leading_slash_when_prefix_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """A prefix with stray leading/trailing slashes still produces a clean
    key -- no double slashes, no leading slash."""
    calls, client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_PREFIX", "/my-host/backups/")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    path = tmp_path / "manual.db"
    path.write_bytes(b"bytes")

    cloud_backup_mod.upload_backup(path)

    assert client.upload_calls == [
        (str(path), "my-bucket", f"my-host/backups/{path.name}")
    ]


def test_upload_key_no_prefix_uploads_at_bucket_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    calls, client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_PREFIX", "")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    path = tmp_path / "manual.db"
    path.write_bytes(b"bytes")

    cloud_backup_mod.upload_backup(path)

    assert client.upload_calls == [(str(path), "my-bucket", path.name)]


# ---------------------------------------------------------------------------
# endpoint_url / region_name wiring (non-AWS S3-compatible endpoints)
# ---------------------------------------------------------------------------


def test_client_receives_configured_endpoint_and_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """REMIND_ME_BACKUP_S3_ENDPOINT_URL/_REGION are actually passed through
    to boto3.client(...) -- not merely accepted and silently ignored. This
    is what makes non-AWS providers (Backblaze B2, MinIO, ...) work."""
    calls, _client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setattr(
        cloud_backup_mod,
        "BACKUP_S3_ENDPOINT_URL",
        "https://s3.us-west-002.backblazeb2.com",
    )
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_REGION", "us-west-002")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    path = tmp_path / "manual.db"
    path.write_bytes(b"bytes")

    cloud_backup_mod.upload_backup(path)

    assert len(calls) == 1
    assert calls[0]["service_name"] == "s3"
    assert calls[0]["endpoint_url"] == "https://s3.us-west-002.backblazeb2.com"
    assert calls[0]["region_name"] == "us-west-002"


def test_client_omits_endpoint_and_region_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_boto3_client
) -> None:
    """Unset endpoint/region (real AWS S3, the default) are not passed to
    boto3.client at all -- letting boto3 fall back to its own defaults/
    credential-chain-resolved region, rather than passing an explicit None
    that could override a value boto3 would otherwise have resolved itself."""
    calls, _client = fake_boto3_client
    monkeypatch.setattr(cloud_backup_mod, "BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setattr(cloud_backup_mod, "DB_ENCRYPTION_KEY", "a-key")

    path = tmp_path / "manual.db"
    path.write_bytes(b"bytes")

    cloud_backup_mod.upload_backup(path)

    assert len(calls) == 1
    assert "endpoint_url" not in calls[0]
    assert "region_name" not in calls[0]
