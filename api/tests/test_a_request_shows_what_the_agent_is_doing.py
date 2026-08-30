"""A request must not sit silent while real machines are being spent on it.

REQ-2026-0239 rested on `auto-building` for twenty minutes. In that time the
agent found MongoDB's own container image, pinned it by digest, built a real
machine, installed it, asked the machine what it had become, destroyed it,
narrowed the recipe's published ports, and did the whole thing a second time.
Two machines, twenty minutes, and real money.

What the requester could see was a stepper resting on "Planned" and a panel
saying "oci-bucket · not a machine — nothing boots, so nothing can report".

EVERY FACT WAS ALREADY IN THE DATABASE. Proof rows are written as a proof starts
and updated when it ends, because certification is DERIVED from them
(ARCHITECTURE P8) rather than being a flag someone sets. Nothing read them out.
So this adds a READ and no new bookkeeping: it cannot slow a build down or
change what one does.

WHAT IT MUST NOT CLAIM. Which RUNG of the install ladder is being tried right
now is not knowable from stored state — those attempts live in memory until the
run ends. `attempts_recorded` is False so the panel says so, rather than leaving
a reader to conclude that nothing is happening.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.models import AuditLog, CertificationProof, Request, RequestComponent
from db.seed import seed
from db.session import Base

WORKFLOW_TS = "webapp/frontend/src/workflow.ts"


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


@pytest.fixture()
def client(db):
    def override():
        yield db

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def a_request(db, status="auto-building", code="mongodb"):
    req = Request(reference="REQ-2026-9999", requester="a@b.c", status=status,
                  deployment_target="oci", environment_name="test",
                  project_code="BIO", cost_centre_code="CC1")
    req.components = [RequestComponent(technology_code=code, size="small")]
    db.add(req)
    db.flush()
    return req


def a_proof(db, status, code="mongodb", minutes=8, finished=True):
    started = datetime(2026, 8, 30, 13, 7, 53, tzinfo=timezone.utc)
    db.add(CertificationProof(
        technology_code=code, deployment_target="oci",
        resource_kind="oci-service-vm",
        reference=f"PROOF-{code.upper()}-{status}-{minutes}",
        status=status,
        detail="Built, verified, and torn down. 90.59 monthly.",
        planned_monthly=90.59, started_at=started,
        finished_at=started + timedelta(minutes=minutes) if finished else None))
    db.flush()


# --- what the endpoint reports ---------------------------------------------------

def test_the_proofs_already_recorded_are_reported(client, db):
    a_request(db)
    a_proof(db, "failed")
    a_proof(db, "passed", minutes=9)
    db.commit()

    body = client.get("/api/requests/REQ-2026-9999/autobuild").json()
    proofs = body["components"][0]["proofs"]

    assert [p["status"] for p in proofs] == ["failed", "passed"]
    assert proofs[0]["seconds"] == 8 * 60
    assert proofs[1]["planned_monthly"] == 90.59


def test_a_proof_still_running_reports_how_long_it_has_been_going(client, db):
    """The one a requester actually wants while they wait. A proof with no
    finish time must report elapsed time, not zero and not nothing."""
    a_request(db)
    a_proof(db, "running", finished=False)
    db.commit()

    proof = client.get("/api/requests/REQ-2026-9999/autobuild").json()[
        "components"][0]["proofs"][0]

    assert proof["status"] == "running"
    assert proof["finished_at"] is None
    assert proof["seconds"] > 0


def test_whether_the_agent_is_still_working_is_reported(client, db):
    a_request(db, status="auto-building")
    db.commit()
    assert client.get("/api/requests/REQ-2026-9999/autobuild").json()["active"] is True


def test_a_finished_run_is_not_reported_as_active(client, db):
    """The panel changes its own heading on this, so it must be the request's
    real status and not merely "there are proofs"."""
    a_request(db, status="in-progress")
    a_proof(db, "passed")
    db.commit()

    assert client.get("/api/requests/REQ-2026-9999/autobuild").json()["active"] is False


def test_it_never_claims_to_know_which_rung_is_being_tried(client, db):
    """The install ladder's attempts are in memory until a run ends. Reporting
    True here would put a confident, empty list in front of a requester."""
    a_request(db)
    db.commit()

    assert client.get(
        "/api/requests/REQ-2026-9999/autobuild").json()["attempts_recorded"] is False


def test_a_component_with_nothing_recorded_is_still_listed(client, db):
    """Absent and unstarted are different. A component with no proofs must
    appear with an empty list, so the panel can say "nothing yet" rather than
    silently omitting the only thing the request is waiting on."""
    a_request(db)
    db.commit()

    body = client.get("/api/requests/REQ-2026-9999/autobuild").json()

    assert [c["code"] for c in body["components"]] == ["mongodb"]
    assert body["components"][0]["proofs"] == []
    assert body["components"][0]["recipe"] is None


def test_an_unknown_request_is_a_404(client):
    assert client.get("/api/requests/REQ-2026-0000/autobuild").status_code == 404


def test_the_proofs_of_another_technology_are_not_borrowed(client, db):
    """Proof rows are keyed by technology, not by request. Reporting every
    proof would show a requester somebody else's failures as their own."""
    a_request(db, code="mongodb")
    a_proof(db, "failed", code="rabbitmq")
    db.commit()

    assert client.get(
        "/api/requests/REQ-2026-9999/autobuild").json()["components"][0]["proofs"] == []


# --- the contract the stepper depends on -----------------------------------------

def test_the_audit_events_the_stepper_keys_on_are_the_ones_the_api_writes():
    """THE STEPPER IS DERIVED FROM THE AUDIT TRAIL, so a renamed event would
    silently remove the stage rather than break anything loudly.

    The front end has no test runner, so the halves are held together here: the
    API writes these two events, and workflow.ts keys on these two names.
    """
    from pathlib import Path

    api_source = Path("api/main.py").read_text(encoding="utf-8")
    stepper = Path(WORKFLOW_TS).read_text(encoding="utf-8")

    for event in ("autobuild.started", "autobuild.finished"):
        assert f'"{event}"' in api_source, f"the API no longer writes {event}"
        assert f"'{event}'" in stepper, (
            f"workflow.ts no longer keys on {event}; the 'Making it buildable' "
            f"stage would silently disappear from the stepper")


def test_the_stage_only_appears_when_the_agent_actually_ran():
    """A request the agent never touched must not grow a stage explaining that
    it did not. The stage is conditional on the audit event, like every other."""
    from pathlib import Path

    stepper = Path(WORKFLOW_TS).read_text(encoding="utf-8")
    at = stepper.index("Making it buildable")
    guard = stepper[max(0, at - 400):at]

    assert "'autobuild.started' in firstTs" in guard, (
        "the stage is not conditional on the agent having run")
