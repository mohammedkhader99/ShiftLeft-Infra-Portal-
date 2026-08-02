"""AI natural-language layer for the approvals bot (F-INT-08, AI Assistant).

Covers the offline interpreter (plain English → one safe command), the
catalogue-of-commands safety net, the endpoint (read-only intents run; approve/
reject are proposed but NEVER executed without a confirm; audit), and the live
Claude adapter with the SDK mocked so the suite stays offline.
"""

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_chat
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


def _make_awaiting(session, reference="REQ-2026-0001", requester="alice@emaratechg.ae", jira="INFRA-1001"):
    req = Request(reference=reference, status="submitted", requester=requester,
                  request_type="create", environment_name="egate-uat")
    session.add(req)
    session.add(Approval(jira_key=jira, status="pending", request=req))
    session.commit()
    return req


# --- Mock interpreter --------------------------------------------------------

def test_interpret_pending():
    out = ai_chat.interpret("what's waiting on me right now?")
    assert out["mode"] == "mock" and out["action"] == "pending"


def test_interpret_status_from_phrase_and_bare_reference():
    assert ai_chat.interpret("show me REQ-2026-0001")["action"] == "status"
    bare = ai_chat.interpret("REQ-2026-0001")
    assert bare["action"] == "status" and bare["reference"] == "REQ-2026-0001"


def test_interpret_approve_with_note():
    out = ai_chat.interpret("approve REQ-2026-0001, looks good to me")
    assert out["action"] == "approve"
    assert out["reference"] == "REQ-2026-0001"
    assert out["note"] == "looks good to me"


def test_interpret_reject_including_do_not_approve():
    assert ai_chat.interpret("please reject REQ-2026-0001")["action"] == "reject"
    # "do not approve" must read as reject, not approve.
    assert ai_chat.interpret("do not approve REQ-2026-0001")["action"] == "reject"


def test_interpret_help_and_unknown():
    assert ai_chat.interpret("what can you do?")["action"] == "help"
    assert ai_chat.interpret("hello there")["action"] == "unknown"


def test_interpret_portal_question_is_explain():
    # A question about the portal (no request id, not a command) → explain.
    assert ai_chat.interpret("what does the reduce capacity page do?")["action"] == "explain"
    assert ai_chat.interpret("what is the purpose of the admin page?")["action"] == "explain"


def test_interpret_normalizes_loose_reference():
    assert ai_chat.interpret("status req 2026 1")["reference"] == "REQ-2026-0001"


def test_decide_without_reference_is_downgraded():
    # An approve/reject with no request id must not become a proposable decision.
    out = ai_chat.interpret("approve it")
    assert out["action"] == "unknown"
    assert any("request id" in w.lower() for w in out["warnings"])


# --- Endpoint: read-only intents run ----------------------------------------

def test_endpoint_help(client):
    r = client.post("/api/ai/chat", json={"message": "what can you do?"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "help" and body["needs_confirmation"] is False
    assert "approve" in body["response"]


def test_endpoint_pending_runs_the_engine(client, session):
    _make_awaiting(session)
    r = client.post("/api/ai/chat", json={"message": "anything pending for me?"})
    body = r.json()
    assert body["action"] == "pending" and body["needs_confirmation"] is False
    assert "REQ-2026-0001" in body["response"]


def test_endpoint_status_runs_the_engine(client, session):
    _make_awaiting(session)
    r = client.post("/api/ai/chat", json={"message": "how's REQ-2026-0001 doing?"})
    body = r.json()
    assert body["action"] == "status"
    assert "REQ-2026-0001" in body["response"] and body["needs_confirmation"] is False


# --- Endpoint: approve/reject are proposed, never executed -------------------

def test_endpoint_approve_asks_to_confirm_and_does_not_act(client, session):
    _make_awaiting(session, requester="alice@emaratechg.ae")
    r = client.post(
        "/api/ai/chat",
        headers={"X-Requester": "bob@emaratechg.ae"},
        json={"message": "approve REQ-2026-0001, all good"},
    )
    body = r.json()
    # It proposes the exact command but does NOT run it.
    assert body["needs_confirmation"] is True
    assert body["command"] == "approve REQ-2026-0001 all good"
    # The decision was NOT recorded: no chatops audit, approval still pending.
    assert session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001")).status == "pending"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "chatops.approved")).all() == []


def test_endpoint_reject_also_only_proposes(client, session):
    _make_awaiting(session)
    r = client.post("/api/ai/chat", json={"message": "reject REQ-2026-0001"})
    body = r.json()
    assert body["needs_confirmation"] is True and body["command"] == "reject REQ-2026-0001"
    assert session.scalar(select(Approval).where(Approval.jira_key == "INFRA-1001")).status == "pending"


def test_endpoint_explains_a_portal_feature(client):
    r = client.post("/api/ai/chat", json={"message": "what does the reduce capacity request do?"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "explain" and body["needs_confirmation"] is False
    assert "lowers the size" in body["response"]
    assert body["topic"] == "Reduce capacity request"


def test_endpoint_unknown_question_offers_the_topic_overview(client):
    r = client.post("/api/ai/chat", json={"message": "what is the meaning of life?"})
    body = r.json()
    assert body["needs_confirmation"] is False
    # Falls back to the catalogue of things it can explain.
    assert "Request types" in body["response"]


def test_endpoint_help_mentions_asking_about_pages(client):
    body = client.post("/api/ai/chat", json={"message": "what can you do?"}).json()
    assert body["action"] == "help"
    assert "page or feature" in body["response"]


def test_endpoint_audits_the_interpretation(client, session):
    client.post("/api/ai/chat", json={"message": "what's pending?"})
    rows = session.scalars(select(AuditLog).where(AuditLog.event == "ai.chat")).all()
    assert len(rows) == 1
    assert rows[0].detail["action"] == "pending" and rows[0].detail["mode"] == "mock"


def test_endpoint_rejects_too_short(client):
    assert client.post("/api/ai/chat", json={"message": "a"}).status_code == 422


# --- Live adapter (Claude SDK mocked) ---------------------------------------

def _inject_fake_anthropic(monkeypatch, payload: dict, captured: dict | None = None):
    class FakeText:
        type = "text"

        def __init__(self, text):
            self.text = text

    class FakeResp:
        def __init__(self, content):
            self.content = content

    class FakeMessages:
        def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return FakeResp([FakeText(json.dumps(payload))])

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)


def test_live_mode_maps_message_to_a_command(monkeypatch):
    captured: dict = {}
    _inject_fake_anthropic(monkeypatch, {
        "action": "approve", "reference": "req-2026-1", "note": "ok",
    }, captured)
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = ai_chat.interpret("approve the egate request")
    assert out["mode"] == "live" and out["action"] == "approve"
    assert out["reference"] == "REQ-2026-0001"  # normalised from the model's loose form
    assert out["note"] == "ok"
    assert "format" in captured["output_config"]


def test_live_mode_without_key_is_unavailable(monkeypatch):
    _inject_fake_anthropic(monkeypatch, {})
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ai_chat.AiUnavailable):
        ai_chat.interpret("approve REQ-2026-0001")
