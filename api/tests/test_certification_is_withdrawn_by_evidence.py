"""A blueprint that keeps failing must stop being offered (C1).

The standing rule is that once a component is certified it should not fail when
a user selects it. Nothing enforced that. `oci-oke` was certified by hand on
15 August and then failed REQ-2026-0148, 0149, 0150 and 0151 in a row while
staying on the catalogue the whole time; `postgres16` did the same across
0154-0158. A person had to notice, and nobody did.

These tests drive `certification.review` against real request rows. The
interesting cases are all about ATTRIBUTION — deciding which failures are
evidence about a recipe and which are not — because getting that wrong would
pull working blueprints off the catalogue, which is a worse defect than the one
being fixed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import certification
from db.models import Blueprint, ProvisionedResource, Request, RequestComponent
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = Factory()
    seed(s)
    s.add_all([
        Blueprint(technology_code="oci-oke", deployment_target="oci",
                  blueprint_ref="oci/oke", status="certified",
                  resource_kind="oci-oke",
                  certified_by="mohammed.khader@emaratechg.ae"),
        Blueprint(technology_code="nginx", deployment_target="oci",
                  blueprint_ref="oci/service-vm", status="certified",
                  resource_kind="oci-service-vm",
                  certified_by="mohammed.khader@emaratechg.ae"),
    ])
    s.commit()
    s.factory = Factory          # for driving the poll sweep end to end
    yield s
    s.close()


_N = [0]


def add_request(session, status, *, kind=None, detail=None, techs=("oci-oke",),
                built=None, target="oci"):
    """One finished create request. `kind` is what a failure blames; `built` is
    what a success actually produced."""
    _N[0] += 1
    ref = f"REQ-2026-{2000 + _N[0]}"
    if detail is None and kind:
        detail = f"Terraform apply failed for {kind}: terraform apply failed: boom"
    req = Request(reference=ref, requester="a@b.com", request_type="create",
                  status=status, deployment_target=target,
                  environment_name="test", status_detail=detail)
    req.components = [RequestComponent(technology_code=t, size="small") for t in techs]
    session.add(req)
    session.flush()
    for k in (built or []):
        session.add(ProvisionedResource(reference=ref, kind=k, name=f"{ref}-{k}",
                                        details={}, lifecycle_state="active"))
    session.commit()
    return ref


def bp(session, code="oci-oke"):
    return session.get(Blueprint, (code, "oci"))


# --- The acceptance criterion ------------------------------------------------

def test_three_consecutive_failures_withdraw_certification(db):
    """THE case. This is REQ-2026-0148/0150/0151 replayed."""
    refs = [add_request(db, "apply-failed", kind="oci-oke") for _ in range(3)]

    withdrawn = certification.review(db)
    db.commit()

    assert len(withdrawn) == 1, withdrawn
    row = bp(db)
    assert row.status == certification.SUSPENDED
    # The console must be able to say WHY, naming the requests.
    for ref in refs:
        assert ref in row.notes, row.notes
    assert "3 consecutive failures" in row.notes


def test_two_failures_are_not_yet_a_verdict(db):
    """One bad request is noise; two is a coincidence. The threshold exists so a
    cloud having a moment cannot pull a working recipe off the catalogue."""
    add_request(db, "apply-failed", kind="oci-oke")
    add_request(db, "apply-failed", kind="oci-oke")

    assert certification.review(db) == []
    assert bp(db).status == "certified"


def test_a_success_between_failures_resets_the_count(db):
    """Consecutive means consecutive. A recipe that works sometimes is a
    different problem from one that never works, and is not fixed by hiding it."""
    add_request(db, "apply-failed", kind="oci-oke")
    add_request(db, "apply-failed", kind="oci-oke")
    add_request(db, "provisioned", built=["oci-oke"])
    add_request(db, "apply-failed", kind="oci-oke")

    assert certification.review(db) == []
    assert bp(db).status == "certified"


# --- Attribution: which failures are evidence about a recipe ----------------

def test_a_timeout_blames_nobody(db):
    """REQ-2026-0149 failed with 'unreachable: timed out'. That was the
    orchestrator giving up, not the blueprint — the cluster it was building went
    on to exist. Counting it would suspend a recipe for the platform's fault."""
    for _ in range(4):
        add_request(db, "apply-failed", detail="unreachable: timed out")

    assert certification.review(db) == []
    assert bp(db).status == "certified"


def test_failures_are_attributed_to_the_component_that_broke(db):
    """A two-component stack that fails on postgres must not suspend nginx.

    Blaming every component of a failed request would be the easy implementation
    and would take working recipes down with the broken one.
    """
    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-postgres",
                    techs=("nginx", "postgres16"))

    certification.review(db)
    db.commit()
    assert bp(db, "nginx").status == "certified", (
        "nginx was suspended for a failure in the other half of the stack")


def test_only_the_named_cloud_is_affected(db):
    """Certification is per (technology, cloud). A recipe failing on OCI says
    nothing about the same technology on another cloud."""
    db.add(Blueprint(technology_code="oci-oke", deployment_target="azure",
                     blueprint_ref="azure/aks", status="certified",
                     resource_kind="oci-oke"))
    db.commit()
    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-oke", target="oci")

    certification.review(db)
    db.commit()
    assert db.get(Blueprint, ("oci-oke", "oci")).status == certification.SUSPENDED
    assert db.get(Blueprint, ("oci-oke", "azure")).status == "certified"


# --- The safe direction only -------------------------------------------------

def test_a_later_success_does_not_quietly_re_certify(db):
    """Coming back requires a human, or a proof build.

    Auto-recertifying on one success is exactly the "it worked once" reasoning
    that certified oci-oke before it failed four times.
    """
    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-oke")
    certification.review(db)
    db.commit()
    assert bp(db).status == certification.SUSPENDED

    add_request(db, "provisioned", built=["oci-oke"])
    certification.review(db)
    db.commit()
    assert bp(db).status == certification.SUSPENDED, (
        "one success un-suspended a blueprint the evidence had condemned")


def test_an_uncertified_blueprint_is_left_alone(db):
    """Nothing to withdraw, and no reason to overwrite someone's notes."""
    row = bp(db)
    row.status = "draft"
    row.notes = "waiting on the network team"
    db.commit()
    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-oke")

    assert certification.review(db) == []
    assert bp(db).notes == "waiting on the network team"


def test_a_blueprint_with_no_track_record_is_not_condemned(db):
    """Certified an hour ago and never exercised is not evidence of anything."""
    assert certification.review(db) == []
    assert bp(db).status == "certified"


# --- The attribution parser --------------------------------------------------

@pytest.mark.parametrize("detail,expected", [
    ("Terraform apply failed for oci-oke: terraform apply failed: x", "oci-oke"),
    ("Terraform apply failed for oci-postgres: 400-InvalidParameter", "oci-postgres"),
    ("unreachable: timed out", None),
    ("orchestrator 502", None),
    ("", None),
    (None, None),
])
def test_blame_is_read_from_the_message_not_guessed(detail, expected):
    assert certification.blamed_kind(detail) == expected


def test_the_threshold_is_configurable_and_floored(monkeypatch):
    monkeypatch.setenv("CERTIFICATION_FAILURE_THRESHOLD", "5")
    assert certification.failure_threshold() == 5
    # Below two, a single unlucky request would decertify a working recipe.
    monkeypatch.setenv("CERTIFICATION_FAILURE_THRESHOLD", "1")
    assert certification.failure_threshold() == 2
    monkeypatch.setenv("CERTIFICATION_FAILURE_THRESHOLD", "nonsense")
    assert certification.failure_threshold() == 3


# --- Wired into the sweep, not just importable -------------------------------

def test_the_poller_sweep_actually_performs_the_review(db, monkeypatch):
    """The integration point.

    Every test above would still pass with the sweep never wired into
    _poll_once — the same "tests the part, not the path" mistake that let a
    correct PostgreSQL resolver ship without ever running. So drive the sweep.
    """
    from api import main

    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-oke")

    monkeypatch.setattr(main, "SessionLocal", db.factory)
    monkeypatch.setattr(main, "_advance_request", lambda s, r: None)
    monkeypatch.setattr(main, "_escalate_sla", lambda s, r: None)
    for other in ("_sweep_ttls", "_sweep_project_expiry", "_sweep_reports"):
        if hasattr(main, other):
            monkeypatch.setattr(main, other, lambda *a, **k: None)

    main._poll_once()

    row = db.get(Blueprint, ("oci-oke", "oci"))
    db.refresh(row)
    assert row.status == certification.SUSPENDED, (
        "the sweep never reviewed certification — review() is correct but unreachable")
    assert "consecutive failures" in (row.notes or "")


def test_the_withdrawal_is_audited(db, monkeypatch):
    """Everything privileged is audited. Taking a component off the catalogue
    changes what users may request, so it belongs in the chain."""
    from api import main
    from db.models import AuditLog
    from sqlalchemy import select as _select

    for _ in range(3):
        add_request(db, "apply-failed", kind="oci-oke")

    monkeypatch.setattr(main, "SessionLocal", db.factory)
    monkeypatch.setattr(main, "_advance_request", lambda s, r: None)
    monkeypatch.setattr(main, "_escalate_sla", lambda s, r: None)
    for other in ("_sweep_ttls", "_sweep_project_expiry", "_sweep_reports"):
        if hasattr(main, other):
            monkeypatch.setattr(main, other, lambda *a, **k: None)

    main._poll_once()

    events = [a.event for a in db.scalars(_select(AuditLog)).all()]
    assert "blueprint.suspended" in events, events


def test_a_build_that_was_later_decommissioned_still_counts_as_proof(db):
    """Found on real data, not imagined.

    REQ-2026-0153 built an OKE cluster in 15 minutes and was later torn down by
    REQ-2026-0161, which left its status as 'decommissioned'. Counting only
    'provisioned' made that success invisible, so oci-oke read as three straight
    failures and would have been suspended — a recipe that demonstrably works,
    pulled off the catalogue because someone tidied up after it.

    Tearing an environment down is normal lifecycle, not a verdict on the recipe.
    """
    add_request(db, "apply-failed", kind="oci-oke")
    add_request(db, "apply-failed", kind="oci-oke")
    add_request(db, "decommissioned", built=["oci-oke"])   # built fine, then removed
    add_request(db, "apply-failed", kind="oci-oke")

    assert certification.review(db) == [], (
        "a successful build was ignored because it had since been decommissioned")
    assert bp(db).status == "certified"
