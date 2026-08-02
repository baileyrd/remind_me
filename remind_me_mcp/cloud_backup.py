"""
remind_me_mcp.cloud_backup — Optional cloud upload of local backups (issue #196).

A post-backup hook, not a replacement for or a race with `backup.py`'s
local-first, atomic-rename-to-final-name discipline (see its module
docstring): `backup.create_backup` calls :func:`upload_backup` only after
`os.replace(tmp_dest, dest)` has already completed, i.e. only once the local
backup file is fully finalized under its real name. Nothing here can affect
whether `list_backups`/`_prune_old_backups` see a half-written file, and a
failed or misconfigured upload never undoes or blocks the local backup that
already succeeded.

**Provider.** `boto3` (the AWS SDK) talks to S3 and, via a configurable
`endpoint_url`, essentially every S3-compatible object-storage provider too
-- Backblaze B2, a self-hosted MinIO, etc. -- so one client/dependency
covers most providers instead of needing a per-provider integration. Gated
behind the optional `cloud-backup` extra (`pip install
remind-me-mcp[cloud-backup]`); the base install has no dependency on it, and
this module's import of `boto3` is deferred to the moment an upload is
actually attempted, mirroring `pdf_import.py`/`image_import.py`/`db.py`'s
"only actually needed extra is imported at the point of use" discipline. A
missing `boto3` raises a clear, actionable error internally -- but see below,
it is always logged, never allowed to propagate out of `create_backup`.

**Credentials.** Deliberately no `REMIND_ME_BACKUP_S3_*` credential env var.
`boto3` already has its own standard credential resolution chain
(`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars, the shared
`~/.aws/credentials` file, an EC2/ECS/Lambda instance role, ...) -- this
module relies on it as-is rather than reinventing a parallel bespoke
credential config, which would be worse, not better (one more secret-storage
convention to get right, with none of boto3's existing hardening).

**The plaintext-upload gate.** Per ARCHITECTURE.md's "Encryption at rest"
section (issue #184): when `REMIND_ME_DB_ENCRYPTION_KEY` is set, the local
backup file `create_backup` just wrote is already SQLCipher ciphertext (a
byte-for-byte fact, not an assumption -- confirmed by issue #184's own
`test_encrypted_file_is_not_plaintext_readable` test), so uploading it
as-is to a third-party bucket is safe by default. When it is *not* set, the
backup file is plaintext personal data, and uploading it introduces a real,
distinct risk this feature would otherwise add silently. Uploading a
plaintext backup therefore requires the explicit
`REMIND_ME_BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD` opt-in; without it, the upload
is refused with a clear explanatory error (logged, not raised -- see below)
before `boto3` is ever touched.

**Failure discipline.** Mirrors `notifications.py`'s "a failed or
unconfigured [channel] must never raise out of the calling path" rule.
:func:`upload_backup` is the only function `backup.create_backup` calls, and
it never raises: a missing `boto3`, the plaintext gate refusing, or any
`boto3`/network failure during the actual upload is logged at `error` level
and swallowed. The local backup is the primary guarantee this codebase makes
(see issue #17/#149); cloud upload is strictly a best-effort enhancement on
top of it, never a condition of it succeeding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import (
    BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD,
    BACKUP_S3_BUCKET,
    BACKUP_S3_ENDPOINT_URL,
    BACKUP_S3_PREFIX,
    BACKUP_S3_REGION,
    DB_ENCRYPTION_KEY,
)

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("remind_me_mcp.cloud_backup")

CLOUD_BACKUP_EXTRA_INSTALL_MSG = (
    "REMIND_ME_BACKUP_S3_BUCKET is set but the optional 'boto3' dependency is not "
    "installed. Install it with `pip install remind-me-mcp[cloud-backup]`, or unset "
    "REMIND_ME_BACKUP_S3_BUCKET to disable cloud backup upload."
)
"""User-facing error message for a missing boto3 dependency (matches the
"install this extra" phrasing used by db.py/pdf_import.py/image_import.py)."""

PLAINTEXT_UPLOAD_REFUSED_MSG = (
    "Cloud backup upload refused: REMIND_ME_DB_ENCRYPTION_KEY is not set, so the "
    "local backup file is plaintext personal data (see ARCHITECTURE.md's "
    "'Encryption at rest' section). Uploading plaintext personal data to "
    "third-party cloud storage needs explicit consent, not silent default "
    "behavior. Set REMIND_ME_BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD=1 to upload anyway, "
    "or set REMIND_ME_DB_ENCRYPTION_KEY to encrypt backups at rest (recommended) "
    "before enabling cloud backup upload."
)


class CloudBackupError(Exception):
    """Raised internally when an upload cannot proceed.

    Always caught inside :func:`upload_backup` and logged -- never allowed to
    propagate to :func:`remind_me_mcp.backup.create_backup`. A distinct
    exception type (rather than a bare ``RuntimeError``, which
    ``_build_client`` also happens to raise for a *different* reason -- a
    missing dependency) exists only so the two internal helpers below share
    one catch clause in :func:`upload_backup`, not to distinguish failure
    modes for any caller outside this module.
    """


def _s3_key(prefix: str, filename: str) -> str:
    """Join *prefix* and *filename* into an S3 object key.

    A blank *prefix* (the default) uploads at the bucket root. A configured
    prefix has any leading/trailing slashes stripped first so
    ``REMIND_ME_BACKUP_S3_PREFIX=/my-host/backups/`` and
    ``REMIND_ME_BACKUP_S3_PREFIX=my-host/backups`` produce the identical key
    -- a bare `/`-joined result never doubles up on a leading slash the
    caller may or may not have included.
    """
    prefix = prefix.strip("/")
    return f"{prefix}/{filename}" if prefix else filename


def _check_plaintext_gate() -> None:
    """Refuse the upload unless it's safe by default or explicitly allowed.

    Safe by default when ``DB_ENCRYPTION_KEY`` is set (the backup file is
    already SQLCipher ciphertext). Otherwise requires the explicit
    ``BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD`` opt-in.

    Raises:
        CloudBackupError: if neither condition holds.
    """
    if DB_ENCRYPTION_KEY:
        return
    if BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD:
        return
    raise CloudBackupError(PLAINTEXT_UPLOAD_REFUSED_MSG)


def _build_client() -> Any:
    """Construct a boto3 S3 client from the configured endpoint/region.

    The ``boto3`` import is deferred to this call site so importing this
    module (and every other module that imports it) never requires the
    optional ``cloud-backup`` extra to be installed -- only actually
    attempting an upload does.

    Returns:
        A ``boto3`` S3 client (typed ``Any``: ``boto3`` ships no inline type
        stubs the way ``sqlcipher3-wheels`` does, so this mirrors how
        `embeddings.py`/`reranker.py` treat other stub-less optional
        dependencies rather than threading a precise type through).

    Raises:
        CloudBackupError: if ``boto3`` is not installed.
    """
    try:
        import boto3
    except ImportError as e:
        raise CloudBackupError(CLOUD_BACKUP_EXTRA_INSTALL_MSG) from e

    kwargs: dict[str, Any] = {}
    if BACKUP_S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = BACKUP_S3_ENDPOINT_URL
    if BACKUP_S3_REGION:
        kwargs["region_name"] = BACKUP_S3_REGION
    return boto3.client("s3", **kwargs)


def _upload(path: Path) -> None:
    """The actual upload, with no failure handling of its own.

    Every exception this raises (``CloudBackupError`` from the gate/missing
    dependency, or any ``boto3``/network error from the upload call itself)
    is caught by :func:`upload_backup`, the only caller.
    """
    _check_plaintext_gate()
    client = _build_client()
    key = _s3_key(BACKUP_S3_PREFIX, path.name)
    client.upload_file(str(path), BACKUP_S3_BUCKET, key)
    log.info("Uploaded backup %s to s3://%s/%s", path, BACKUP_S3_BUCKET, key)


def upload_backup(path: Path) -> None:
    """Upload *path* (an already-finalized local backup file) to cloud storage.

    A no-op when ``BACKUP_S3_BUCKET`` isn't configured, so
    ``backup.create_backup`` can call this unconditionally after every
    successful backup without checking availability itself first -- the same
    discipline ``notifications.notify`` follows for its own callers.

    Never raises: the plaintext-upload gate refusing, a missing ``boto3``,
    and any upload failure (network error, bad credentials, wrong bucket,
    ...) are all logged at ``error`` level and swallowed. This function is
    called strictly *after* the local backup file already exists and is
    valid -- that guarantee must never be put at risk by anything cloud
    upload does or fails to do.

    Args:
        path: Path to the local backup file `create_backup` just finalized.
    """
    if not BACKUP_S3_BUCKET:
        return
    try:
        _upload(path)
    except CloudBackupError as e:
        log.error(str(e))
    except Exception as e:  # noqa: BLE001 — cloud upload must never fail create_backup
        log.error(
            "Cloud backup upload of %s to s3://%s failed: %s", path, BACKUP_S3_BUCKET, e
        )


__all__ = [
    "CloudBackupError",
    "upload_backup",
]
