"""Self-service backup & restore checks (F-LCM-06).

Backups are owner-initiated (no approval); restoring FROM one is approval-governed
and verified. Mirrors the governed request-type pattern. The orchestrator call is
mocked so the suite stays offline; mock restores/backs up nothing real.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from api.validation import validate_submission
from db.models import Approval, AuditLog, Backup, Request
from db.seed import seed
from db.session import Base


@pytest.fixture()
def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override():
        yield _db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def _mock_restore(monkeypatch, ok=True, verified=True):
    def _fake(body, sig, path="/provision"):
        if not ok:
            return None, "connection refused"
        return FakeResp(200, {"restored": True, "verified": verified, "summary": "mock restore"}), None
    monkeypatch.setattr(main, "_post_to_orchestrator", _fake)
    monkeypatch.setattr(main, "jira_mode", lambda: "mock")
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)


def _prov(session, ref, tier="uat", requester="owner@x.com"):
    req = Request(reference=ref, status="provisioned", requester=requester, request_type="create",
                  environment_name=ref.lower(), environment_tier=tier, deployment_target="oci",
                  project_code="EGATE", cost_centre_code="IMD-1001")
    session.add(req)
    session.add(Approval(jira_key=f"INFRA-{ref}", status="approved", request=req))
    session.commit()
    return req


def _backup(session, ref, label="nightly"):
    b = Backup(reference=ref, label=label, created_by="owner@x.com")
    session.add(b)
    session.commit()
    return b


def _restore_req(session, target_ref, backup_id, ref="REQ-RS"):
    req = Request(reference=ref, status="submitted", requester="owner@x.com", request_type="restore",
                  source_reference=target_ref, restore_backup_id=backup_id)
    session.add(req)
    session.add(Approval(jira_key="INFRA-RS", status="approved", request=req))
    session.commit()
    return req


# --- Backup (owner self-service, no approval) --------------------------------

def test_backup_creates_restore_point_and_audits(client, session):
    _prov(session, "REQ-UAT")
    body = client.post("/api/requests/REQ-UAT/backup", json={"label": "before-upgrade"}).json()
    assert body["label"] == "before-upgrade" and body["id"]
    assert client.get("/api/requests/REQ-UAT/backups").json()["backups"][0]["label"] == "before-upgrade"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "backup.created")).all()


def test_backup_rejects_non_provisioned(client, session):
    session.add(Request(reference="REQ-D", status="draft", request_type="create", requester="u@x.com"))
    session.commit()
    assert client.post("/api/requests/REQ-D/backup", json={}).status_code == 400


def test_backup_rbac_non_owner_forbidden(client, session, monkeypatch):
    _prov(session, "REQ-UAT")  # owner is owner@x.com
    monkeypatch.setenv("ROLE_MAP", '{"stranger@x.com": ["requester"]}')
    r = client.post("/api/requests/REQ-UAT/backup", headers={"X-Requester": "stranger@x.com"}, json={})
    assert r.status_code == 403


# --- Restore validation ------------------------------------------------------

def test_validate_restore_valid(session):
    _prov(session, "REQ-UAT")
    b = _backup(session, "REQ-UAT")
    errs = validate_submission({"request_type": "restore", "source_reference": "REQ-UAT",
                                "restore_backup_id": b.id}, session)
    assert errs == {}


def test_validate_restore_backup_must_belong_to_target(session):
    _prov(session, "REQ-UAT")
    _prov(session, "REQ-SIT", tier="sit")
    b = _backup(session, "REQ-SIT")  # a backup of a DIFFERENT env
    errs = validate_submission({"request_type": "restore", "source_reference": "REQ-UAT",
                                "restore_backup_id": b.id}, session)
    assert "restore_backup_id" in errs


def test_validate_restore_requires_backup(session):
    _prov(session, "REQ-UAT")
    assert "restore_backup_id" in validate_submission(
        {"request_type": "restore", "source_reference": "REQ-UAT"}, session)


# --- Restore executor --------------------------------------------------------

def test_restore_succeeds_verified_and_audits(session, monkeypatch):
    _prov(session, "REQ-UAT")
    b = _backup(session, "REQ-UAT")
    req = _restore_req(session, "REQ-UAT", b.id)
    _mock_restore(monkeypatch)
    out = main._restore(session, req, actor="approver")
    assert out["restored"] is True and out["verified"] is True
    assert session.scalar(select(Request).where(Request.reference == "REQ-RS")).status == "restored"
    assert session.scalar(select(Request).where(Request.reference == "REQ-UAT")).status == "provisioned"
    assert "restore.performed" in [e.event for e in session.scalars(select(AuditLog).where(AuditLog.reference == "REQ-RS"))]


def test_restore_mismatched_backup_fails(session, monkeypatch):
    _prov(session, "REQ-UAT")
    _prov(session, "REQ-SIT", tier="sit")
    b = _backup(session, "REQ-SIT")
    req = _restore_req(session, "REQ-UAT", b.id)
    _mock_restore(monkeypatch)
    out = main._restore(session, req, actor="approver")
    assert out["restored"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-RS")).status == "restore-failed"


def test_restore_orchestrator_failure(session, monkeypatch):
    _prov(session, "REQ-UAT")
    b = _backup(session, "REQ-UAT")
    req = _restore_req(session, "REQ-UAT", b.id)
    _mock_restore(monkeypatch, ok=False)
    out = main._restore(session, req, actor="approver")
    assert out["restored"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-RS")).status == "restore-failed"
