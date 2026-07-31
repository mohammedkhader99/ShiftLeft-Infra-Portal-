"""Spend forecast checks (F-RPT-05).

The forecast projects monthly spend from today's provisioned run-rate + the
in-flight pipeline - non-prod environments as their TTL expires. Deterministic
and read-only.
"""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import forecast
from api.main import app, get_session
from db.models import Estimate, ProvisionedResource, Request
from db.seed import seed
from db.session import Base

TODAY = datetime.now(timezone.utc).date()


def _month_date(offset: int, day: int = 15) -> date:
    total = TODAY.month - 1 + offset
    return date(TODAY.year + total // 12, total % 12 + 1, day)


def _dt(offset: int) -> datetime:
    d = _month_date(offset)
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)
    yield session
    session.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override_get_session():
        yield _db

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _req(session, reference, status, monthly, *, request_type="create",
         delivery=None, ttl_month=None):
    req = Request(reference=reference, status=status, requester="x@x.com",
                  request_type=request_type, environment_name=reference.lower(),
                  required_delivery_date=delivery)
    session.add(req)
    session.add(Estimate(request=req, monthly=monthly, annual=monthly * 12, one_time=0, currency="AED"))
    if ttl_month is not None:
        session.add(ProvisionedResource(reference=reference, kind="oci-bucket",
                                        name=reference.lower(), ttl_expiry=_dt(ttl_month),
                                        lifecycle_state="active"))
    session.commit()
    return req


def _estate(session):
    _req(session, "REQ-P1", "provisioned", 1000)                       # base, never expires
    _req(session, "REQ-P2", "provisioned", 500, ttl_month=2)           # expires month 2
    _req(session, "REQ-PL1", "submitted", 300, delivery=_month_date(1))  # lands month 1
    _req(session, "REQ-PL2", "planned", 200)                          # lands month 1 (no date)
    _req(session, "REQ-DEC", "submitted", 400, request_type="decommission")  # excluded


def test_headline_totals(session):
    _estate(session)
    f = forecast.spend_forecast(session, months=6)
    assert f["current_monthly"] == 1500          # provisioned run-rate
    assert f["pipeline_monthly"] == 500           # 300 + 200 (decommission excluded)
    assert f["expiring_monthly"] == 500           # REQ-P2's TTL
    assert f["horizon_months"] == 6


def test_month_by_month_projection(session):
    _estate(session)
    months = {m["month"]: m for m in forecast.spend_forecast(session, months=6)["months"]}
    assert months[0]["projected_monthly"] == 1500   # today's run-rate
    assert months[1]["projected_monthly"] == 2000   # + pipeline (both land)
    assert months[2]["projected_monthly"] == 1500   # - REQ-P2 expires
    assert months[6]["projected_monthly"] == 1500   # settled


def test_drivers_are_ranked(session):
    _estate(session)
    d = forecast.spend_forecast(session, months=6)["drivers"]
    assert [p["monthly"] for p in d["pipeline"]] == [300, 200]
    assert [e["reference"] for e in d["expiring"]] == ["REQ-P2"]


def test_endpoint_and_rbac(client, session, monkeypatch):
    _estate(session)
    body = client.get("/api/forecast?months=6").json()
    assert body["current_monthly"] == 1500 and len(body["months"]) == 7
    # Oversight-gated: a plain requester is refused.
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.get("/api/forecast", headers={"X-Requester": "req@x.com"})
    assert r.status_code == 403
