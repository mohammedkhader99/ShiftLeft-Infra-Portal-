"""Read-only cloud state sync checks (reconciliation, increment 1).

The orchestrator adapter reports actual cloud state; the API reconciles it
against the registry and flags out-of-band changes. Observes only — nothing is
changed. The orchestrator call is mocked so the suite stays offline.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from orchestrator import cloud_state
from api.main import app, get_session
from db.models import Approval, AuditLog, ProvisionedResource, Request
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


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def _mock_orch(monkeypatch, resources):
    """Make the orchestrator /state return the given actual resources."""
    monkeypatch.setattr(
        main, "_post_to_orchestrator",
        lambda body, sig, path="/provision": (FakeResp(200, {"resources": resources}), None),
    )


def _prov(session, reference="REQ-S", bucket="b1"):
    req = Request(reference=reference, status="provisioned", requester="u@x.com",
                  request_type="create", environment_name=reference.lower())
    session.add(req)
    session.add(Approval(jira_key="INFRA-1", status="approved", request=req))
    session.add(ProvisionedResource(reference=reference, kind="oci-bucket", name=bucket,
                                    lifecycle_state="active"))
    session.commit()
    return req


# --- Orchestrator adapter ----------------------------------------------------

def test_adapter_mock_reflects_registry():
    out = cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])
    assert out == [{"kind": "oci-bucket", "name": "b1", "status": "active", "exists": True, "source": "mock"}]


def test_adapter_simulate_missing(monkeypatch):
    monkeypatch.setenv("CLOUD_STATE_SIMULATE_MISSING", "REQ-1,REQ-2")
    out = cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])
    assert out[0]["exists"] is False and out[0]["status"] == "missing"


def test_adapter_live_is_unconfigured(monkeypatch):
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    with pytest.raises(cloud_state.CloudStateUnavailable):
        cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])


# --- API reconciliation ------------------------------------------------------

def test_reconcile_in_sync(session, monkeypatch):
    req = _prov(session)
    _mock_orch(monkeypatch, [{"name": "b1", "status": "active", "exists": True}])
    result = main._reconcile(session, req)
    assert result["in_sync"] is True and result["divergences"] == []


def test_reconcile_detects_deleted_resource(session, monkeypatch):
    req = _prov(session)
    _mock_orch(monkeypatch, [{"name": "b1", "status": "missing", "exists": False}])
    result = main._reconcile(session, req)
    assert result["in_sync"] is False and result["divergences"][0]["issue"] == "missing"


def test_reconcile_detects_stopped_resource(session, monkeypatch):
    req = _prov(session)
    _mock_orch(monkeypatch, [{"name": "b1", "status": "stopped", "exists": True}])
    result = main._reconcile(session, req)
    assert result["in_sync"] is False and result["divergences"][0]["issue"] == "stopped"


# --- Endpoints ---------------------------------------------------------------

def test_reconcile_endpoint_stores_and_audits(client, session, monkeypatch):
    _prov(session)
    _mock_orch(monkeypatch, [{"name": "b1", "status": "missing", "exists": False}])
    body = client.post("/api/requests/REQ-S/reconcile").json()
    assert body["in_sync"] is False
    req = session.scalar(select(Request).where(Request.reference == "REQ-S"))
    assert req.state_status == "drifted" and req.state_synced_at is not None
    assert session.scalars(select(AuditLog).where(AuditLog.event == "state.drift")).all()


def test_reconcile_endpoint_rejects_non_provisioned(client, session):
    session.add(Request(reference="REQ-D", status="draft", requester="u@x.com", request_type="create"))
    session.commit()
    assert client.post("/api/requests/REQ-D/reconcile").status_code == 400


def test_reconcile_endpoint_rbac(client, session, monkeypatch):
    _prov(session)
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.post("/api/requests/REQ-S/reconcile", headers={"X-Requester": "req@x.com"})
    assert r.status_code == 403


def test_state_overview(client, session):
    _prov(session, "REQ-S")
    body = client.get("/api/state").json()
    assert body["count"] == 1 and body["sync_enabled"] is False
    assert body["environments"][0]["state_status"] == "unknown"  # not yet reconciled


# --- Opt-in sweep ------------------------------------------------------------

def test_sweep_is_a_noop_when_disabled(session, monkeypatch):
    _prov(session)
    _mock_orch(monkeypatch, [{"name": "b1", "status": "missing", "exists": False}])
    main._sweep_state(session)
    assert session.scalar(select(Request).where(Request.reference == "REQ-S")).state_status is None


def test_sweep_reconciles_when_enabled(session, monkeypatch):
    _prov(session)
    monkeypatch.setenv("CLOUD_STATE_SYNC_ENABLED", "true")
    _mock_orch(monkeypatch, [{"name": "b1", "status": "active", "exists": True}])
    main._sweep_state(session)
    assert session.scalar(select(Request).where(Request.reference == "REQ-S")).state_status == "in-sync"
