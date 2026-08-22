"""Cancelling a request that is not going to be fulfilled.

REQ-2026-0176 is why this exists. It was quoted 0.00 AED for a component that
could not be priced, approved at that fiction, and then refused at execution —
leaving it in `in-progress` for ever, looking active. There was no way to close
it, so it was closed by editing the database.

THE RULE IS ABOUT RESOURCES, NOT STATUS. Cancel closes a record; it destroys
nothing. So a request that owns anything real must be refused and pointed at
decommission, whatever state its record happens to be in — closing the record
while the infrastructure keeps running is exactly how an orphan is made, and it
would still be billing. This portal already has a page for finding orphans; it
should not also have a button for making them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.models import AuditLog, ProvisionedResource, Request, RequestComponent
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def client(session):
    def override():
        yield session
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def make(session, reference, status, requester="a@b.com", resources=()):
    req = Request(reference=reference, requester=requester, request_type="create",
                  status=status, deployment_target="oci",
                  environment_name="test-env", environment_tier="Development")
    req.components = [RequestComponent(technology_code="nginx", size="small")]
    session.add(req)
    for kind in resources:
        session.add(ProvisionedResource(reference=reference, kind=kind,
                                        name=f"{reference}-{kind}"))
    session.commit()
    return req


# --- the case it was built for -----------------------------------------------

def test_a_stuck_request_can_be_closed(client, session):
    """REQ-2026-0176 sat in in-progress for ever after execution refused it."""
    make(session, "REQ-CANCEL-1", "in-progress")
    r = client.post("/api/requests/REQ-CANCEL-1/cancel",
                    json={"reason": "priced at 0.00 by mistake"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    row = session.scalar(select(Request).where(Request.reference == "REQ-CANCEL-1"))
    assert "Nothing was created" in row.status_detail
    assert "priced at 0.00 by mistake" in row.status_detail


@pytest.mark.parametrize("status", ["draft", "submitted", "planned", "in-progress",
                                    "apply-failed", "verify-failed", "manual-fulfil"])
def test_every_state_where_nothing_is_being_built_can_be_cancelled(client, session, status):
    make(session, f"REQ-CAN-{status}", status)
    r = client.post(f"/api/requests/REQ-CAN-{status}/cancel", json={"reason": "x"})
    assert r.status_code == 200, f"{status}: {r.text}"


def test_cancelling_is_recorded_in_the_audit_trail(client, session):
    make(session, "REQ-CANCEL-2", "submitted")
    client.post("/api/requests/REQ-CANCEL-2/cancel", json={"reason": "changed my mind"})

    entry = session.scalar(select(AuditLog).where(
        AuditLog.reference == "REQ-CANCEL-2", AuditLog.event == "request.cancelled"))
    assert entry is not None, "a request was closed with no record of who or why"
    assert entry.detail["was"] == "submitted"
    assert entry.detail["reason"] == "changed my mind"
    assert entry.entry_hash, "the entry was not chained"


# --- THE safety property -----------------------------------------------------

def test_a_request_that_built_something_is_refused(client, session):
    """The property that matters. Cancel closes a record and destroys nothing, so
    closing one that owns live infrastructure would leave it running and billing
    with no request pointing at it."""
    make(session, "REQ-CANCEL-3", "in-progress", resources=("oci-service-vm",))
    r = client.post("/api/requests/REQ-CANCEL-3/cancel", json={"reason": "oops"})

    assert r.status_code == 409
    assert session.scalar(select(Request).where(
        Request.reference == "REQ-CANCEL-3")).status == "in-progress"


def test_the_refusal_names_what_was_built_and_what_to_do_instead(client, session):
    """A refusal that does not say what to do next is half a refusal."""
    make(session, "REQ-CANCEL-4", "apply-failed", resources=("oci-service-vm",))
    detail = client.post("/api/requests/REQ-CANCEL-4/cancel",
                         json={"reason": ""}).json()["detail"]

    assert "oci-service-vm" in detail
    assert "billing" in detail
    assert "ecommission" in detail


def test_a_provisioned_request_must_be_decommissioned_not_cancelled(client, session):
    """A running environment is torn down by decommission, which destroys the
    infrastructure and then closes the loop."""
    make(session, "REQ-CANCEL-5", "provisioned")
    r = client.post("/api/requests/REQ-CANCEL-5/cancel", json={"reason": "done"})
    assert r.status_code == 409


def test_a_decommissioned_request_cannot_be_cancelled(client, session):
    make(session, "REQ-CANCEL-6", "decommissioned")
    assert client.post("/api/requests/REQ-CANCEL-6/cancel",
                       json={"reason": ""}).status_code == 409


# --- who may do it -----------------------------------------------------------

def test_cancelling_twice_is_harmless(client, session):
    """A double-click, or a retry after a timeout, must not be an error."""
    make(session, "REQ-CANCEL-7", "submitted")
    first = client.post("/api/requests/REQ-CANCEL-7/cancel", json={"reason": "x"})
    second = client.post("/api/requests/REQ-CANCEL-7/cancel", json={"reason": "x"})

    assert first.status_code == 200 and second.status_code == 200
    entries = session.scalars(select(AuditLog).where(
        AuditLog.reference == "REQ-CANCEL-7",
        AuditLog.event == "request.cancelled")).all()
    assert len(entries) == 1, "the second cancel wrote a second record"


def test_a_reason_is_optional_but_the_actor_is_not(client, session):
    """Who closed it is always recorded, even when they said nothing about why."""
    make(session, "REQ-CANCEL-8", "draft")
    client.post("/api/requests/REQ-CANCEL-8/cancel", json={"reason": ""})

    row = session.scalar(select(Request).where(Request.reference == "REQ-CANCEL-8"))
    assert "Cancelled by" in row.status_detail


# --- it must not be picked up again ------------------------------------------

def test_a_cancelled_request_is_not_swept_up_and_provisioned(client, session):
    """The sweep only ever picks up 'submitted' and 'planned'. Asserted here
    because a cancelled request quietly resuming would be the worst outcome of
    all — a closed record building infrastructure."""
    import inspect
    import api.main as main

    src = inspect.getsource(main)
    assert '"cancelled"' in src or "CANCELLED" in src
    make(session, "REQ-CANCEL-9", "submitted")
    client.post("/api/requests/REQ-CANCEL-9/cancel", json={"reason": ""})
    row = session.scalar(select(Request).where(Request.reference == "REQ-CANCEL-9"))
    assert row.status not in ("submitted", "planned")
