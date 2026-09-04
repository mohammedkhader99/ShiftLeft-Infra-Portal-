"""The sweep un-suspended a blueprint on the proof the suspension had rejected.

Audit rows 12764 and 12766, 2026-09-03/04:

    03 Sep 21:50:32  blueprint.withdrawn   mohammed.khader@...
        "Certified by a break-glass proof ... whose recipe was the command-line
         client (package mysql, services: [], no port) ... Withdrawn by the
         operator who ran it."
    04 Sep 04:53:49  blueprint.recertified  certification
        {"technology": "mysql", "was": "suspended",
         "proved_at": "2026-09-03T17:48:00"}

Thirty seconds before the second row, a re-proof through the request path had
published the SAME client recipe into the store as a draft, to be judged again
by a gate that now had the evidence. `restore()` saw: a suspended blueprint, a
passing proof from the day before (the very proof the withdrawal had rejected),
and an orchestrator that could suddenly "build mysql" because a draft under
proof had appeared in the bind-mounted store. It certified the client. The
catalogue offered MySQL with a command-line client behind it for nineteen
minutes, until the gate refused the draft and `take_it_back` emptied the store.

`restore()`'s own comment says a passing proof is not a recipe. What it could
not tell apart:

  * a recipe being TESTED from a recipe that had been PROVED;
  * evidence that is NEW from evidence the suspension already answered;
  * a suspension because the evidence turned against a recipe (three failed
    requests) from a WITHDRAWAL because the recipe itself is gone -- the second
    has nothing for the sweep to bring back; only a new certified recipe can.

Each of those is one rule below, and each reproduces on this file's clean code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import certification, recipe_memory
from db.models import Blueprint, CertificationProof
from db.seed import seed
from db.session import Base

NOW = datetime.now(timezone.utc)
CERTIFIED_AT = NOW - timedelta(hours=2)          # when the current certification was granted
CLIENT_RECIPE = {"code": "mysql", "rhel": {"packages": ["mysql"], "services": []}}


@pytest.fixture(autouse=True)
def _proofs_on(monkeypatch):
    for key in ("CERTIFICATION_PROOF_ENABLED", "CERTIFICATION_VALIDITY_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.add(Blueprint(technology_code="mysql", deployment_target="oci",
                    blueprint_ref="oci/service-vm", resource_kind="oci-service-vm",
                    status="certified", certified_by=certification.CERTIFIED_BY_RUNNER,
                    certified_at=CERTIFIED_AT))
    s.commit()
    yield s
    s.close()


def bp(db):
    return db.get(Blueprint, ("mysql", "oci"))


def proof(db, reference, finished_at, status="passed"):
    db.add(CertificationProof(
        technology_code="mysql", deployment_target="oci", resource_kind="oci-service-vm",
        reference=reference, status=status,
        started_at=(finished_at or NOW) - timedelta(minutes=15), finished_at=finished_at))
    db.commit()


def suspend(db):
    row = bp(db)
    row.status = certification.SUSPENDED
    row.notes = "Certification withdrawn automatically after 3 consecutive failures"
    db.commit()


# --- old evidence -----------------------------------------------------------------

def test_the_proof_that_granted_a_certification_cannot_grant_it_again(db):
    """12766 exactly. The passing proof finished eleven milliseconds before the
    certification it earned was written; suspended afterwards; the store holds
    something that builds. That proof has been answered once already."""
    proof(db, "PROOF-MYSQL-83", CERTIFIED_AT - timedelta(seconds=1))
    suspend(db)

    assert certification.restore(db, builds=frozenset({"mysql"})) == []
    assert bp(db).status == certification.SUSPENDED, (
        "restored on the proof the current certification already rested on")


def test_new_evidence_restores_once(db):
    """A proof after the suspension is what C1 promised, and it is spent by
    being used: the restoration stamps `certified_at`, so the same proof cannot
    bring the blueprint back a second time."""
    proof(db, "PROOF-MYSQL-83", CERTIFIED_AT - timedelta(seconds=1))
    suspend(db)
    proof(db, "PROOF-MYSQL-85", NOW - timedelta(minutes=30))

    back = certification.restore(db, builds=frozenset({"mysql"}))
    db.commit()

    assert [b["technology"] for b in back] == ["mysql"]
    row = bp(db)
    assert row.status == "certified"
    assert row.certified_at is not None
    granted = row.certified_at if row.certified_at.tzinfo else row.certified_at.replace(tzinfo=timezone.utc)
    assert granted > NOW - timedelta(minutes=30), "certified_at was not stamped by the restoration"

    suspend(db)
    assert certification.restore(db, builds=frozenset({"mysql"})) == [], (
        "the same proof restored the blueprint twice")


# --- a withdrawn recipe is not the sweep's to bring back ---------------------------

def test_a_withdrawn_recipe_is_not_the_sweeps_to_bring_back(db):
    """`take_it_back` and the operator's own withdrawal say the RECIPE is gone.
    A draft appearing in the store later -- under proof, unjudged -- makes the
    orchestrator answer "I build mysql" again, and that must not be enough.
    Only a certified recipe ends a withdrawal, and `certify_from_proof` is the
    one thing that can write that."""
    proof(db, "PROOF-MYSQL-83", CERTIFIED_AT - timedelta(seconds=1))
    assert certification.withdraw_for_missing_recipe(
        db, "mysql", "oci", "the recipe was the command-line client")
    db.commit()
    assert bp(db).status == certification.WITHDRAWN

    # A brand-new passing proof, a store that builds it: still no.
    proof(db, "PROOF-MYSQL-84", NOW - timedelta(minutes=30))
    assert certification.restore(db, builds=frozenset({"mysql"})) == []
    assert bp(db).status == certification.WITHDRAWN


def test_withdrawing_the_recipe_of_a_suspended_blueprint_marks_it_withdrawn(db):
    """A suspension (the evidence turned against it) followed by a withdrawal
    (the recipe is gone) ends in the stronger state, not the first one."""
    suspend(db)

    assert certification.withdraw_for_missing_recipe(db, "mysql", "oci", "gone")
    assert bp(db).status == certification.WITHDRAWN
    assert not certification.withdraw_for_missing_recipe(db, "mysql", "oci", "again")


def test_a_withdrawn_blueprint_is_certified_again_only_by_a_proof_of_a_recipe(db):
    """The way back exists; it is the ladder's, not the sweep's."""
    certification.withdraw_for_missing_recipe(db, "mysql", "oci", "gone")
    db.commit()

    certification.certify_from_proof(db, "mysql", "oci", "oci/service-vm",
                                     "oci-service-vm", "PROOF-MYSQL-86")
    db.commit()

    assert bp(db).status == "certified"


# --- a proof in flight decides; the sweep waits ------------------------------------

def test_a_proof_in_flight_holds_the_sweep(db):
    """While a machine is being built to answer the question, the answer is
    that machine's. The sweep racing it is how a draft under proof was offered
    to requesters."""
    suspend(db)
    proof(db, "PROOF-MYSQL-85", NOW - timedelta(minutes=30))
    proof(db, "PROOF-MYSQL-86", None, status="running")

    assert certification.restore(db, builds=frozenset({"mysql"})) == []
    assert bp(db).status == certification.SUSPENDED

    running = db.scalar(
        __import__("sqlalchemy").select(CertificationProof)
        .where(CertificationProof.reference == "PROOF-MYSQL-86"))
    running.status, running.finished_at = "failed", NOW
    db.commit()

    assert [b["technology"] for b in certification.restore(db, builds=frozenset({"mysql"}))] == ["mysql"]


# --- a refuted proof is not evidence ----------------------------------------------

def test_a_refuted_proof_is_not_evidence(db):
    """The gate's refusal is remembered against the proof's reference. A proof
    a machine passed and the gate then refused proves the recipe installs
    files; it does not prove anything the catalogue may offer."""
    suspend(db)
    proof(db, "PROOF-MYSQL-84", NOW - timedelta(minutes=30))
    recipe_memory.remember(db, "mysql", "oci", CLIENT_RECIPE, "PROOF-MYSQL-84",
                           "mysql installs but runs nothing")
    db.commit()

    assert certification.restore(db, builds=frozenset({"mysql"})) == []
    assert bp(db).status == certification.SUSPENDED
    assert certification.last_passing_proof(db, "mysql", "oci") is None, (
        "a refuted proof still counts as the last passing proof")


# --- a proof older than the failures that suspended it -------------------------

def _failed_request(db, reference, at):
    from db.models import Request, RequestComponent
    req = Request(reference=reference, requester="a@b.com", request_type="create",
                  status="apply-failed", deployment_target="oci",
                  environment_name="test",
                  status_detail="Terraform apply failed for oci-service-vm: boom",
                  created_at=at, updated_at=at)
    req.components = [RequestComponent(technology_code="mysql", size="small")]
    db.add(req)
    db.commit()


def test_a_proof_older_than_the_failures_that_suspended_it_does_not_count(db):
    """Certified by hand long ago, proved once, then three requests failed and
    `review()` suspended it. The proof predates every failure: it is the
    evidence the failures contradicted, not the evidence that answers them."""
    proof(db, "PROOF-MYSQL-80", CERTIFIED_AT + timedelta(minutes=10))
    for i in range(3):
        _failed_request(db, f"REQ-2026-30{i}", CERTIFIED_AT + timedelta(hours=1, minutes=i))
    suspend(db)

    assert certification.restore(db, builds=frozenset({"mysql"})) == []
    assert bp(db).status == certification.SUSPENDED

    proof(db, "PROOF-MYSQL-87", NOW - timedelta(minutes=5))
    assert [b["technology"] for b in certification.restore(db, builds=frozenset({"mysql"}))] == ["mysql"]
