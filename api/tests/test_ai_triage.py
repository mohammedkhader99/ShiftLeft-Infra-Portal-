"""AI failure triage checks (F-RPT-08).

Triage reads a request's real failure signals (status, status_detail, failure
audit events) and returns a plain-English diagnosis + next steps. Covers the
mock classifier, the endpoint (RBAC + audit), and the live path (SDK mocked).
"""

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_triage
from api.audit import append_audit
from api.main import app, get_session
from db.models import AuditLog, Request
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


def _make_request(session, reference, status, status_detail=None, events=()):
    req = Request(
        reference=reference,
        status=status,
        requester="req@emaratechg.ae",
        request_type="create",
        status_detail=status_detail,
    )
    session.add(req)
    for event, detail in events:
        append_audit(session, event, reference=reference, detail=detail)
    session.commit()
    return req


# --- Mock classifier ---------------------------------------------------------

def test_triage_diagnoses_a_scan_block(session):
    req = _make_request(
        session, "REQ-T-1", "apply-failed",
        status_detail="IaC scan: 2 high-severity finding(s) in the plan.",
        events=[("scan.findings", {"high": 2, "findings": ["public bucket", "no CMK"]})],
    )
    out = ai_triage.triage_request(req, session)
    assert out["mode"] == "mock" and out["failing"] is True
    text = " ".join(out["likely_causes"] + out["next_steps"]).lower()
    assert "security scan" in text and ("waiver" in text or "evidence" in text)
    assert out["signals"] and out["signals"][0]["event"] == "scan.findings"


def test_triage_diagnoses_orchestrator_error(session):
    req = _make_request(
        session, "REQ-T-2", "apply-failed",
        status_detail="Orchestrator unreachable after retries — not provisioned.",
        events=[("apply.failed", {"error": "could not reach orchestrator: connection refused"})],
    )
    out = ai_triage.triage_request(req, session)
    assert "orchestrator could not be reached" in " ".join(out["likely_causes"]).lower()


def test_triage_falls_back_when_nothing_matches(session):
    req = _make_request(
        session, "REQ-T-3", "apply-failed",
        status_detail="Unexpected backend hiccup 500 while doing the thing.",
        events=[("apply.failed", {"error": "boom 500"})],
    )
    out = ai_triage.triage_request(req, session)
    assert out["failing"] is True
    assert out["likely_causes"] and out["next_steps"]  # generic guidance, not empty


def test_no_failure_detected_for_a_healthy_request(session):
    req = _make_request(session, "REQ-T-4", "provisioned", status_detail=None)
    out = ai_triage.triage_request(req, session)
    assert out["failing"] is False
    assert "no failure" in out["summary"].lower()
    assert out["likely_causes"] == [] and out["signals"] == []


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_triages_and_audits(client, session):
    _make_request(
        session, "REQ-T-5", "apply-failed",
        status_detail="IaC scan: 1 high-severity finding(s).",
        events=[("scan.findings", {"high": 1})],
    )
    resp = client.post("/api/requests/REQ-T-5/triage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["failing"] is True and body["likely_causes"]
    rows = session.scalars(select(AuditLog).where(AuditLog.event == "ai.triaged")).all()
    assert len(rows) == 1 and rows[0].reference == "REQ-T-5"


def test_endpoint_404_for_unknown_request(client):
    assert client.post("/api/requests/REQ-NOPE/triage").status_code == 404


def test_endpoint_requires_platform_admin(client, session, monkeypatch):
    _make_request(session, "REQ-T-6", "apply-failed", status_detail="whatever")
    monkeypatch.setenv("ROLE_MAP", '{"req@emaratechg.ae": ["requester"]}')
    resp = client.post("/api/requests/REQ-T-6/triage", headers={"X-Requester": "req@emaratechg.ae"})
    assert resp.status_code == 403


# --- Live adapter (Claude SDK mocked) ----------------------------------------

def _inject_fake_anthropic(monkeypatch, payload: dict):
    class FakeText:
        type = "text"

        def __init__(self, text):
            self.text = text

    class FakeResp:
        def __init__(self, content):
            self.content = content

    class FakeMessages:
        def create(self, **kwargs):
            return FakeResp([FakeText(json.dumps(payload))])

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)


def test_live_mode_uses_the_sdk_diagnosis(session, monkeypatch):
    req = _make_request(
        session, "REQ-T-7", "apply-failed",
        status_detail="Terraform provider error.",
        events=[("apply.failed", {"error": "provider config invalid"})],
    )
    _inject_fake_anthropic(monkeypatch, {
        "summary": "The apply failed on a Terraform provider misconfiguration.",
        "likely_causes": ["Invalid provider credentials."],
        "next_steps": ["Fix the provider block and re-apply."],
    })
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = ai_triage.triage_request(req, session)
    assert out["mode"] == "live"
    assert out["summary"].startswith("The apply failed")
    assert out["next_steps"] == ["Fix the provider block and re-apply."]
