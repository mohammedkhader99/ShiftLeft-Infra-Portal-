"""Shared api-test fixtures."""

from pathlib import Path

import pytest

from api import blueprint_capabilities, network_egress, settings

_BLUEPRINT_DIR = Path(__file__).resolve().parents[2] / "orchestrator" / "blueprints"


def _manifests_from_disk() -> list[dict]:
    """What the orchestrator's /blueprints endpoint would return.

    Read here rather than fetched, so these tests describe the blueprints this
    repo actually ships. Tests are not shipped in the API image, so reaching
    across to the orchestrator's files is legitimate here in a way it never is
    in api/ itself — see test_api_does_not_import_orchestrator.
    """
    import yaml
    out = []
    for path in sorted(_BLUEPRINT_DIR.glob("*.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(manifest, dict) and manifest.get("builds"):
            out.append(manifest)
    return out


@pytest.fixture(autouse=True)
def _blueprint_capabilities_are_deterministic():
    """Seed the capability cache instead of letting it call a live orchestrator.

    Without this the API code under test POSTs to ORCHESTRATOR_URL, which on a
    developer's machine may happen to be RUNNING — so the OS-image filter tests
    passed here by reaching a real service, and would have behaved differently on
    a machine without one. A test whose result depends on what else is running is
    not a test.
    """
    blueprint_capabilities.reset()
    blueprint_capabilities.refresh(_manifests_from_disk)
    yield
    blueprint_capabilities.reset()


@pytest.fixture(autouse=True)
def _network_egress_is_deterministic():
    """Seed the build-network answer instead of letting it call a live orchestrator.

    Exactly the same hazard as the capability cache above, and the same fix: left
    alone, api code under test POSTs to ORCHESTRATOR_URL, which on a developer's
    machine may be running — so a test's result would depend on the route table
    of a real tenancy.

    Seeded as fully connected because that is what the rest of the suite is about:
    a test choosing an Ubuntu image is exercising something other than the network
    rule, and should not have to know this exists. The tests that DO exercise the
    rule reset it and supply their own answer.
    """
    network_egress.reset()
    network_egress.refresh(lambda: {
        "known": True, "subnet_name": "TEST-SUBNET", "internet": True,
        "oracle_services": True, "families": ["rhel", "debian", "suse"], "reason": ""})
    yield
    network_egress.reset()


@pytest.fixture(autouse=True)
def _isolate_runtime_settings(monkeypatch):
    """The runtime settings store reads the real Postgres via SessionLocal (like
    the role map). In the test suite that would let production overrides leak into
    tests, so neutralise it by default: every allow-listed read falls back to
    .env/built-in default (what the governance tests expect). Tests that exercise
    the override behaviour re-patch `_load_overrides` themselves."""
    monkeypatch.setattr(settings, "_load_overrides", lambda: {})
