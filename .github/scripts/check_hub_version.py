#!/usr/bin/env python3
"""Fail a pull request that changes ``hub/main.py`` without bumping HUB_VERSION.

``HUB_VERSION`` has to be a hand-maintained literal -- the hub's container
image holds ``main.py`` and nothing else, so there is no ``pyproject.toml``,
git checkout, or build metadata inside it to derive a version from. Hand-
maintained constants rot, and a stale hub version is strictly worse than no
version at all: ``/health`` then reports a build identifier that operators,
``setup.sh``'s rollover check, and ``remind_me_sync_reconcile`` all trust,
while pointing at code that is no longer deployed. This is the enforcement
the constant's own documented bump rule was otherwise missing.

Deliberately a nag with an escape hatch, not an absolute gate: plenty of
edits to that file (a comment, a docstring, a test-only refactor) change
nothing a client could observe, and forcing a version bump for those would
make the version churn -- the exact noise that erodes trust in it. Either
mechanism opts out:

  * the ``no-hub-version-bump`` label on the pull request, or
  * a ``[skip hub-version]`` line anywhere in the pull request body.

The decision logic lives in :func:`decide`, kept free of git and environment
access so ``tests/test_hub_version.py`` can exercise it directly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HUB_FILE = "hub/main.py"
SKIP_MARKER = "[skip hub-version]"
SKIP_LABEL = "no-hub-version-bump"

# Matches the assignment only at column 0, so the identical string inside a
# docstring or a comment can't be mistaken for the declaration.
_VERSION_RE = re.compile(r'^HUB_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def extract_version(source: str | None) -> str | None:
    """Return the HUB_VERSION literal in *source*, or None if absent."""
    if source is None:
        return None
    match = _VERSION_RE.search(source)
    return match.group(1) if match else None


def decide(
    base_source: str | None, head_source: str | None, *, skip: bool
) -> tuple[bool, str]:
    """Decide whether this change satisfies the bump rule.

    Args:
        base_source: ``hub/main.py`` at the merge base, or None if it did not
            exist there.
        head_source: ``hub/main.py`` on the branch, or None if it was deleted.
        skip: Whether an opt-out label or marker was present.

    Returns:
        ``(ok, message)`` -- *message* is printed either way, so a pass that
        was only a pass because of the escape hatch still says so.
    """
    if head_source is None:
        return True, f"{HUB_FILE} does not exist on this branch — nothing to check."
    if base_source is None:
        return True, f"{HUB_FILE} is new on this branch — no previous version to bump."
    if base_source == head_source:
        return True, f"{HUB_FILE} unchanged — no bump needed."

    head_version = extract_version(head_source)
    if head_version is None:
        return False, (
            f"{HUB_FILE} changed but no `HUB_VERSION = \"...\"` assignment was "
            "found. Every endpoint reports it; it must not disappear."
        )

    base_version = extract_version(base_source)
    if base_version is None:
        return True, f"{HUB_FILE} changed and HUB_VERSION was introduced ({head_version})."
    if base_version != head_version:
        return True, (
            f"{HUB_FILE} changed and HUB_VERSION was bumped "
            f"({base_version} -> {head_version})."
        )

    if skip:
        return True, (
            f"{HUB_FILE} changed with HUB_VERSION left at {head_version}, "
            f"opted out via `{SKIP_LABEL}` / `{SKIP_MARKER}`."
        )

    return False, (
        f"{HUB_FILE} changed but HUB_VERSION is still {head_version}.\n"
        "\n"
        "The hub's container image has no metadata to derive a version from, so\n"
        "this literal is the only thing identifying the deployed build — to\n"
        "operators, to setup.sh's rollover check, and to remind_me_sync_reconcile.\n"
        "Bump it (see its docstring in hub/main.py):\n"
        "\n"
        "  MAJOR  a wire-protocol break\n"
        "  MINOR  a new endpoint or response field\n"
        "  PATCH  a fix nothing a client can key off\n"
        "\n"
        f"If this change genuinely alters nothing observable, opt out with the\n"
        f"`{SKIP_LABEL}` label or a `{SKIP_MARKER}` line in the PR body."
    )


def _git_show(rev: str, path: str) -> str | None:
    """Read *path* at *rev*, or None when it doesn't exist there."""
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _skip_requested() -> bool:
    """Check the PR body and labels for an opt-out.

    Both arrive via the environment rather than as arguments: interpolating
    a pull request body straight into a workflow's shell is a script
    injection, and it is attacker-controlled text on a fork PR.
    """
    if SKIP_MARKER.lower() in os.environ.get("PR_BODY", "").lower():
        return True
    raw = os.environ.get("PR_LABELS", "")
    if not raw.strip():
        return False
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to a plain comma/space-separated list rather than failing
        # the build over an unexpected payload shape.
        labels = raw.replace(",", " ").split()
    return any(str(name).strip() == SKIP_LABEL for name in labels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Merge-base ref or SHA")
    parser.add_argument("--head", default="HEAD", help="Branch ref or SHA")
    args = parser.parse_args()

    ok, message = decide(
        _git_show(args.base, HUB_FILE),
        _git_show(args.head, HUB_FILE),
        skip=_skip_requested(),
    )
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
