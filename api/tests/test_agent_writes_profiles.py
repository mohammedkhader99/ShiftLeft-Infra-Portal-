"""The agent teaches the proven module a new technology, and proves it (C6).

REQ-2026-0175 asked for keycloak. The agent drafted a Terraform module whose own
opening line said "builds nothing yet", the plan created nothing, and the proof
passed. The lesson was not "generate better Terraform" — it was that generating
Terraform was the wrong move entirely.

`oci/service-vm` already builds machines correctly: it resolves the image from
OCI at run time filtered by shape, keeps the instance on a private subnet,
enables Run Command so the machine can be interrogated, and makes it report what
it became. Every one of those behaviours was bought with a failed request. A
freshly generated module would inherit none of them, and would collide on a
resource kind that already has a module.

keycloak is software on a machine. What it needs is a package, a systemd unit and
a port — what that blueprint's manifest calls "a data change".
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild, certification
from api.proof import ProofOutcome
from db.models import Blueprint
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
    "builds": ["nginx", "redis7", "java21", "python312", "nodejs20"],
    "version": "1.0.0",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
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


class Store:
    """A generated store that records the ORDER things happened in."""

    def __init__(self, wires_up=True):
        self.files: dict[str, str] = {}
        self.events: list[str] = []
        self._wires_up = wires_up

    def publish(self, files):
        self.files.update(files)
        self.events.append("publish")
        return list(files)

    def withdraw(self, files):
        for f in files:
            self.files.pop(f, None)
        self.events.append("withdraw")
        return list(files)

    def shipped(self, candidate):
        """The orchestrator re-read: it only builds what a profile taught it."""
        if candidate in SERVICE_VM["builds"]:
            return SERVICE_VM
        if self._wires_up and any(candidate in f for f in self.files):
            return {**SERVICE_VM, "builds": SERVICE_VM["builds"] + [candidate]}
        return None


def certifier(db, code):
    def certify(manifest, reference):
        return certification.certify_from_proof(
            db, code, manifest["target"], manifest["ref"],
            manifest["resource_kind"], reference,
            version=manifest.get("version", ""))
    return certify


def proof(status="passed", detail=None):
    detail = detail or {"passed": "built, verified, destroyed",
                        "failed": "no package keycloak in the OL9 repositories"}[status]
    return lambda session, manifest: ProofOutcome("PROOF-KEYCLOAK-X", status, detail)


def run(db, store, status="passed", candidate="keycloak"):
    return autobuild.ensure(
        candidate, db, target="oci", shipped=store.shipped,
        run_proof=proof(status), publish=store.publish,
        certify=certifier(db, candidate), withdraw=store.withdraw)


# --- the happy path ----------------------------------------------------------

def test_keycloak_gets_a_profile_not_a_terraform_module(db):
    store = Store()
    result = run(db, store)

    assert result.status == "published", result.detail
    written = list(store.files)
    assert written, "nothing was written"
    assert all(p.endswith(".json") for p in written), (
        f"it wrote Terraform for software that runs on a machine: {written}")
    assert not any(p.endswith(".tf") for p in written)


def test_the_profile_names_a_package_a_unit_and_a_way_to_check_it(db):
    store = Store()
    run(db, store)
    profile = json.loads(next(iter(store.files.values())))

    assert profile["rhel"]["packages"], "the machine is told to install nothing"
    assert profile["version_command"], (
        "no way to ask the machine what it actually received — the defect that "
        "shipped Redis 6.2 under a catalogue entry promising Redis 7")

    # A SERVICE IS NO LONGER REQUIRED, and the reason matters.
    #
    # This asserted `services` non-empty, guarding "installed but never
    # started". The guess met it by declaring `services: [code]` for every
    # technology — asserting that anything unknown is a daemon named after
    # itself. True for nginx; false for .NET, Java, Python and every runtime,
    # and REQ-2026-0212 failed on `dotnet8 did not start` after the machine had
    # installed .NET correctly.
    #
    # An invented name is not a weaker guard than none, it is a false one. What
    # remains asserted is that the profile carries AT LEAST ONE way to prove the
    # machine became something — which a runtime satisfies with its version
    # command, and a daemon with its service or its ports.
    assert (profile["version_command"] or profile["rhel"]["services"]
            or profile.get("ports")), (
        "nothing in this profile could ever show the software works")
    assert profile["builds_on"] == "oci/service-vm"


def test_it_certifies_against_the_blueprint_it_extended(db):
    """NOT a new resource kind. The machine really is an oci-service-vm, and
    saying otherwise would make the catalogue claim a module that does not
    exist."""
    store = Store()
    run(db, store)
    db.commit()

    row = db.get(Blueprint, ("keycloak", "oci"))
    assert row is not None and row.status == "certified"
    assert row.blueprint_ref == "oci/service-vm"
    assert row.resource_kind == "oci-service-vm"
    assert row.certified_by == certification.CERTIFIED_BY_RUNNER


# --- the ordering, which is the subtle part ----------------------------------

def test_the_profile_is_published_BEFORE_the_proof_runs(db):
    """Everywhere else a draft is proved before it is published. A profile must
    be the other way round: the orchestrator reads it from the store when it
    renders first-boot configuration, so proving first would boot a machine with
    nothing to install — which succeeds, and proves nothing.
    """
    store = Store()
    order = []

    def watching_proof(session, manifest):
        order.append("proof")
        return ProofOutcome("PROOF-X", "passed", "ok")

    def watching_publish(files):
        order.append("publish")
        return store.publish(files)

    autobuild.ensure("keycloak", db, target="oci", shipped=store.shipped,
                     run_proof=watching_proof, publish=watching_publish,
                     certify=certifier(db, "keycloak"), withdraw=store.withdraw)

    assert order == ["publish", "proof"], f"wrong order: {order}"


# --- failure must leave nothing behind ---------------------------------------

def test_a_failed_proof_withdraws_the_profile(db):
    """An unproven profile left in the store makes an uninstallable technology
    look installable to every later request."""
    store = Store()
    result = run(db, store, status="failed")
    db.commit()

    assert result.status == "failed"
    assert store.files == {}, "an unproven profile was left in the store"
    # One publish/withdraw per INSTALL METHOD tried (2026-08-23). keycloak now
    # climbs a ladder — package, then archive — so a refuted rung is withdrawn
    # before the next is published. What must hold is not the count but that
    # every rung cleans up after itself: the store ends empty, and no publish is
    # ever left without its withdraw.
    assert store.events, "nothing was attempted"
    assert store.events.count("publish") == store.events.count("withdraw"), (
        f"a published profile was left behind: {store.events}")
    assert store.events[0] == "publish" and store.events[-1] == "withdraw"
    assert db.get(Blueprint, ("keycloak", "oci")) is None
    assert "no package keycloak" in result.detail, "it hid why"


def test_a_profile_that_does_not_wire_up_is_withdrawn_and_said_so(db):
    """Two gates must accept a profile — the blueprint's `builds` list and
    configure.py templates — and missing either is silent. Asking the
    orchestrator again is the only honest way to know it took."""
    store = Store(wires_up=False)
    result = run(db, store)

    assert result.status == "failed"
    assert store.files == {}
    assert "still reports no blueprint that builds it" in result.detail
    assert db.get(Blueprint, ("keycloak", "oci")) is None


def test_a_blocked_draft_never_boots_a_machine_OR_reaches_the_store(db):
    """The linter is cheap; a machine is not.

    nginx already has a profile somebody checked against a real image. A
    generated one must be refused BEFORE anything is published or booted — and
    the refusal must come from the linter, not from a later gate. Passing
    shipped_codes is what makes that check real: without it this test passed for
    the wrong reason, caught by the wire-up check after publishing.
    """
    store = Store()
    proved = []

    def never(session, manifest):
        proved.append(1)
        return ProofOutcome("PROOF-X", "passed", "ok")

    result = autobuild.ensure(
        "nginx", db, target="oci",
        shipped=lambda c: None,        # pretend no manifest builds it
        run_proof=never, publish=store.publish,
        certify=certifier(db, "nginx"), withdraw=store.withdraw,
        shipped_codes=frozenset({"nginx"}))

    assert proved == [], "it booted a machine for a draft the linter refused"
    assert store.events == [], "a refused draft was written to the store anyway"
    assert store.files == {}
    assert "already has a reviewed profile" in result.detail
    assert result.attempts and result.attempts[0].outcome == "blocked"


# --- the other route is untouched --------------------------------------------

def test_a_cloud_managed_service_still_goes_down_the_terraform_path(db):
    """A bucket is not a package. `oci-*`, `aws-*` and their like are cloud
    services, and a profile could never install one."""
    from api import ai_blueprint
    kind, _ = ai_blueprint.classify("oci-somethingnew", db)
    assert kind == "new-service"


def test_an_existing_recipe_is_still_just_proved(db):
    """The cheapest case must not regress: nginx needs no profile and no
    Terraform, only a proof."""
    store = Store()
    result = autobuild.ensure("nginx", db, target="oci",
                              shipped=lambda c: SERVICE_VM,
                              run_proof=proof("passed"), publish=store.publish,
                              certify=certifier(db, "nginx"), withdraw=store.withdraw)
    assert result.status == "published"
    assert store.files == {}, "it wrote something for a component that needed nothing"
