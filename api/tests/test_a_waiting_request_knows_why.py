"""An approved request that is not moving must say why.

THE PORTAL PROVISIONS ONE REQUEST AT A TIME, ON PURPOSE. A single elected leader
runs the sweep, and the sweep walks the approved requests in turn — because two
sweeps thirty seconds apart once started duplicate builds for the same request.

Measured on 2026-08-30, that costs real time: REQ-2026-0240's two components
proved one after the other, nodejs20 starting five seconds after nginx finished,
and REQ-2026-0239 held the poller for twenty-six minutes before it. Anything
approved in that window simply sat there, and the portal said nothing at all.

AND THE QUEUE WAS NOT A QUEUE. The sweep's query carried no `order_by`, so it
took whatever order the database happened to return. Nothing depended on that
while nobody could see it. The moment a requester is told "two requests ahead of
you", it has to be true — so the order is now declared, and the display is
computed by THE SAME function the sweep uses. A queue shown one way and walked
another is a confident falsehood.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import main as api_main
from api.main import app, get_session
from db.models import Approval, Request, RequestComponent
from db.seed import seed
from db.session import Base


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


def a_request(db, ref, status="submitted", approved=True):
    req = Request(reference=ref, requester="a@b.c", status=status,
                  deployment_target="oci", environment_name="test",
                  project_code="BIO", cost_centre_code="CC1")
    req.components = [RequestComponent(technology_code="nginx", size="small")]
    db.add(req)
    db.flush()
    if approved:
        db.add(Approval(request_id=req.id, jira_key=f"SD-{ref[-4:]}",
                        status="approved"))
        db.flush()
    return req


# --- the queue is a queue --------------------------------------------------------

def test_the_queue_is_ordered(db):
    """It had no `order_by` at all. Whatever the database returned was the order
    the sweep took, and that is not something to tell a requester about."""
    for ref in ("REQ-2026-0003", "REQ-2026-0001", "REQ-2026-0002"):
        a_request(db, ref)
    db.commit()

    # Ordered by id — the order they were created, which is the order they were
    # approved in, which is the only fair one.
    assert api_main._queue_references(db) == [
        "REQ-2026-0003", "REQ-2026-0001", "REQ-2026-0002"]


def test_an_unapproved_request_is_not_in_the_queue(db):
    """The sweep only advances approved requests. Counting an unapproved one
    would tell everybody behind it that they are further back than they are."""
    a_request(db, "REQ-2026-0001", approved=False)
    a_request(db, "REQ-2026-0002")
    db.commit()

    assert api_main._queue_references(db) == ["REQ-2026-0002"]


def test_a_request_already_being_worked_on_is_not_queued(db):
    """It is not waiting; it is the reason everybody else is."""
    a_request(db, "REQ-2026-0001", status="auto-building")
    a_request(db, "REQ-2026-0002")
    db.commit()

    assert api_main._queue_references(db) == ["REQ-2026-0002"]


# --- what a waiting requester is told --------------------------------------------

def test_a_requester_is_told_how_many_are_ahead(client, db):
    a_request(db, "REQ-2026-0001")
    a_request(db, "REQ-2026-0002")
    a_request(db, "REQ-2026-0003")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0003/queue").json()

    assert body["waiting"] is True
    assert body["position"] == 3
    assert body["ahead"] == ["REQ-2026-0001", "REQ-2026-0002"]


def test_the_request_holding_the_queue_is_named(client, db):
    """"Two ahead of you" is much less useful than naming the one actually
    being built — that is the one whose progress explains the wait."""
    a_request(db, "REQ-2026-0001", status="auto-building")
    a_request(db, "REQ-2026-0002")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0002/queue").json()

    assert body["working_on"] == ["REQ-2026-0001"]
    assert body["ahead"] == []


def test_the_first_in_line_is_told_so(client, db):
    a_request(db, "REQ-2026-0001")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0001/queue").json()

    assert body["position"] == 1
    assert body["ahead"] == []


def test_a_request_that_is_finished_is_not_waiting(client, db):
    a_request(db, "REQ-2026-0001", status="provisioned")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0001/queue").json()

    assert body["waiting"] is False
    assert body["position"] is None


def test_the_poller_being_off_is_reported_not_implied(client, db, monkeypatch):
    """An empty queue and a stopped poller look identical from the outside and
    mean opposite things: one is "you are next", the other is "nothing is
    coming". Conflating them is the same fault as an unreachable orchestrator
    reported as an absence."""
    a_request(db, "REQ-2026-0001")
    db.commit()

    monkeypatch.setattr(api_main, "auto_provision_enabled", lambda: False)
    assert client.get(
        "/api/requests/REQ-2026-0001/queue").json()["poller_running"] is False

    monkeypatch.setattr(api_main, "auto_provision_enabled", lambda: True)
    assert client.get(
        "/api/requests/REQ-2026-0001/queue").json()["poller_running"] is True


def test_an_unknown_request_is_a_404(client):
    assert client.get("/api/requests/REQ-2026-0000/queue").status_code == 404


# --- the sweep and the display must not drift ------------------------------------

def test_the_sweep_walks_the_queue_this_endpoint_reports():
    """ONE FUNCTION, BOTH ANSWERS. If the sweep built its list one way and the
    display another, they would drift, and every defect found this month has
    been two sources of truth for one fact."""
    from pathlib import Path

    source = Path("api/main.py").read_text(encoding="utf-8")
    at = source.index("def _poll_once")
    body = source[at:at + 700]

    assert "_queue_references(session)" in body, (
        "_poll_once no longer uses the same queue the requester is shown")


# --- ahead in the sweep is not ahead in the queue ---------------------------------
#
# Deployed without this distinction for exactly one commit, and the live
# database said so at once: ten requests from 30 July to 16 August sit in
# `submitted` with a PENDING Jira ticket. The sweep must keep re-checking them --
# polling Jira is the whole reason they are there -- but they are not ahead of
# anybody in the build queue, and telling a new requester "10 requests ahead of
# you" would promise a wait that does not exist.

def a_pending_request(db, ref):
    req = a_request(db, ref, approved=False)
    db.add(Approval(request_id=req.id, jira_key=f"SD-{ref[-4:]}", status="pending"))
    db.flush()
    return req


def test_the_sweep_still_rechecks_a_pending_ticket(db):
    """It has to: polling Jira for the decision is why it is in the sweep."""
    a_pending_request(db, "REQ-2026-0001")
    db.commit()

    assert api_main._queue_references(db) == ["REQ-2026-0001"]


def test_a_pending_ticket_is_not_ahead_of_anybody(db):
    """THE DEFECT, as the live database showed it."""
    a_pending_request(db, "REQ-2026-0001")
    a_request(db, "REQ-2026-0002")
    db.commit()

    assert api_main._queue_references(db, approved_only=True) == ["REQ-2026-0002"]


def test_a_requester_behind_pending_tickets_is_not_told_they_are_ahead(client, db):
    for ref in ("REQ-2026-0001", "REQ-2026-0002"):
        a_pending_request(db, ref)
    a_request(db, "REQ-2026-0003")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0003/queue").json()

    assert body["ahead"] == []
    assert body["position"] == 1


def test_a_request_awaiting_approval_is_told_that_and_not_about_a_queue(client, db):
    """"Waiting to be built" would send its owner to watch a platform that is
    not the thing holding them up."""
    a_pending_request(db, "REQ-2026-0001")
    db.commit()

    body = client.get("/api/requests/REQ-2026-0001/queue").json()

    assert body["awaiting_approval"] is True
    assert body["jira_key"] == "SD-0001"


def test_an_approved_request_is_not_reported_as_awaiting_approval(client, db):
    a_request(db, "REQ-2026-0001")
    db.commit()

    assert client.get(
        "/api/requests/REQ-2026-0001/queue").json()["awaiting_approval"] is False
