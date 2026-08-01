"""Outbound webhook checks (F-INT-10).

Lifecycle events are delivered to admin-configured endpoints, HMAC-signed,
at-least-once with bounded retries. Opt-in; payloads carry no secrets. httpx is
mocked so the suite stays offline (no real outbound calls).
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import webhooks
from api.audit import append_audit
from api.main import app, get_session
from common.signing import sign
from db.models import AuditLog, WebhookDelivery, WebhookState, WebhookSubscription
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


def _mock_post(monkeypatch, status=200, capture=None):
    class Resp:
        status_code = status

    def _post(url, content=None, headers=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "headers": headers, "content": content})
        return Resp()
    monkeypatch.setattr(webhooks.httpx, "post", _post)


def _sub(session, events=None, url="https://hook.example/x", secret="topsecret1"):
    s = WebhookSubscription(url=url, secret=secret, events=events or [])
    session.add(s)
    session.commit()
    return s


def _event(session, event="provisioned", reference="REQ-1", detail=None):
    row = append_audit(session, event, reference=reference, detail=detail or {})
    session.commit()
    return row


# --- Payload safety + matching ----------------------------------------------

def test_matches_filter():
    all_sub = WebhookSubscription(url="x", secret="s", events=[])
    one = WebhookSubscription(url="x", secret="s", events=["decommissioned"])
    assert webhooks.matches(all_sub, "provisioned") is True
    assert webhooks.matches(one, "provisioned") is False and webhooks.matches(one, "decommissioned")


def test_payload_strips_sensitive_keys(session):
    row = _event(session, "access.granted", detail={"scope": "ssh", "handle": "mock-x", "link": "https://secret"})
    p = webhooks.event_payload(row)
    assert p["detail"] == {"scope": "ssh"}  # handle + link stripped
    assert "link" not in json.dumps(p) and "handle" not in json.dumps(p)


# --- Fan-out sweep -----------------------------------------------------------

def test_process_delivers_signed(session, monkeypatch):
    session.add(WebhookState(id=1, last_event_id=0))
    sub = _sub(session)  # events [] = all
    ev = _event(session, "provisioned", "REQ-9")
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    cap = []
    _mock_post(monkeypatch, 200, cap)
    webhooks.process(session)
    d = session.scalar(select(WebhookDelivery).where(WebhookDelivery.event_id == ev.id))
    assert d.status == "delivered"
    # signature is valid for the delivered body
    sent = cap[0]
    assert sent["headers"]["X-Signature"] == sign(sub.secret, sent["content"])
    assert session.scalars(select(AuditLog).where(AuditLog.event == "webhook.delivered")).all()


def test_process_skips_nonmatching_event(session, monkeypatch):
    session.add(WebhookState(id=1, last_event_id=0))
    _sub(session, events=["decommissioned"])
    _event(session, "provisioned", "REQ-9")
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    _mock_post(monkeypatch)
    webhooks.process(session)
    assert session.scalars(select(WebhookDelivery)).all() == []  # nothing enqueued


def test_process_noop_when_disabled(session, monkeypatch):
    session.add(WebhookState(id=1, last_event_id=0))
    _sub(session)
    _event(session, "provisioned", "REQ-9")
    monkeypatch.delenv("WEBHOOKS_ENABLED", raising=False)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deliver when disabled"))
    monkeypatch.setattr(webhooks.httpx, "post", boom)
    webhooks.process(session)
    assert session.scalars(select(WebhookDelivery)).all() == []


def test_process_does_not_backfill_history(session, monkeypatch):
    _event(session, "provisioned", "REQ-OLD")  # history, before any subscription
    _sub(session)
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    _mock_post(monkeypatch)
    webhooks.process(session)  # first run just sets the cursor to the tip
    assert session.scalars(select(WebhookDelivery)).all() == []


def test_process_retries_then_fails(session, monkeypatch):
    session.add(WebhookState(id=1, last_event_id=0))
    _sub(session)
    _event(session, "provisioned", "REQ-9")
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    monkeypatch.setattr(webhooks, "MAX_ATTEMPTS", 2)
    _mock_post(monkeypatch, status=500)
    webhooks.process(session)  # attempt 1 -> still pending
    d = session.scalar(select(WebhookDelivery))
    assert d.status == "pending" and d.attempts == 1
    webhooks.process(session)  # attempt 2 -> failed
    d = session.scalar(select(WebhookDelivery))
    assert d.status == "failed" and d.attempts == 2
    assert session.scalars(select(AuditLog).where(AuditLog.event == "webhook.failed")).all()


# --- Admin endpoints ---------------------------------------------------------

def test_admin_crud_and_no_secret_leak(client, session):
    body = client.post("/api/webhooks", json={"url": "https://hook.example/y",
                                              "secret": "sharedsecret", "events": ["provisioned"]}).json()
    assert body["id"] and "secret" not in body
    listed = client.get("/api/webhooks").json()
    assert listed["webhooks"][0]["url"] == "https://hook.example/y"
    assert all("secret" not in w for w in listed["webhooks"])
    assert client.delete(f"/api/webhooks/{body['id']}").json()["deleted"] == body["id"]


def test_admin_rbac_and_url_validation(client, session, monkeypatch):
    assert client.post("/api/webhooks", json={"url": "ftp://x", "secret": "sharedsecret"}).status_code == 422
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.post("/api/webhooks", headers={"X-Requester": "req@x.com"},
                    json={"url": "https://x.example", "secret": "sharedsecret"})
    assert r.status_code == 403


def test_test_send(client, session, monkeypatch):
    sub_id = client.post("/api/webhooks", json={"url": "https://hook.example/z", "secret": "sharedsecret"}).json()["id"]
    cap = []
    _mock_post(monkeypatch, 200, cap)
    body = client.post(f"/api/webhooks/{sub_id}/test").json()
    assert body["ok"] is True
    assert json.loads(cap[0]["content"])["event"] == "webhook.test"
