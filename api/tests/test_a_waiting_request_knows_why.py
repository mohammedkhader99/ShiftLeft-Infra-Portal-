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


# --- a cached answer about a request that has moved on is a wrong answer ----------

MY_REQUESTS = "webapp/frontend/src/pages/MyRequests.tsx"


def test_the_queue_panel_is_gated_on_the_live_status_not_only_the_payload():
    """REQ-2026-0243 read "Waiting for approval - nothing is built until this is
    approved in Jira (SDIMD-81211)" while its own stepper on the same screen said
    Approved 17:34 and the agent was already building it.

    The API was right: request.status was `auto-building` and approval.status was
    `approved`. The BROWSER was showing an answer it had fetched while the
    request was still `submitted`, kept after the fetch stopped.

    Two things fix it and this holds both: the cached answers are dropped when a
    request's status changes, and the panel additionally refuses to render for a
    request that is not in a waiting status -- so a stale payload cannot reach
    the screen even for the frame between the two.

    The front end has no test runner, so the contract is held here.
    """
    from pathlib import Path

    source = Path(MY_REQUESTS).read_text(encoding="utf-8")

    # The agent-progress heading is decided by the row's LIVE status too, for
    # the same reason: a fetched payload is a photograph of a moment that has
    # already passed, and the row's own status is what the list poll refreshes.
    assert "{r.status === 'auto-building'" in source, (
        "the progress heading is not driven by the request's live status")
    assert "building[r.reference]!.active" not in source, (
        "the heading still reads `active` from the cached payload, which is "
        "what left REQ-2026-0244 saying 'in progress' after it had moved on")

    for panel in ("queue[r.reference]?.awaiting_approval",
                  "queue[r.reference]?.waiting"):
        guard = source[max(0, source.index(panel) - 200):source.index(panel)]
        assert "WAITING_STATUSES.includes(r.status)" in guard, (
            f"the {panel} panel is not gated on the request's live status, so a "
            f"cached answer can be shown for a request that has moved on")


def test_cached_panels_are_dropped_when_the_request_moves_on():
    """The invalidation itself. Without it the panel is merely hidden while the
    stale answer sits in memory, and it would reappear the moment a request
    entered a waiting status again."""
    from pathlib import Path

    source = Path(MY_REQUESTS).read_text(encoding="utf-8")

    assert "fetchedUnder[ref] !== status" in source, (
        "nothing notices when a request's status changes")
    # ALL THREE. The first version of this listed setBoot and setQueue and left
    # setBuilding out — and the bug came straight back in the cache the test did
    # not name: REQ-2026-0244 went on reading "Making it buildable — in progress"
    # while the stepper beside it had that stage ticked. A guard that covers two
    # of three caches does not cover the class.
    for cache in ("setBoot", "setQueue", "setBuilding"):
        assert f"{cache}((s) => {{ const next = {{ ...s }}; delete next[ref]; return next }})" in source, (
            f"{cache} is not invalidated when the request's status changes")


def test_nothing_claims_the_present_tense_from_a_fetched_snapshot():
    """THE RULE, rather than the three places it was broken.

    Every "this is happening now" message on a request row was computed from a
    payload fetched at some earlier moment. Invalidating the caches on a status
    change narrows the window but does not close it: there is still a poll's
    width between a request moving on and the refetch landing, and REQ-2026-0244
    sat in exactly that window reading

        kafka - proving now - 6m49s so far

    while it was already provisioned. It was reported twice, in two different
    panels, because each fix addressed the instance in front of it.

    So the rule is: the ROW'S OWN STATUS decides whether anything is happening,
    and the payload only supplies the detail. A request that is not building
    cannot say it is proving anything.

    IT ALSO FIXES A CASE NOBODY HAS HIT YET. A proof abandoned mid-flight leaves
    its row `running` forever; without this it would read "proving now - 3 days
    so far" indefinitely.
    """
    from pathlib import Path

    source = Path(MY_REQUESTS).read_text(encoding="utf-8")

    assert "const stillBuilding = r.status === 'auto-building'" in source, (
        "there is no live-status gate for present-tense messages")

    # `live` is the summary's gate and is itself derived from the live status,
    # so either name counts as a guard.
    assert "const live = stillBuilding" in source, (
        "the summary's 'proving now' is not gated on the live status")

    lines = source.splitlines()
    for number, line in enumerate(lines):
        if line.lstrip().startswith("//"):
            continue  # a comment quoting the bug is not a claim
        for claim in ("'proving now'", "' so far'"):
            if claim not in line:
                continue
            # TWO LINES, NOT SIX. A wider window let ADJACENT claims cover for
            # each other: deleting the guard from the elapsed-time line still
            # found the one belonging to the line above it, and the plant went
            # undetected. A guard has to sit near enough to the thing it guards
            # that removing it is visible.
            window = "\n".join(lines[max(0, number - 2):number + 1])
            assert "stillBuilding" in window or "live" in window, (
                f"line {number + 1} renders {claim} without checking the request "
                f"is actually building, so a finished request can claim to be "
                f"working: {line.strip()}")
