"""
The declared package version must match the newest RELEASE_NOTES entry.

This exists because they silently diverged: `pyproject.toml` sat at 1.19.0
(last set 2026-07-21) while `RELEASE_NOTES.md` accumulated seven entries
beneath it. Nothing caught it, because nothing compared them.

The drift is user-visible rather than cosmetic. `remind_me_mcp.__version__`
resolves from installed package metadata, which comes from `pyproject.toml`,
so `remind_me_check_update` and every status surface reported a version seven
releases stale — including immediately after a successful update, which is
exactly when an operator is looking at that number to confirm the update
worked.

Unlike the BACKLOG-drift class of problem (prose that stops being true, which
no test can check), this one is a mechanical equality and cheap to enforce.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
RELEASE_NOTES = ROOT / "RELEASE_NOTES.md"

# "## v1.20.0 — 2026-07-30" (em dash) — capture just the version.
_HEADING = re.compile(r"^##\s+v(?P<version>\d+\.\d+\.\d+)\b", re.MULTILINE)


def _declared_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def _release_note_versions() -> list[str]:
    return _HEADING.findall(RELEASE_NOTES.read_text(encoding="utf-8"))


def test_pyproject_version_matches_latest_release_note() -> None:
    """The declared version is the one the newest release note documents."""
    declared = _declared_version()
    versions = _release_note_versions()
    assert versions, "RELEASE_NOTES.md has no '## vX.Y.Z' headings to compare against"
    latest = versions[0]
    assert declared == latest, (
        f"pyproject.toml declares {declared} but the newest RELEASE_NOTES entry "
        f"is v{latest}. Bump the version in the same PR as the release note — "
        "__version__ comes from package metadata, so a stale value is reported "
        "by remind_me_check_update and every status surface."
    )


def test_release_note_versions_are_ordered_newest_first() -> None:
    """Newest entry is at the top, so 'latest' above is well defined.

    Without this, prepending an out-of-order entry would silently redefine
    what the check above compares against.
    """
    versions = _release_note_versions()
    parsed = [tuple(int(p) for p in v.split(".")) for v in versions]
    assert parsed == sorted(parsed, reverse=True), (
        "RELEASE_NOTES.md entries are not in descending version order: "
        f"{versions[:8]}"
    )


def test_declared_version_is_importable_metadata() -> None:
    """`__version__` resolves rather than falling back to the dev sentinel.

    A '0.0.0-dev' here means the package isn't installed in the test env, so
    the equality check above would be comparing against something users never
    see.
    """
    from remind_me_mcp import __version__

    assert __version__ != "0.0.0-dev", (
        "remind_me_mcp is not installed (metadata lookup fell back to the dev "
        "sentinel) — install with `uv sync` so the version check is meaningful"
    )
