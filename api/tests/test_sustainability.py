"""Sustainability estimate checks (F-FIN-13).

Indicative energy/carbon per environment from its sizing. Cloud is more efficient
than on-prem (lower PUE + grid intensity); bigger sizing means more energy.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import sustainability
from api.main import app, get_session
from db.models import Request, RequestComponent
from db.seed import seed
from db.session import Base


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


def _prov(session, reference, size, target):
    req = Request(reference=reference, status="provisioned", requester="u@x.com",
                  request_type="create", environment_name=reference.lower(),
                  environment_tier="uat", deployment_target=target)
    req.components = [RequestComponent(technology_code="postgres16", size=size)]
    session.add(req)
    session.commit()
    return req


# --- Footprint math ----------------------------------------------------------

def test_cloud_is_greener_than_onprem(session):
    onprem = sustainability.footprint(4, 16, 200, "onprem")
    azure = sustainability.footprint(4, 16, 200, "azure")
    assert onprem["energy_kwh_month"] > azure["energy_kwh_month"] > 0  # PUE
    assert onprem["carbon_kg_month"] > azure["carbon_kg_month"] > 0    # PUE + grid intensity


def test_energy_scales_with_sizing(session):
    small = sustainability.footprint(4, 16, 200, "onprem")
    large = sustainability.footprint(8, 64, 500, "onprem")
    assert large["energy_kwh_month"] > small["energy_kwh_month"]


def test_equivalents_present():
    eq = sustainability._equivalents(100.0)
    assert eq["car_km"] > 0 and eq["trees_year"] > 0


# --- Estate rollup + endpoint ------------------------------------------------

def test_estate_rollup_sums_and_sorts(session):
    _prov(session, "REQ-A", "large", "onprem")   # bigger + dirtier grid
    _prov(session, "REQ-B", "medium", "azure")   # smaller + greener
    est = sustainability.estate_footprint(session)
    assert est["environment_count"] == 2
    assert est["total_carbon_kg_month"] == round(
        sum(e["carbon_kg_month"] for e in est["environments"]), 1
    )
    # Sorted by carbon, biggest first.
    assert est["environments"][0]["reference"] == "REQ-A"
    assert est["total_energy_kwh_month"] > 0 and est["equivalents"]["car_km"] > 0


def test_endpoint_and_rbac(client, session, monkeypatch):
    _prov(session, "REQ-A", "large", "onprem")
    body = client.get("/api/sustainability").json()
    assert body["environment_count"] == 1 and body["total_carbon_kg_month"] > 0
    assert body["environments"][0]["vcpu"] == 8  # postgres16 large sizing
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    assert client.get("/api/sustainability", headers={"X-Requester": "req@x.com"}).status_code == 403
