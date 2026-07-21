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
