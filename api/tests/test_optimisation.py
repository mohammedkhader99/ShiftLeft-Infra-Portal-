"""Optimisation digest checks (F-FIN-12).

Each non-prod rule produces a recommendation with a re-priced saving; prod is
left alone; recommendations roll up per owner. Read-only.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import optimisation
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


def _prov(session, reference, tier, *, target="onprem", comps=(("postgres16", "medium"),),
          advanced=None, requester="u@x.com", env_owner=None):
    req = Request(reference=reference, status="provisioned", requester=requester,
                  request_type="create", environment_name=reference.lower(),
                  environment_tier=tier, deployment_target=target,
                  advanced_options=advanced, environment_owner=env_owner)
    req.components = [RequestComponent(technology_code=t, size=s) for t, s in comps]
    session.add(req)
    session.commit()
    return req


def _recs(session, req):
    return {r["type"]: r for r in optimisation._recommendations(session, req)}


def test_rightsize_large_component(session):
    req = _prov(session, "REQ-RS", "uat", comps=[("postgres16", "large")])
    recs = _recs(session, req)
    assert "rightsize" in recs and recs["rightsize"]["monthly_saving"] > 0


def test_disable_ha_on_nonprod(session):
    req = _prov(session, "REQ-HA", "dev", advanced={"high_availability": True})
    recs = _recs(session, req)
    assert "disable-ha" in recs and recs["disable-ha"]["monthly_saving"] > 0


def test_downgrade_monitoring_and_support(session):
    req = _prov(session, "REQ-OPT", "test",
                advanced={"monitoring_level": "enhanced", "support_tier": "premium"})
    recs = _recs(session, req)
    assert "downgrade-monitoring" in recs and recs["downgrade-monitoring"]["monthly_saving"] > 0
    assert "downgrade-support" in recs and recs["downgrade-support"]["monthly_saving"] > 0


def test_prod_is_left_alone(session):
    req = _prov(session, "REQ-PROD", "prod", comps=[("postgres16", "xlarge")],
                advanced={"high_availability": True})
    assert optimisation._recommendations(session, req) == []


def test_well_sized_nonprod_has_no_recs(session):
    req = _prov(session, "REQ-OK", "dev", comps=[("postgres16", "medium")])
    assert optimisation._recommendations(session, req) == []


def test_digest_groups_by_owner(session):
    _prov(session, "REQ-A", "uat", comps=[("postgres16", "large")], env_owner="alice@x.com")
    _prov(session, "REQ-B", "dev", advanced={"high_availability": True}, env_owner="alice@x.com")
    _prov(session, "REQ-C", "sit", comps=[("postgres16", "xlarge")], env_owner="bob@x.com")
    digest = optimisation.optimisation_digest(session)
    assert digest["owner_count"] == 2 and digest["environment_count"] == 3
    alice = next(o for o in digest["owners"] if o["owner"] == "alice@x.com")
    assert len(alice["environments"]) == 2
    assert alice["saving"] == round(sum(e["saving"] for e in alice["environments"]), 2)
    # Owners ranked by total saving.
    assert digest["owners"] == sorted(digest["owners"], key=lambda o: o["saving"], reverse=True)
    assert digest["total_saving"] == round(sum(o["saving"] for o in digest["owners"]), 2)


def test_endpoint_filter_and_rbac(client, session, monkeypatch):
    _prov(session, "REQ-A", "uat", comps=[("postgres16", "large")], env_owner="alice@x.com")
    _prov(session, "REQ-C", "sit", comps=[("postgres16", "xlarge")], env_owner="bob@x.com")
    all_ = client.get("/api/optimisation").json()
    assert all_["owner_count"] == 2 and all_["total_saving"] > 0
    scoped = client.get("/api/optimisation?owner=alice@x.com").json()
    assert scoped["owner_count"] == 1 and scoped["owners"][0]["owner"] == "alice@x.com"
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    assert client.get("/api/optimisation", headers={"X-Requester": "req@x.com"}).status_code == 403
