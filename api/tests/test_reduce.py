"""Reduce capacity checks (F-CAT) — scale a provisioned env's components DOWN.

A governed request type mirroring decommission/refresh: the portal validates
(reduce-only) -> Jira approves -> the orchestrator resizes (re-verified). The
orchestrator call is mocked so the suite stays offline; mock resizes nothing.
"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.validation import validate_submission
from db.models import Approval, AuditLog, Request, RequestComponent
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


def _mock_reduce(monkeypatch, ok=True):
    def _fake(body, sig, path="/provision"):
        if not ok:
            return None, "connection refused"
        return FakeResp(200, {"reduced": True, "summary": "mock reduce"}), None
    monkeypatch.setattr(main, "_post_to_orchestrator", _fake)
    monkeypatch.setattr(main, "jira_mode", lambda: "mock")
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)


def _prov(session, ref="REQ-SRC", *, comps=(("postgres16", "large"), ("redis7", "medium"))):
    req = Request(reference=ref, status="provisioned", requester="u@x.com", request_type="create",
                  environment_name=ref.lower(), environment_tier="uat", deployment_target="oci",
                  project_code="EGATE", cost_centre_code="IMD-1001")
    req.components = [RequestComponent(technology_code=t, size=s) for t, s in comps]
    session.add(req)
    session.commit()
    return req


def _data(**comps):
    return {"request_type": "reduce", "source_reference": "REQ-SRC",
            "components": [{"technology_code": t, "size": s} for t, s in comps.items()]}


# --- Validation (reduce-only guardrail) --------------------------------------

def test_valid_reduce(session):
    _prov(session)  # postgres16 is 'large'
    assert validate_submission(_data(postgres16="medium"), session) == {}


def test_requires_source(session):
    data = {"request_type": "reduce", "components": [{"technology_code": "postgres16", "size": "small"}]}
    assert "source_reference" in validate_submission(data, session)


def test_source_must_be_provisioned(session):
    session.add(Request(reference="REQ-SRC", status="draft", request_type="create", requester="u@x.com"))
    session.commit()
    assert "not provisioned" in validate_submission(_data(postgres16="small"), session)["source_reference"]


def test_component_must_be_in_stack(session):
    _prov(session)
    assert "components" in validate_submission(_data(mongodb="small"), session)  # not in the source's stack


def test_size_must_be_strictly_smaller(session):
    _prov(session)  # postgres16 is 'large'
    assert "components" in validate_submission(_data(postgres16="large"), session)   # equal -> reject
    assert "components" in validate_submission(_data(postgres16="xlarge"), session)  # larger -> reject


def test_needs_at_least_one_component(session):
    _prov(session)
    data = {"request_type": "reduce", "source_reference": "REQ-SRC", "components": []}
    assert "components" in validate_submission(data, session)


# --- Executor ----------------------------------------------------------------

def _reduce_req(session, ref="REQ-RED", *, reductions=(("postgres16", "medium"),)):
    req = Request(reference=ref, status="submitted", requester="u@x.com", request_type="reduce",
                  source_reference="REQ-SRC")
    req.components = [RequestComponent(technology_code=t, size=s) for t, s in reductions]
    session.add(req)
    session.add(Approval(jira_key="INFRA-9", status="approved", request=req))
    session.commit()
    return req


def test_reduce_succeeds_and_audits(session, monkeypatch):
    _prov(session)
    req = _reduce_req(session)
    _mock_reduce(monkeypatch)
    out = main._reduce(session, req, actor="approver")
    assert out["reduced"] is True
    assert session.scalar(select(Request).where(Request.reference == "REQ-RED")).status == "reduced"
    # The source stays provisioned (mock resizes nothing).
    assert session.scalar(select(Request).where(Request.reference == "REQ-SRC")).status == "provisioned"
    events = [e.event for e in session.scalars(select(AuditLog).where(AuditLog.reference == "REQ-RED"))]
    assert "capacity.reduced" in events


def test_reduce_records_from_and_to(session, monkeypatch):
    _prov(session)
    req = _reduce_req(session, reductions=(("postgres16", "small"),))
    _mock_reduce(monkeypatch)
    red = main._reduce(session, req, actor="approver")["reductions"][0]
    assert red["technology"] == "postgres16" and red["from"] == "large" and red["to"] == "small"


def test_reduce_orchestrator_failure_marks_failed(session, monkeypatch):
    _prov(session)
    req = _reduce_req(session)
    _mock_reduce(monkeypatch, ok=False)
    assert main._reduce(session, req, actor="approver")["reduced"] is False
    assert session.scalar(select(Request).where(Request.reference == "REQ-RED")).status == "reduce-failed"
