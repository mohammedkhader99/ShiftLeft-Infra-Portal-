"""Increment 0.2 checks: the one-command run is wired correctly.

These validate docker-compose.yml is well-formed and declares both the `db`
and `api` services, so a broken compose file is caught automatically. If the
Docker CLI is not installed, the CLI-dependent check is skipped rather than
failing (the plain-text checks still run).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"


def test_compose_file_exists():
    assert COMPOSE_FILE.is_file(), "docker-compose.yml should exist at the project root"


def test_compose_declares_both_services():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "postgres:16" in text, "Postgres 16 image should be pinned (ARCHITECTURE.md §9)"
    assert "db:" in text, "compose should declare a 'db' service"
    assert "api:" in text, "compose should declare an 'api' service"


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="Docker CLI not available on this machine"
)
def test_compose_config_is_valid():
    # `docker compose config` parses and validates the file (and the .env it reads).
    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"compose file is invalid:\n{result.stderr}"
    services = result.stdout.split()
    assert "db" in services
    assert "api" in services
