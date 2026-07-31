"""ChatOps approvals bot checks (F-INT-08).

The bot runs as the mapped human's identity: queries are read-only; approve/
reject enforce RBAC + segregation of duties and record the decision (mock mode
stands in for the Jira transition). Slack requests are signature-verified.
All offline.
"""

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import chatbot
from api.main import app, get_session
from db.models import Approval, AuditLog, Request
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


def _make_awaiting(session, reference="REQ-C-1", requester="alice@emaratechg.ae", jira="INFRA-1001"):
    req = Request(reference=reference, status="submitted", requester=requester, request_type="create",
                  environment_name="egate-uat")
    session.add(req)
    session.add(Approval(jira_key=jira, status="pending", request=req))
    session.commit()
    return req


# --- Query commands ----------------------------------------------------------

def test_help_and_unknown(session):
    assert "approve" in chatbot.handle_command("help", "u@x.com", session)["response"]
    assert chatbot.handle_command("frobnicate", "u@x.com", session)["ok"] is False


def test_status_and_pending(session):
    _make_awaiting(session)
    status = chatbot.handle_command("status REQ-C-1", "u@emaratechg.ae", session)
    assert "REQ-C-1" in status["response"] and status["status"] == "submitted"
    pend = chatbot.handle_command("pending", "u@emaratechg.ae", session)
    assert pend["count"] == 1 and "REQ-C-1" in pend["response"]
    assert chatbot.handle_command("status REQ-NOPE", "u@x.com", session)["ok"] is False


# --- Approve / reject --------------------------------------------------------

def test_approver_can_approve_and_it_is_audited(session):
    _make_awaiting(session)
    out = chatbot.handle_command("approve REQ-C-1 looks good", "bob@emaratechg.ae", session)
    assert out["ok"] and out["action"] == "approved"
    appr = session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001"))
    assert appr.status == "approved"
    rows = session.scalars(select(AuditLog).where(AuditLog.event == "chatops.approved")).all()
    assert len(rows) == 1 and rows[0].actor == "bob@emaratechg.ae" and rows[0].detail["note"] == "looks good"


def test_reject_records_the_decision(session):
    _make_awaiting(session)
    out = chatbot.handle_command("reject REQ-C-1", "bob@emaratechg.ae", session)
    assert out["ok"] and out["action"] == "rejected"
    assert session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001")).status == "rejected"


def test_non_approver_is_refused(session, monkeypatch):
    _make_awaiting(session)
    monkeypatch.setenv("ROLE_MAP", '{"ro@emaratechg.ae": ["read_only"]}')
    out = chatbot.handle_command("approve REQ-C-1", "ro@emaratechg.ae", session)
    assert out["ok"] is False and "approver role" in out["response"]
    # unchanged
    assert session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001")).status == "pending"


def test_segregation_of_duties_blocks_self_approval(session):
    _make_awaiting(session, requester="alice@emaratechg.ae")
    out = chatbot.handle_command("approve REQ-C-1", "alice@emaratechg.ae", session)
    assert out["ok"] is False and "Segregation of duties" in out["response"]
    assert session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001")).status == "pending"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "sod.blocked")).all()


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_runs_as_the_signed_in_user(client, session):
    _make_awaiting(session)
    r = client.post("/api/chatops", headers={"X-Requester": "carol@emaratechg.ae"},
                    json={"command": "approve REQ-C-1"})
    assert r.status_code == 200 and r.json()["action"] == "approved"


# --- Slack adapter -----------------------------------------------------------

def _sign(secret, body, ts):
    base = f"v0:{ts}:{body}".encode()
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_verify_slack(monkeypatch):
    body = "text=pending&user_id=U1"
    ts = str(int(time.time()))
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")
    good = {"x-slack-request-timestamp": ts, "x-slack-signature": _sign("shhh", body, ts)}
    assert chatbot.verify_slack(body.encode(), good) is True
    bad = {"x-slack-request-timestamp": ts, "x-slack-signature": "v0=deadbeef"}
    assert chatbot.verify_slack(body.encode(), bad) is False
    # No secret configured → rejected.
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    assert chatbot.verify_slack(body.encode(), good) is False


def test_slack_endpoint_verifies_and_maps_user(client, session, monkeypatch):
    _make_awaiting(session)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")
    monkeypatch.setenv("CHATOPS_SLACK_MAP", '{"U1": "bob@emaratechg.ae"}')
    body = "text=status+REQ-C-1&user_id=U1&user_name=bob"
    ts = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": _sign("shhh", body, ts),
        "content-type": "application/x-www-form-urlencoded",
    }
    r = client.post("/api/chatops/slack", content=body, headers=headers)
    assert r.status_code == 200 and "REQ-C-1" in r.json()["text"]
    # A bad signature is rejected.
    bad = dict(headers, **{"x-slack-signature": "v0=nope"})
    assert client.post("/api/chatops/slack", content=body, headers=bad).status_code == 401
