"""Anomaly detection checks (F-FIN-09 cost + F-RPT-10 request).

Each deterministic detector fires on a crafted case and stays quiet on a normal
estate. Read-only — the detectors only flag signals.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import anomalies
from api.main import app, get_session
from db.models import ActualCost, Budget, Estimate, Request, RequestComponent
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


def _req(session, reference, status, monthly, *, requester="u@x.com", tier=None,
         classification=None, target=None, cost_centre=None, request_type="create",
         advanced=None, sizes=None):
    req = Request(reference=reference, status=status, requester=requester,
                  request_type=request_type, environment_tier=tier,
                  data_classification=classification, deployment_target=target,
                  cost_centre_code=cost_centre, advanced_options=advanced)
    if sizes:
        req.components = [RequestComponent(technology_code="t", size=s) for s in sizes]
    session.add(req)
    if monthly is not None:
        session.add(Estimate(request=req, monthly=monthly, annual=monthly * 12, one_time=0, currency="AED"))
    session.commit()
    return req


# --- Cost anomalies (F-FIN-09) -----------------------------------------------

def test_cost_outlier(session):
    for i in range(6):
        _req(session, f"REQ-N{i}", "provisioned", 200, requester=f"u{i}@x.com")
    _req(session, "REQ-BIG", "provisioned", 3000, requester="big@x.com")
    out = anomalies._cost_outliers(session)
    assert [a["reference"] for a in out] == ["REQ-BIG"]
    assert out[0]["kind"] == "cost" and out[0]["type"] == "outlier"


def test_cost_overrun(session):
    _req(session, "REQ-OV", "provisioned", 1000)
    session.add(ActualCost(reference="REQ-OV", billed_monthly=2000))
    session.commit()
    out = anomalies._cost_overruns(session)
    assert len(out) == 1 and out[0]["severity"] == "high" and out[0]["detail"]["variance_pct"] == 100.0


def test_budget_breach(session):
    _req(session, "REQ-B", "provisioned", 1200, cost_centre="CC1")
    session.add(Budget(cost_centre_code="CC1", monthly_limit=1000))
    session.commit()
    out = anomalies._budget_breaches(session)
    assert len(out) == 1 and out[0]["severity"] == "high" and out[0]["subject"] == "CC1"


# --- Request anomalies (F-RPT-10) --------------------------------------------

def test_requester_burst(session):
    for i in range(5):
        _req(session, f"REQ-S{i}", "submitted", 100, requester="spammer@x.com")
    out = anomalies._bursts(session)
    assert len(out) == 1 and out[0]["subject"] == "spammer@x.com" and out[0]["detail"]["count"] == 5


def test_sensitive_exposure(session):
    _req(session, "REQ-X", "provisioned", 500, classification="restricted", target="azure")
    out = anomalies._sensitive_exposure(session)
    assert len(out) == 1 and out[0]["severity"] == "high"
    assert "public cloud" in out[0]["signal"].lower()


def test_high_impact_inflight(session):
    _req(session, "REQ-XL", "submitted", 900, sizes=["medium", "xlarge"])
    _req(session, "REQ-PROD", "planned", 900, tier="prod")
    out = anomalies._high_impact(session)
    assert {a["reference"] for a in out} == {"REQ-XL", "REQ-PROD"}
    assert all(a["severity"] == "low" for a in out)


# --- Aggregation + endpoint --------------------------------------------------

def test_clean_estate_has_no_anomalies(session):
    _req(session, "REQ-C1", "provisioned", 300, requester="a@x.com", tier="dev",
         classification="internal", target="onprem")
    _req(session, "REQ-C2", "provisioned", 320, requester="b@x.com", tier="test",
         classification="internal", target="onprem")
    assert anomalies.detect_anomalies(session)["count"] == 0


def test_endpoint_sorts_and_gates(client, session, monkeypatch):
    _req(session, "REQ-X", "provisioned", 500, classification="restricted", target="azure")
    body = client.get("/api/anomalies").json()
    assert body["count"] >= 1 and body["by_severity"]["high"] >= 1
    assert body["anomalies"][0]["severity"] == "high"  # most-severe first
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    assert client.get("/api/anomalies", headers={"X-Requester": "req@x.com"}).status_code == 403
