"""Environment refresh checks (F-LCM-03).

Refresh copies data from a higher environment down into a lower one, masking
sensitive source data. It's a governed request type mirroring decommission:
portal validates -> Jira approves -> orchestrator executes (re-verified). The
orchestrator call is mocked so the suite stays offline; mock copies no data.
"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.validation import validate_submission
from db.models import Approval, AuditLog, Request
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def _mock_refresh(monkeypatch, ok=True):
    def _fake(body, sig, path="/provision"):
        if not ok:
            return None, "connection refused"
        masked = bool(json.loads(body).get("mask"))
        return FakeResp(200, {"refreshed": True, "masked": masked, "summary": "mock refresh"}), None
    monkeypatch.setattr(main, "_post_to_orchestrator", _fake)
    monkeypatch.setattr(main, "jira_mode", lambda: "mock")
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)


def _prov_env(session, ref, tier, classification="internal"):
    req = Request(reference=ref, status="provisioned", requester="u@x.com", request_type="create",
                  environment_name=ref.lower(), environment_tier=tier, data_classification=classification,
                  deployment_target="oci", project_code="EGATE", cost_centre_code="IMD-1001")
    session.add(req)
    session.commit()
    return req


def _refresh_req(session, target_ref, from_ref, ref="REQ-R"):
    req = Request(reference=ref, status="submitted", requester="u@x.com", request_type="refresh",
                  source_reference=target_ref, refresh_from_reference=from_ref)
    session.add(req)
    session.add(Approval(jira_key="INFRA-9", status="approved", request=req))
    session.commit()
    return req


def _data(target, src):
    return {"request_type": "refresh", "source_reference": target, "refresh_from_reference": src}


# --- Guardrails --------------------------------------------------------------

def test_valid_refresh_uat_from_prod(session):
    _prov_env(session, "REQ-UAT", "uat")
    _prov_env(session, "REQ-PROD", "prod")
    assert validate_submission(_data("REQ-UAT", "REQ-PROD"), session) == {}


def test_rejects_prod_target(session):
    _prov_env(session, "REQ-PROD", "prod")
    _prov_env(session, "REQ-DR", "dr")
    assert "source_reference" in validate_submission(_data("REQ-PROD", "REQ-DR"), session)


def test_rejects_source_lower_than_target(session):
    _prov_env(session, "REQ-UAT", "uat")
    _prov_env(session, "REQ-DEV", "dev")
    assert "refresh_from_reference" in validate_submission(_data("REQ-UAT", "REQ-DEV"), session)


def test_rejects_same_environment(session):
    _prov_env(session, "REQ-UAT", "uat")
    assert "refresh_from_reference" in validate_submission(_data("REQ-UAT", "REQ-UAT"), session)


def test_target_must_be_provisioned(session):
    session.add(Request(reference="REQ-D", status="draft", request_type="create",
                        requester="u@x.com", environment_tier="uat"))
    _prov_env(session, "REQ-PROD", "prod")
    session.commit()
    assert "source_reference" in validate_submission(_data("REQ-D", "REQ-PROD"), session)


def test_missing_source(session):
    _prov_env(session, "REQ-UAT", "uat")
    assert "refresh_from_reference" in validate_submission(
        {"request_type": "refresh", "source_reference": "REQ-UAT"}, session)


# --- Executor ----------------------------------------------------------------

def test_refresh_succeeds_and_audits(session, monkeypatch):
    _prov_env(session, "REQ-UAT", "uat")
    _prov_env(session, "REQ-PROD", "prod")
    req = _refresh_req(session, "REQ-UAT", "REQ-PROD")
    _mock_refresh(monkeypatch)
    out = main._refresh(session, req, actor="approver")
    assert out["refreshed"] is True and out["masked"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-R")).status == "refreshed"
    # the target is refreshed, not consumed — it stays provisioned
    assert session.scalar(select(Request).where(Request.reference == "REQ-UAT")).status == "provisioned"
    assert "refresh.performed" in [e.event for e in session.scalars(select(AuditLog).where(AuditLog.reference == "REQ-R"))]


def test_refresh_masks_sensitive_source(session, monkeypatch):
    _prov_env(session, "REQ-UAT", "uat")
    _prov_env(session, "REQ-PROD", "prod", classification="restricted")
    req = _refresh_req(session, "REQ-UAT", "REQ-PROD")
    _mock_refresh(monkeypatch)
    assert main._refresh(session, req, actor="approver")["masked"] is True


def test_refresh_orchestrator_failure_marks_failed(session, monkeypatch):
    _prov_env(session, "REQ-UAT", "uat")
    _prov_env(session, "REQ-PROD", "prod")
    req = _refresh_req(session, "REQ-UAT", "REQ-PROD")
    _mock_refresh(monkeypatch, ok=False)
    out = main._refresh(session, req, actor="approver")
    assert out["refreshed"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-R")).status == "refresh-failed"


def test_refresh_missing_target(session, monkeypatch):
    _prov_env(session, "REQ-PROD", "prod")
    req = _refresh_req(session, "REQ-GONE", "REQ-PROD")
    _mock_refresh(monkeypatch)
    out = main._refresh(session, req, actor="approver")
    assert out["refreshed"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-R")).status == "refresh-failed"
