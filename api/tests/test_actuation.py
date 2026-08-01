"""Actuation checks (stop/start from the portal, cloud-sync increment 2).

Unlike reconciliation (increment 1, read-only), actuation CHANGES resource power
state. It is platform-admin gated + audited, runs through the orchestrator (signed
handoff), and is idempotent. The orchestrator call is mocked so the suite stays
offline; mock mode changes no real cloud regardless.
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


def _mock_actuate(monkeypatch, status=200, fail=False):
    """Make the orchestrator /actuate echo the requested power state."""
    def _fake(body, sig, path="/provision"):
        if fail:
            return None, "connection refused"
        payload = json.loads(body)
        power = "stopped" if payload.get("action") == "stop" else "running"
        return FakeResp(status, {"resources": [{"name": "b1", "power_state": power}]}), None
    monkeypatch.setattr(main, "_post_to_orchestrator", _fake)


def _prov(session, reference="REQ-A", bucket="b1", power="running", tier="uat", auto_stopped=None):
    req = Request(reference=reference, status="provisioned", requester="u@x.com",
                  request_type="create", environment_name=reference.lower(),
                  environment_tier=tier, auto_stopped=auto_stopped)
    session.add(req)
    session.add(Approval(jira_key="INFRA-1", status="approved", request=req))
    # Actuation targets compute instances (buckets can't be stopped/started).
    session.add(ProvisionedResource(reference=reference, kind="oci-instance", name=bucket,
                                    lifecycle_state="active", power_state=power))
    session.commit()
    return req


# --- Orchestrator adapter ----------------------------------------------------

def test_adapter_mock_stop_reports_stopped():
    out = cloud_state.actuate("REQ-1", [{"kind": "oci-bucket", "name": "b1"}], "stop")
    assert out == [{"kind": "oci-bucket", "name": "b1", "power_state": "stopped", "source": "mock"}]


def test_adapter_mock_start_reports_running():
    out = cloud_state.actuate("REQ-1", [{"kind": "oci-bucket", "name": "b1"}], "start")
    assert out[0]["power_state"] == "running"


def test_adapter_rejects_unknown_action():
    with pytest.raises(ValueError):
        cloud_state.actuate("REQ-1", [{"kind": "oci-bucket", "name": "b1"}], "reboot")


def test_adapter_live_is_unconfigured(monkeypatch):
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    with pytest.raises(cloud_state.CloudStateUnavailable):
        cloud_state.actuate("REQ-1", [{"kind": "oci-bucket", "name": "b1"}], "stop")


# --- Endpoint: stop / start --------------------------------------------------

def test_stop_sets_power_and_audits(client, session, monkeypatch):
    _prov(session)
    _mock_actuate(monkeypatch)
    body = client.post("/api/requests/REQ-A/actuate", json={"action": "stop"}).json()
    assert body["power"] == "stopped"
    res = session.scalar(select(ProvisionedResource).where(ProvisionedResource.reference == "REQ-A"))
    assert res.power_state == "stopped"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "resource.stopped")).all()


def test_start_brings_it_back(client, session, monkeypatch):
    _prov(session, power="stopped")
    _mock_actuate(monkeypatch)
    body = client.post("/api/requests/REQ-A/actuate", json={"action": "start"}).json()
    assert body["power"] == "running"
    res = session.scalar(select(ProvisionedResource).where(ProvisionedResource.reference == "REQ-A"))
    assert res.power_state == "running"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "resource.started")).all()


def test_stop_is_idempotent_and_skips_orchestrator(client, session, monkeypatch):
    _prov(session, power="stopped")
    # If the orchestrator were called it would raise; the endpoint must short-circuit.
    def _boom(*a, **k):
        raise AssertionError("orchestrator must not be called when already in target state")
    monkeypatch.setattr(main, "_post_to_orchestrator", _boom)
    body = client.post("/api/requests/REQ-A/actuate", json={"action": "stop"}).json()
    assert body["idempotent"] is True and body["power"] == "stopped"


# --- Guards ------------------------------------------------------------------

def test_rejects_non_provisioned(client, session):
    session.add(Request(reference="REQ-D", status="draft", requester="u@x.com", request_type="create"))
    session.commit()
    assert client.post("/api/requests/REQ-D/actuate", json={"action": "stop"}).status_code == 400


def test_rejects_unknown_action(client, session, monkeypatch):
    _prov(session)
    _mock_actuate(monkeypatch)
    assert client.post("/api/requests/REQ-A/actuate", json={"action": "reboot"}).status_code == 422


def test_orchestrator_failure_is_502(client, session, monkeypatch):
    _prov(session)
    _mock_actuate(monkeypatch, fail=True)
    r = client.post("/api/requests/REQ-A/actuate", json={"action": "stop"})
    assert r.status_code == 502
    # power unchanged on failure
    res = session.scalar(select(ProvisionedResource).where(ProvisionedResource.reference == "REQ-A"))
    assert res.power_state == "running"


def test_rbac_requester_is_forbidden(client, session, monkeypatch):
    _prov(session)
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.post("/api/requests/REQ-A/actuate", json={"action": "stop"},
                    headers={"X-Requester": "req@x.com"})
    assert r.status_code == 403


# --- Power exposed on the request views --------------------------------------

def test_power_state_on_list_and_detail(client, session, monkeypatch):
    _prov(session, power="stopped")
    row = next(r for r in client.get("/api/requests").json() if r["reference"] == "REQ-A")
    assert row["power"] == "stopped"
    assert client.get("/api/requests/REQ-A").json()["power"] == "stopped"


# --- Auto-shutdown sweep drives actuation, per-env (F-FIN-06 increment B) -----

def _policy(monkeypatch, off_hours, enabled=True):
    """Force each env's effective policy + the off/in-hours state."""
    monkeypatch.setattr(main.autoshutdown, "effective_policy",
                        lambda s, req: {"enabled": enabled, "days": "mon-fri",
                                        "start": "08:00", "end": "20:00", "tz": "UTC"})
    monkeypatch.setattr(main.autoshutdown, "is_off_hours", lambda *a, **k: off_hours)


def _never_orch(monkeypatch):
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("orchestrator must not be called")))


def test_sweep_stops_running_off_hours(session, monkeypatch):
    _prov(session, "REQ-NP", power="running")
    _mock_actuate(monkeypatch)
    _policy(monkeypatch, off_hours=True)
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "stopped"
    req = session.scalar(select(Request).where(Request.reference == "REQ-NP"))
    assert req.auto_stopped is True  # marked, so it will be auto-started later
    assert "resource.stopped" in [e.event for e in session.scalars(select(AuditLog).where(AuditLog.reference == "REQ-NP"))]


def test_sweep_starts_auto_stopped_in_hours(session, monkeypatch):
    _prov(session, "REQ-NP", power="stopped", auto_stopped=True)
    _mock_actuate(monkeypatch)
    _policy(monkeypatch, off_hours=False)
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "running"
    req = session.scalar(select(Request).where(Request.reference == "REQ-NP"))
    assert req.auto_stopped is False


def test_sweep_never_starts_a_manual_stop(session, monkeypatch):
    _prov(session, "REQ-NP", power="stopped", auto_stopped=None)  # stopped by a human
    _never_orch(monkeypatch)
    _policy(monkeypatch, off_hours=False)
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "stopped"  # left alone


def test_sweep_skips_when_effective_disabled(session, monkeypatch):
    _prov(session, "REQ-NP", power="running")
    _never_orch(monkeypatch)
    _policy(monkeypatch, off_hours=True, enabled=False)  # opt-out / master off
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "running"


def test_sweep_skips_prod(session, monkeypatch):
    _prov(session, "REQ-PROD", power="running", tier="prod")
    _never_orch(monkeypatch)
    _policy(monkeypatch, off_hours=True)
    main._sweep_shutdowns(session)  # prod isn't in nonprod_provisioned — untouched
    assert main._power_for(session, "REQ-PROD") == "running"


def test_sweep_skips_buckets(session, monkeypatch):
    req = Request(reference="REQ-B", status="provisioned", requester="u@x.com",
                  request_type="create", environment_name="req-b", environment_tier="uat")
    session.add(req)
    session.add(Approval(jira_key="INFRA-2", status="approved", request=req))
    session.add(ProvisionedResource(reference="REQ-B", kind="oci-bucket", name="bkt",
                                    lifecycle_state="active", power_state="running"))
    session.commit()
    _never_orch(monkeypatch)
    _policy(monkeypatch, off_hours=True)
    main._sweep_shutdowns(session)  # a bucket has no power — skipped, no orchestrator call
    assert main._power_for(session, "REQ-B") is None
