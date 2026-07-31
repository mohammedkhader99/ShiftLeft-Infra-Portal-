"""Lifecycle event stream checks (F-INT-02).

The feed is a curated, read-only view over the audit log: lifecycle events in,
internal noise out, tailed by a monotonic cursor. Covers the query logic, the
cursor endpoint (RBAC + filters), and the SSE frame format.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import eventstream
from api.audit import append_audit
from api.main import app, get_session
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


def _seed_events(session):
    # A mix of lifecycle events and internal noise, in order.
    append_audit(session, "provisioning.started", reference="REQ-1")
    append_audit(session, "poll.error", reference="REQ-1", detail={"error": "blip"})  # noise
    append_audit(session, "provisioned", reference="REQ-1")
    append_audit(session, "ai.drafted", actor="x")  # noise
    append_audit(session, "apply.failed", reference="REQ-2", detail={"error": "boom"})
    append_audit(session, "ttl.renewed", reference="REQ-1")
    session.commit()


# --- Query logic -------------------------------------------------------------

def test_feed_publishes_lifecycle_events_and_excludes_noise(session):
    _seed_events(session)
    events, cursor = eventstream.lifecycle_events(session)
    types = [e["type"] for e in events]
    assert "provisioned" in types and "apply.failed" in types and "ttl.renewed" in types
    assert "poll.error" not in types and "ai.drafted" not in types
    assert cursor == events[-1]["id"]  # cursor is the max id returned


def test_include_all_returns_the_noise_too(session):
    _seed_events(session)
    events, _ = eventstream.lifecycle_events(session, include_all=True)
    types = {e["type"] for e in events}
    assert "poll.error" in types and "ai.drafted" in types


def test_since_cursor_returns_only_newer(session):
    _seed_events(session)
    events, cursor = eventstream.lifecycle_events(session)
    again, cursor2 = eventstream.lifecycle_events(session, since=cursor)
    assert again == [] and cursor2 == cursor  # nothing newer
    # Add one more; only it comes back.
    append_audit(session, "drift.detected", reference="REQ-1")
    session.commit()
    more, _ = eventstream.lifecycle_events(session, since=cursor)
    assert [e["type"] for e in more] == ["drift.detected"]


def test_filter_by_reference_and_type(session):
    _seed_events(session)
    by_ref, _ = eventstream.lifecycle_events(session, reference="REQ-2")
    assert {e["type"] for e in by_ref} == {"apply.failed"}
    by_type, _ = eventstream.lifecycle_events(session, types=["ttl.renewed"])
    assert {e["type"] for e in by_type} == {"ttl.renewed"}


def test_tail_returns_the_most_recent_n_oldest_first(session):
    _seed_events(session)
    events, _ = eventstream.lifecycle_events(session, tail=2)
    # Most recent two lifecycle events are apply.failed then ttl.renewed.
    assert [e["type"] for e in events] == ["apply.failed", "ttl.renewed"]


def test_sse_frame_format():
    frame = eventstream.sse_frame({"id": 7, "type": "provisioned", "reference": "REQ-1"})
    assert frame.startswith("id: 7\nevent: provisioned\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.splitlines()[2][len("data: "):])
    assert payload["type"] == "provisioned"


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_returns_events_and_cursor(client, session):
    _seed_events(session)
    body = client.get("/api/events").json()
    assert "cursor" in body and body["events"]
    assert all(e["type"] in eventstream.LIFECYCLE_EVENTS for e in body["events"])
    # Incremental: nothing newer than the cursor.
    assert client.get(f"/api/events?since={body['cursor']}").json()["events"] == []


def test_endpoint_tail_and_reference(client, session):
    _seed_events(session)
    tail = client.get("/api/events?tail=1").json()["events"]
    assert len(tail) == 1
    scoped = client.get("/api/events?reference=REQ-2").json()["events"]
    assert {e["type"] for e in scoped} == {"apply.failed"}


def test_endpoint_requires_oversight_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"req@emaratechg.ae": ["requester"]}')
    r = client.get("/api/events", headers={"X-Requester": "req@emaratechg.ae"})
    assert r.status_code == 403  # view_overview = platform_admin / auditor / finops
