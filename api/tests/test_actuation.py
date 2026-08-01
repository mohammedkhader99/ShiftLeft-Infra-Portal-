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


def _prov(session, reference="REQ-A", bucket="b1", power="running"):
    req = Request(reference=reference, status="provisioned", requester="u@x.com",
                  request_type="create", environment_name=reference.lower())
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


# --- Auto-shutdown sweep drives actuation (F-FIN-06) -------------------------

def _enable_shutdown(monkeypatch, off_hours, envs):
    """Enable the sweep, force the off/on-hours state, and feed it the given
    non-prod environments (bypassing the pricing-based selection)."""
    monkeypatch.setattr(main.autoshutdown, "enabled", lambda: True)
    monkeypatch.setattr(main.autoshutdown, "is_off_hours", lambda *a, **k: off_hours)
    monkeypatch.setattr(main.autoshutdown, "shutdown_savings",
                        lambda s: {"environments": envs, "total_saving": 42.0})
    main._shutdown_last_off = None


def test_sweep_stops_nonprod_compute_off_hours(session, monkeypatch):
    _prov(session, "REQ-NP", power="running")
    _mock_actuate(monkeypatch)
    _enable_shutdown(monkeypatch, off_hours=True, envs=[{"reference": "REQ-NP"}])
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "stopped"
    events = [e.event for e in session.scalars(select(AuditLog).where(AuditLog.reference == "REQ-NP"))]
    assert "resource.stopped" in events
    swept = session.scalar(select(AuditLog).where(AuditLog.event == "shutdown.paused"))
    assert swept.actor == "auto-shutdown" and "REQ-NP" in swept.detail["actuated"]


def test_sweep_starts_compute_in_hours(session, monkeypatch):
    _prov(session, "REQ-NP", power="stopped")
    _mock_actuate(monkeypatch)
    _enable_shutdown(monkeypatch, off_hours=False, envs=[{"reference": "REQ-NP"}])
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "running"
    assert session.scalar(select(AuditLog).where(AuditLog.event == "shutdown.resumed")) is not None


def test_sweep_noop_when_disabled(session, monkeypatch):
    _prov(session, "REQ-NP", power="running")
    monkeypatch.setattr(main.autoshutdown, "enabled", lambda: False)
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not actuate")))
    main._shutdown_last_off = None
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "running"


def test_sweep_skips_already_stopped(session, monkeypatch):
    _prov(session, "REQ-NP", power="stopped")
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("already stopped")))
    _enable_shutdown(monkeypatch, off_hours=True, envs=[{"reference": "REQ-NP"}])
    main._sweep_shutdowns(session)
    assert main._power_for(session, "REQ-NP") == "stopped"


def test_sweep_skips_buckets(session, monkeypatch):
    req = Request(reference="REQ-B", status="provisioned", requester="u@x.com",
                  request_type="create", environment_name="req-b")
    session.add(req)
    session.add(Approval(jira_key="INFRA-2", status="approved", request=req))
    session.add(ProvisionedResource(reference="REQ-B", kind="oci-bucket", name="bkt",
                                    lifecycle_state="active", power_state="running"))
    session.commit()
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not actuate a bucket")))
    _enable_shutdown(monkeypatch, off_hours=True, envs=[{"reference": "REQ-B"}])
    main._sweep_shutdowns(session)  # bucket has no power — skipped, no orchestrator call
    swept = session.scalar(select(AuditLog).where(AuditLog.event == "shutdown.paused"))
    assert swept.detail["actuated"] == []
