"""
Sanity checks for hub/deploy/*'s alternative deploy templates (cognee gap
#8, Phase 8): each file must at least be syntactically valid in its format,
and share the env var contract hub/main.py actually reads (DATABASE_URL,
SYNC_SECRET).

These are static file checks, not deploy-tool integration tests (no docker/
fly/railway CLI invoked) — pytest.importorskip("yaml") keeps the compose
check optional, matching the chromadb/sqlite-vec/opentelemetry convention,
since PyYAML is only a transitive dependency (chromadb/huggingface-hub) and
CI's "base" leg installs neither.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parent.parent / "hub" / "deploy"


def test_docker_compose_is_valid_yaml_with_expected_services() -> None:
    yaml = pytest.importorskip("yaml")
    with (_DEPLOY_DIR / "docker-compose.yml").open() as f:
        data = yaml.safe_load(f)

    services = data["services"]
    assert "remind-me-hub" in services
    assert "remind-me-postgres" in services

    hub = services["remind-me-hub"]
    assert hub["build"] == {"context": "..", "dockerfile": "Containerfile"}
    assert hub["env_file"] == "hub.env"
    # Localhost-only by default, matching the quadlet posture.
    assert any(
        str(p).startswith("127.0.0.1:") for p in hub["ports"]
    ), f"expected a 127.0.0.1-bound port, got {hub['ports']}"

    postgres = services["remind-me-postgres"]
    healthcheck_test = postgres["healthcheck"]["test"]
    # $$POSTGRES_USER (escaped, single literal $) is deliberate: Compose's
    # own ${...} interpolation never sees env_file: values (those only
    # reach the container's runtime env), so a single-dollar
    # ${POSTGRES_USER} here would silently always resolve to nothing at
    # Compose parse time -- before postgres.env is ever read. Guard against
    # that regressing back in.
    assert "$$POSTGRES_USER" in healthcheck_test[-1]
    assert "${POSTGRES_USER" not in healthcheck_test[-1]


def test_fly_toml_is_valid_and_uses_the_containerfile() -> None:
    with (_DEPLOY_DIR / "fly.toml").open("rb") as f:
        data = tomllib.load(f)

    assert data["build"]["dockerfile"] == "Containerfile"
    # No public [[services]] block -- reachable only over Fly's private
    # network, matching the "don't expose the hub publicly" posture.
    assert "services" not in data


def test_railway_json_is_valid_and_uses_the_containerfile() -> None:
    with (_DEPLOY_DIR / "railway.json").open() as f:
        data = json.load(f)

    assert data["build"]["builder"] == "DOCKERFILE"
    assert data["build"]["dockerfilePath"] == "Containerfile"
    assert data["deploy"]["healthcheckPath"] == "/health"


def test_deploy_readme_documents_all_three_targets() -> None:
    text = (_DEPLOY_DIR / "README.md").read_text()
    for target in ("docker-compose.yml", "fly.toml", "railway.json"):
        assert target in text


# ---------------------------------------------------------------------------
# Image identity and rollover verification (issues #212, #213)
# ---------------------------------------------------------------------------

_HUB_DIR = _DEPLOY_DIR.parent


def test_containerfile_labels_carry_the_version() -> None:
    """A stopped or crash-looping hub can't be asked over HTTP.

    That is exactly when "which build is this?" matters, so the image has to
    answer it too — via `podman inspect`, from a build-arg fed by setup.sh.
    """
    text = (_HUB_DIR / "Containerfile").read_text()
    assert "ARG HUB_VERSION" in text
    assert "org.opencontainers.image.version=" in text
    assert "${HUB_VERSION}" in text


def test_containerfile_version_arg_defaults_to_unknown() -> None:
    """A hand-built image that skipped the build-arg must not claim a version.

    Defaulting to a number would make `podman inspect` confidently report a
    build the image may not contain — the same stale-but-authoritative
    failure the HUB_VERSION constant exists to avoid.
    """
    text = (_HUB_DIR / "Containerfile").read_text()
    assert "ARG HUB_VERSION=unknown" in text


def test_setup_tags_the_image_with_the_version_and_latest() -> None:
    """Both tags: `latest` for the quadlet, the version for rollback.

    Every build overwriting `latest` alone leaves nothing to roll back to,
    so a bad hub deploy could only be fixed by rebuilding from an older
    checkout.
    """
    text = (_HUB_DIR / "setup.sh").read_text()
    assert "hub_version_from_source" in text
    assert '-t "remind-me-hub:$version"' in text
    assert "-t remind-me-hub:latest" in text
    assert '--build-arg "HUB_VERSION=$version"' in text


def test_update_verifies_the_new_version_is_actually_serving() -> None:
    """wait_for_hub proves *something* answers, not that it's the new build.

    Without this check the classic bad deploy — failed build or no-op
    restart, old process still serving — reports success.
    """
    text = (_HUB_DIR / "setup.sh").read_text()
    assert "live_hub_version" in text
    # Compared against the version just pulled, not the one seen before the
    # restart: an update that touched no hub code is *expected* to leave it
    # unchanged, and failing on that would be its own false alarm.
    assert "expected=$(hub_version_from_source)" in text
    assert "the new image is not serving" in text
