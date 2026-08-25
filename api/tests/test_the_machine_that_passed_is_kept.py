"""G1: a proof build stops throwing away the machine that worked.

Every proof already boots a real VM, installs the software, proves it healthy
and destroys it. The instance is the only artefact in that process worth
keeping, and until 2026-08-25 it was the one thing guaranteed to be discarded.

The whole increment rests on one property, and most of this file defends it:

    A CAPTURE CAN NEVER CHANGE A PROOF'S VERDICT.

It runs after `healthy` is computed, nothing downstream reads its result, and
every failure path — refused, unreachable, exploded, unparseable — lands in a
row and stops there. Capture is an optimisation on a path that already works.
The day a failed capture blocks a provisioning that would otherwise have
succeeded is the day this feature became a liability.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import proof
from db.session import Base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


class _Blueprint:
    technology_code = "dotnet8"
    deployment_target = "oci"
    resource_kind = "oci-service-vm"
    os_family = "rhel"


APPLIED = "oci-service-vm: Plan: 1 to add, 0 to change, 0 to destroy."


def _post(fail: set[str] | None = None):
    """A cooperative orchestrator. Records what it was asked to do."""
    fail = fail or set()
    calls: list[str] = []

    def post(path, payload):
        calls.append(path)
        if path in fail:
            return False, f"{path} refused"
        return True, APPLIED
    post.calls = calls
    return post


def _run(session, *, capture=None, healthy=True, post=None):
    return proof.run_proof(
        session, _Blueprint(),
        post=post or _post(),
        price=lambda components: 10.0,
        verify=lambda ref, payload: (healthy, "boot report clean"),
        capture=capture)


def _images(session):
    from db.models import GoldenImage
    return session.query(GoldenImage).all()


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")
    monkeypatch.setenv("CERTIFICATION_SANDBOX_TIER", "Development")
    monkeypatch.setenv("CERTIFICATION_COST_CAP_MONTHLY", "250")


# --- the machine is kept ------------------------------------------------------

def test_a_healthy_proof_keeps_the_machine(db_session):
    seen = {}

    def capture(reference, payload):
        seen["reference"] = reference
        return {"captured": True, "image_ocid": "ocid1.image.oc1..abc",
                "source_instance_ocid": "ocid1.instance.oc1..xyz",
                "size_gb": 50, "detail": "Capturing."}

    outcome = _run(db_session, capture=capture)

    assert outcome.status == "passed"
    rows = _images(db_session)
    assert len(rows) == 1, "the proven machine was thrown away"
    assert rows[0].image_ocid == "ocid1.image.oc1..abc"
    assert rows[0].technology_code == "dotnet8"
    assert rows[0].proof_reference == seen["reference"], (
        "the image cannot be traced back to the proof that justified it")


def test_the_capture_happens_BEFORE_the_teardown(db_session):
    """The instance only exists between verify and destroy. Capturing after the
    teardown would photograph nothing, and would do it silently."""
    order: list[str] = []
    post = _post()
    original = post

    def tracking_post(path, payload):
        order.append(path)
        return original(path, payload)

    def capture(reference, payload):
        order.append("/capture-image")
        return {"captured": True, "image_ocid": "ocid1.image.oc1..abc"}

    proof.run_proof(db_session, _Blueprint(), post=tracking_post,
                    price=lambda c: 10.0,
                    verify=lambda r, p: (True, "ok"), capture=capture)

    assert "/capture-image" in order, "nothing was captured at all"
    assert order.index("/capture-image") < order.index("/destroy"), (
        f"the machine was destroyed before it was captured: {order}")


# --- a capture can never change the verdict -----------------------------------

def test_an_unhealthy_proof_captures_NOTHING(db_session):
    """There is nothing to keep. An image of a machine that never worked is
    worse than no image: it is a broken install, made permanent and reusable."""
    called = []
    outcome = _run(db_session, healthy=False,
                   capture=lambda r, p: called.append(r) or {"captured": True})

    assert outcome.status == "failed"
    assert not called, "a machine that failed to verify was captured anyway"
    assert _images(db_session) == []


def test_a_REFUSED_capture_still_passes_the_proof(db_session):
    outcome = _run(db_session,
                   capture=lambda r, p: {"captured": False, "detail": "quota exceeded"})

    assert outcome.status == "passed", (
        "a failed capture changed the verdict of a proof that had already "
        "verified healthy")
    rows = _images(db_session)
    assert len(rows) == 1 and rows[0].state == "failed"
    assert "quota exceeded" in rows[0].detail, (
        "the reason was lost, so nobody can find out why images stopped")


def test_a_capture_that_EXPLODES_still_passes_the_proof(db_session):
    """The branch a real orchestrator will not produce on demand, which is
    exactly why it is injected and tested here."""
    def capture(reference, payload):
        raise RuntimeError("the SDK fell over")

    outcome = _run(db_session, capture=capture)

    assert outcome.status == "passed"
    rows = _images(db_session)
    assert len(rows) == 1 and rows[0].state == "failed"
    assert "RuntimeError" in rows[0].detail


def test_no_capture_path_wired_is_recorded_not_ignored(db_session):
    """Silence is the failure mode that hides. If nothing is wired in, the proof
    must still pass AND still say so."""
    outcome = _run(db_session, capture=None)

    assert outcome.status == "passed"
    rows = _images(db_session)
    assert len(rows) == 1 and rows[0].state == "failed"
    assert "wired" in rows[0].detail


def test_a_capture_returning_junk_does_not_pass_for_success(db_session):
    outcome = _run(db_session, capture=lambda r, p: None)
    assert outcome.status == "passed"
    assert _images(db_session)[0].state == "failed"


# --- the teardown is still unconditional --------------------------------------

def test_the_machine_is_still_destroyed_when_the_capture_succeeds(db_session):
    """ARCHITECTURE.md §4: a proof that leaves a resource behind is a failed
    proof however healthy the resource was. Keeping an IMAGE is not a licence to
    keep the MACHINE — one is a 50 GB snapshot, the other is a running VM."""
    post = _post()
    proof.run_proof(db_session, _Blueprint(), post=post, price=lambda c: 10.0,
                    verify=lambda r, p: (True, "ok"),
                    capture=lambda r, p: {"captured": True, "image_ocid": "ocid1.x"})

    assert "/destroy" in post.calls, "the proven machine was left running"
