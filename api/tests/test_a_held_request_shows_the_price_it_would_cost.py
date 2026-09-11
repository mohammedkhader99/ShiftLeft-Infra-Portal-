"""A request the cost guard is holding must not quote the price it was approved at.

REQ-2026-0305 is why this exists. The guard worked perfectly: minio could not be
priced when the request was approved, it was certified automatically while the
request was in flight, the real total turned out to be 1141.73 a month against
916.13, and the portal refused to build at a cost nobody had agreed to.

Then the screen said both numbers at once. The Monthly column printed 916.13,
under a header that just says "Monthly", and the banner immediately beneath it
said "it now costs 1141.73 AED a month". Only one of the two was live and
nothing on the row said which.

The browser could not have done better: the list sent `{currency, monthly}` and
the real figure existed nowhere structured — it was interpolated into a sentence.
Parsing a price back out of English in the browser would have been wrong twice,
so the figure is carried as a field, read from the guard's own audit entry.

BOTH NUMBERS STAY. `estimate` is what was APPROVED and must not move: F-FIN-01
measures actuals against it, and it is the record of what somebody said yes to.
`repriced` is what it would cost now. They are different facts, not two versions
of one, and the earlier bug was having a place to put only one of them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from db.models import Estimate, Request, RequestComponent
from db.seed import seed
from db.session import Base

APPROVED_AT = 916.13
COSTS_NOW = 1141.73
ALICE = {"X-Requester": "alice@example.com"}


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


def a_request(session, reference="REQ-HELD-1", requester="alice@example.com"):
    """An approved request carrying the estimate it was approved at."""
    req = Request(reference=reference, requester=requester, request_type="create",
                  status="in-progress", deployment_target="oci",
                  environment_name="test-env", environment_tier="Development")
    req.components = [RequestComponent(technology_code="minio", size="small")]
    session.add(req)
    session.flush()
    session.add(Estimate(request_id=req.id, currency="AED", one_time=0,
                         monthly=APPROVED_AT, annual=APPROVED_AT * 12))
    session.commit()
    return req


def held(session, reference="REQ-HELD-1", requester="alice@example.com"):
    """The request as the guard leaves it: held, with the real price recorded."""
    req = a_request(session, reference, requester)
    main._ask_for_reapproval(session, req, jira_key=None, monthly=COSTS_NOW)
    session.commit()
    return req


def row(client, reference):
    body = client.get("/api/requests", headers=ALICE).json()
    return next(r for r in body if r["reference"] == reference)


# --- the defect that was on screen -------------------------------------------

def test_the_row_carries_the_price_it_would_cost_now(session, client):
    held(session)
    assert row(client, "REQ-HELD-1")["repriced"]["monthly"] == COSTS_NOW


def test_the_approved_price_is_not_overwritten_to_make_the_screen_agree(session, client):
    """The cheap fix is to update the estimate until the two numbers match. That
    destroys the record of what was agreed, which is the fiction REQ-2026-0176
    was approved on, running the other way."""
    held(session)
    r = row(client, "REQ-HELD-1")

    assert r["estimate"]["monthly"] == APPROVED_AT
    assert r["repriced"]["monthly"] == COSTS_NOW
    # Numeric(14, 2) comes back as Decimal; the point is the value, not the type.
    assert float(session.scalar(select(Estimate.monthly))) == APPROVED_AT


def test_the_figure_on_the_row_is_the_one_in_the_banner(session, client):
    """The two came from different places and were allowed to disagree. They are
    now one fact read twice, so they cannot."""
    req = held(session)
    r = row(client, "REQ-HELD-1")

    assert f"{r['repriced']['monthly']:.2f}" in req.status_detail


# --- and it appears nowhere it should not ------------------------------------

def test_a_request_nobody_is_holding_has_no_live_price(session, client):
    a_request(session, "REQ-HELD-2")
    assert row(client, "REQ-HELD-2")["repriced"] is None


def test_a_held_request_that_was_cancelled_stops_quoting_a_price(session, client):
    """Cancelling is the remedy the banner offers. A closed request still
    quoting what it would cost a month reads as though it is going to."""
    held(session, "REQ-HELD-3")
    assert client.post("/api/requests/REQ-HELD-3/cancel", json={"reason": "raising a new one"},
                       headers=ALICE).status_code == 200
    assert row(client, "REQ-HELD-3")["repriced"] is None


def test_the_newest_figure_wins_when_the_guard_fires_twice(session):
    """A price can become knowable, then change again. The current cost is the
    one found last, not the first one recorded."""
    req = held(session, "REQ-HELD-4")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=1500.00)
    session.commit()

    assert main._repriced_for(session, req)["monthly"] == 1500.00


def test_a_guard_that_could_not_price_it_offers_no_figure(session):
    """Pricing can fail; the guard still holds the request and says so in words.
    A row that printed nothing where a number goes is honest; one that printed
    0.00 would be the fiction again."""
    req = a_request(session, "REQ-HELD-5")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=None)
    session.commit()

    assert req.status == main.COST_CHANGED
    assert main._repriced_for(session, req) is None
