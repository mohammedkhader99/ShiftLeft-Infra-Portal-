"""The agent certifies its own work, on evidence (2026-08-21).

The reviewer's requirement, stated three times and mandatory: no human in the
certification path. If a component has no recipe the agent writes one; either
way it proves it, certifies it, and the request proceeds.

What makes that more than a rubber stamp is that certification cannot be reached
without a passing proof — the thing was built, verified healthy, priced under the
cap and destroyed again. A person clicking Certify proved nothing at all, which
is how `oci-oke` came to fail four consecutive real requests while on offer.

The case that matters most here is the middle one. `nginx` needs NO new
Terraform: `oci/service-vm` already builds it, and four of its five technologies
are certified. Reaching for a model there would write a second recipe for the
same resource kind — the collision the registry refuses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild, certification
from api.proof import ProofOutcome
from db.models import Blueprint
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm",
    "target": "oci",
    "resource_kind": "oci-service-vm",
    "builds": ["nginx", "redis7", "java21", "python312", "nodejs20"],
    "version": "1.0.0",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
        monkeypatch.delenv(key, raising=False)
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


def proof(status="passed"):
    detail = {"passed": "built, verified, destroyed",
              "failed": "apply failed: shape not published in this region"}[status]
    return lambda session, manifest: ProofOutcome(
        "PROOF-NGINX-20260821T150000", status, detail)


def certifier(db):
    """The real certification, so these tests exercise what actually happens."""
    def certify(manifest, proof_reference):
        return certification.certify_from_proof(
            db, "nginx", manifest["target"], manifest["ref"],
            manifest["resource_kind"], proof_reference,
            version=manifest.get("version", ""))
    return certify


def nginx(db):
    return db.get(Blueprint, ("nginx", "oci"))


# --- The nginx case: a recipe exists, nobody certified it --------------------

def test_an_existing_recipe_is_proved_and_certified_without_writing_anything(db):
    """REQ-2026-0165 and 0166, both held for this. oci/service-vm already builds
    nginx and four of its five technologies are certified."""
    written = []
    result = autobuild.ensure(
        "nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
        run_proof=proof("passed"), publish=written.append, certify=certifier(db))
    db.commit()

    assert result.status == "published"
    assert written == [], "it wrote a second recipe for a resource kind that has one"
    row = nginx(db)
    assert row is not None and row.status == "certified"
    assert row.blueprint_ref == "oci/service-vm"
    assert row.resource_kind == "oci-service-vm"


def test_the_certification_records_the_runner_not_a_person(db):
    """An audit trail that attributed this to a human would be a lie, and which
    of these a person approved is the first question anybody asks later."""
    autobuild.ensure("nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
                     run_proof=proof("passed"), publish=lambda f: None,
                     certify=certifier(db))
    db.commit()

    row = nginx(db)
    assert row.certified_by == certification.CERTIFIED_BY_RUNNER
    assert "PROOF-NGINX-20260821T150000" in row.notes
    assert "built, verified healthy and destroyed" in row.notes


def test_a_recipe_that_fails_its_proof_is_NOT_certified(db):
    """The whole difference between this and a rubber stamp. `oci-oke` was
    certified by hand and then failed four consecutive real requests."""
    result = autobuild.ensure(
        "nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
        run_proof=proof("failed"), publish=lambda f: None, certify=certifier(db))
    db.commit()

    assert result.status == "failed"
    assert nginx(db) is None, "it certified something that failed to build"
    assert "has NOT been certified" in result.detail
    assert "shape not published" in result.detail


# --- No recipe at all: the full loop -----------------------------------------

def test_a_component_with_no_recipe_is_written_a_profile_not_a_module(db):
    """The requirement's other half — and it changed on 2026-08-22 (C6).

    This test used to assert that a component with no recipe got a .tf and a
    .yaml. REQ-2026-0175 showed why that was wrong: the Terraform the agent
    drafts for software-on-a-machine is a skeleton that builds nothing, and it
    would collide with `oci/service-vm`, which already builds machines correctly.

    cassandra5 is software on a machine. It gets a PROFILE — a package, a unit
    and a way to ask the machine what it actually installed — and the proof boots
    a real machine to find out whether that package exists at all.
    """
    written = []
    result = autobuild.ensure(
        "cassandra5", db, target="oci",
        # The orchestrator re-read: once the profile is in the store, the
        # blueprint that builds machines reports that it now builds this too.
        shipped=lambda c: (SERVICE_VM if written else None),
        run_proof=proof("passed"), publish=written.append, certify=certifier(db),
        withdraw=lambda f: None)

    assert result.status == "published", result.detail
    assert written, "nothing was written for a component that had no recipe"
    files = written[0]
    assert any(p.endswith(".json") for p in files), f"expected a profile: {files}"
    assert not any(p.endswith(".tf") for p in files), (
        "it wrote a second Terraform module for a resource kind that has one")


def test_drafting_is_not_reached_when_a_recipe_already_exists(db):
    """Writing a second blueprint for one resource kind is the collision the
    registry refuses — the expensive way to be wrong."""
    drafted = []
    from api import ai_blueprint

    real_draft = ai_blueprint.draft

    def spy(candidate, session, target="oci"):
        drafted.append(candidate)
        return real_draft(candidate, session, target=target)

    import api.autobuild as ab
    original = ab.ai_blueprint.draft
    ab.ai_blueprint.draft = spy
    try:
        autobuild.ensure("nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
                         run_proof=proof("passed"), publish=lambda f: None,
                         certify=certifier(db))
    finally:
        ab.ai_blueprint.draft = original

    assert drafted == [], "the drafter ran even though a recipe already existed"


# --- Still off unless asked --------------------------------------------------

def test_it_does_nothing_when_autobuild_is_disabled(db, monkeypatch):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "false")
    result = autobuild.ensure("nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
                              run_proof=proof("passed"), publish=lambda f: None,
                              certify=certifier(db))
    assert result.status == "refused"
    assert nginx(db) is None


# --- Certification remains revocable -----------------------------------------

def test_an_agent_certified_blueprint_is_still_withdrawn_when_it_fails(db):
    """C1 does not care who certified it. Self-certification must not create a
    blueprint that cannot be taken away."""
    autobuild.ensure("nginx", db, target="oci", shipped=lambda c: SERVICE_VM,
                     run_proof=proof("passed"), publish=lambda f: None,
                     certify=certifier(db))
    db.commit()
    assert nginx(db).status == "certified"

    from db.models import Request, RequestComponent
    for i in range(3):
        ref = f"REQ-2026-90{i}"
        r = Request(reference=ref, requester="a@b.com", request_type="create",
                    status="apply-failed", deployment_target="oci",
                    environment_name="test",
                    status_detail="Terraform apply failed for oci-service-vm: boom")
        r.components = [RequestComponent(technology_code="nginx", size="small")]
        db.add(r)
    db.commit()

    certification.review(db)
    db.commit()
    assert nginx(db).status == certification.SUSPENDED


# --- The two services must agree on what a proof looks like ------------------

def test_a_contract_skew_is_refused_with_a_sentence_that_names_it(monkeypatch):
    """FOUND IN PRODUCTION, 2026-08-21. common/proof_rules.py is shared by both
    images. The API was rebuilt with a new reference format and the orchestrator
    was not, so every proof came back:

        'PROOF-NGINX-...' is not a proof reference. A proof may only act under a
        reference this runner minted.

    Correct behaviour, and an alarming thing to read: it describes an impostor,
    not a stale container. A skew should be named as a skew.
    """
    import api.main as main

    monkeypatch.setattr(main, "_orchestrator_posture",
                        lambda: {"proof_reference_pattern": "^SOMETHING-ELSE$"})
    skew = main._proof_contract_skew()
    assert "rebuild BOTH images" in skew
    assert "proof_rules" in skew


def test_agreement_is_not_reported_as_a_problem(monkeypatch):
    import api.main as main
    from common import proof_rules

    monkeypatch.setattr(main, "_orchestrator_posture",
                        lambda: {"proof_reference_pattern": proof_rules._REFERENCE.pattern})
    assert main._proof_contract_skew() == ""


def test_an_unreachable_orchestrator_is_not_mistaken_for_a_skew(monkeypatch):
    """Different problem, reported elsewhere. Calling it a skew would send
    somebody rebuilding images to fix a network fault."""
    import api.main as main

    monkeypatch.setattr(main, "_orchestrator_posture", lambda: None)
    assert main._proof_contract_skew() == ""
