"""
Sanity checks for docs/openapi.yaml (cognee gap #7, Phase 8): the spec must
be valid OpenAPI 3.0 and cover every route actually registered in api.py, so
it can't silently drift out of sync with the real REST surface.

pytest.importorskip keeps both checks optional (PyYAML is only a transitive
dependency; openapi-spec-validator isn't a project dependency at all — it's
a doc-authoring aid, not something remind_me needs at runtime or in every
CI leg).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


def _load_spec():
    yaml = pytest.importorskip("yaml")
    with _SPEC_PATH.open() as f:
        return yaml.safe_load(f)


def test_openapi_spec_is_valid() -> None:
    pytest.importorskip("yaml")
    validator_mod = pytest.importorskip("openapi_spec_validator")
    spec = _load_spec()
    validator_mod.validate(spec)


def test_openapi_spec_covers_every_registered_route() -> None:
    """Every Starlette route path in api.py (minus the dashboard's own "/")
    appears in the spec, so an added/removed route can't silently drift out
    of sync with this hand-maintained doc."""
    from remind_me_mcp.api import _build_api_app

    app = _build_api_app()
    registered_paths = {
        route.path for route in app.routes if hasattr(route, "path")
    } - {"/"}

    spec = _load_spec()
    spec_paths = set(spec["paths"])

    assert registered_paths == spec_paths
