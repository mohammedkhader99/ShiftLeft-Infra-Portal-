"""The test suite must not depend on anything outside the process.

THE LESSON THIS FILE EXISTS FOR: a check that validates what it looks at cannot
see what it doesn't. `conftest.py` pinned auth, Jira, pricing, provisioning and
cloud state to mock and read as though it had covered everything. It had covered
what somebody thought of.

Three times now the thing it missed announced itself as a timing mystery rather
than a failure:

  * DATABASE_URL was unpinned, so every TestClient startup tried localhost:5432.
    It "worked" only because something happened to be listening there and
    rejected the credentials instantly. The day that container stopped, refusal
    went from instant to two seconds per address family.
  * CLOUD_STATE_MODE was unpinned until the day it was first set to `live` in a
    real .env, and four tests failed at once.
  * REGISTRY_MODE did not exist. api/registry.py asks a public container
    registry over the internet with an 8-second timeout, and nothing pinned it
    because there was nothing to pin. `test_asking_creates_nothing` made four
    such asks and took 68.9 seconds of an 18-minute run. The same suite has
    taken 9 minutes, 18 minutes and 1h44m with an identical pass count, decided
    by the network rather than by anything under test.

So this does not hold a list of its own. It reads what the CODE reads and checks
that each one is pinned — the one arrangement that cannot drift, because the
thing being checked is the thing being used.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from api import registry

#: Where production code lives. Tests are excluded deliberately: a test setting
#: a mode to `live` for its own duration is the documented way to exercise a
#: live path against a substituted transport.
SOURCE_ROOTS = ("api", "cli", "common", "db", "orchestrator", "portal")

_READS_A_MODE = re.compile(r'getenv\(\s*"([A-Z][A-Z0-9_]*_MODE)"')


def modes_the_code_reads() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    for package in SOURCE_ROOTS:
        for path in (root / package).rglob("*.py"):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            found |= set(_READS_A_MODE.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_scan_finds_something():
    """If the scan ever returns nothing the test below passes vacuously, which
    is the failure mode of every source-reading check: it would go green on the
    day it stopped looking."""
    found = modes_the_code_reads()
    assert len(found) >= 10, f"the scan found only {sorted(found)}"
    assert "REGISTRY_MODE" in found, "the scan is not reading api/registry.py"


def test_every_mode_the_code_reads_is_pinned_for_the_suite():
    """Not 'the list in conftest is long enough' — that is a second list, and a
    second list is what drifted. This asks the environment the tests actually
    run in."""
    unpinned = sorted(n for n in modes_the_code_reads() if os.getenv(n) != "mock")
    assert not unpinned, (
        f"{unpinned} are read by the code and not pinned in conftest.py. Until "
        f"they are, a developer's .env decides what the suite tests.")


# --- the registry specifically, because it is the one with no safe default ----

def test_the_registry_is_offline_for_the_suite():
    assert registry.mode() != "live"


def test_a_manifest_read_refuses_rather_than_connecting():
    with pytest.raises(registry.RegistryOffline):
        registry._get("https://registry-1.docker.io/v2/library/nginx/manifests/latest", {})


def test_a_search_answers_nothing_rather_than_connecting():
    """`search` swallows failures by design — a registry that will not answer is
    not an error — so offline looks like 'nothing published', which is what an
    unreachable registry has always looked like here."""
    assert registry.search("clickhouse") == []


def test_live_is_still_the_default_outside_the_suite(monkeypatch):
    """PRODUCTION MUST ASK A REAL REGISTRY. The whole module exists so that
    nobody hard-codes what an image is; a mock default would quietly turn every
    deployment into 'no published image'."""
    monkeypatch.delenv("REGISTRY_MODE", raising=False)
    assert registry.mode() == "live"
