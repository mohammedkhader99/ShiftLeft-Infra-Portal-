"""Every setting the orchestrator reads must be passed to it by compose.

A container sees only the environment variables its compose service declares.
A setting read in orchestrator code but absent from docker-compose.yml can be
set correctly in .env, survive a rebuild, survive a recreate — and still be
empty inside the container, with nothing anywhere saying why.

This has now happened three times in this codebase:

  * OCI_PSQL_* and VAULT_* and CONFIG_*, found only because someone asked
    "where do we set OCI_PSQL_ENABLED?"
  * OCI_OKE_BASTION_CIDR, after an afternoon spent choosing the right value for
    a variable that could never arrive
  * OCI_KAFKA_SOURCE_URL, immediately after building the gate that depends on it

Each time the code was right, the .env was right, and the wiring between them was
missing. Reviewing a diff does not catch it, because the absent line is in a
different file from the change. So the build checks instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
ORCH_DIR = ROOT / "orchestrator"

_READ = re.compile(r'os\.getenv\(\s*"([A-Z][A-Z0-9_]*)"')

# Read by orchestrator code but deliberately NOT supplied per-service. Empty
# today, and that is the healthy state: every setting the orchestrator reads is
# one a deployment can actually set.
NOT_FROM_COMPOSE: set[str] = set()


def _settings_read_by_the_orchestrator() -> set[str]:
    found: set[str] = set()
    for path in sorted(ORCH_DIR.rglob("*.py")):
        if path.name.startswith("test_") or "tests" in path.parts:
            continue
        found |= set(_READ.findall(path.read_text(encoding="utf-8")))
    return found


def _orchestrator_service_block() -> str:
    """The orchestrator service's section of docker-compose.yml."""
    text = COMPOSE.read_text(encoding="utf-8")
    start = text.index("\n  orchestrator:")
    rest = text[start + 1:]
    # The next top-level service starts at exactly two spaces of indent.
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", rest)
    return rest[: nxt.start()] if nxt else rest


def test_every_setting_the_orchestrator_reads_is_passed_to_it():
    block = _orchestrator_service_block()
    declared = set(re.findall(r"^\s+([A-Z][A-Z0-9_]*):", block, re.M))
    missing = sorted(_settings_read_by_the_orchestrator() - declared - NOT_FROM_COMPOSE)
    assert not missing, (
        "The orchestrator reads these settings, but docker-compose.yml does not "
        "pass them to the container, so they are always empty inside it no matter "
        f"what .env says:\n  " + "\n  ".join(missing)
        + "\n\nAdd each to the orchestrator service's `environment:` block as "
          "NAME: ${NAME:-}."
    )


def test_the_exclusions_are_still_real():
    """A stale exclusion hides the next instance of this bug."""
    read = _settings_read_by_the_orchestrator()
    stale = sorted(k for k in NOT_FROM_COMPOSE if k not in read)
    assert not stale, f"NOT_FROM_COMPOSE lists settings no longer read: {stale}"
