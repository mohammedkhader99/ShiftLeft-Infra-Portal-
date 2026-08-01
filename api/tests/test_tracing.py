"""Distributed tracing checks (F-OPS-04): a correlation trace id propagated across
the stack and stamped on every audit entry, kept out of the tamper-evident hash.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import tracing
from api.audit import append_audit, verify_chain
from api.main import app, get_session
from db.models import AuditLog
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


# --- Trace-id context --------------------------------------------------------

def test_start_trace_honours_valid_incoming():
    assert tracing.start_trace("req-abc_123") == "req-abc_123"


def test_start_trace_mints_when_absent_or_bad():
    a = tracing.start_trace(None)
    b = tracing.start_trace("bad id with spaces!")
    assert len(a) == 32 and len(b) == 32 and a != b


# --- Middleware --------------------------------------------------------------

def test_middleware_echoes_incoming_trace():
    r = TestClient(app).get("/health", headers={"X-Trace-Id": "abc123"})
    assert r.headers["X-Trace-Id"] == "abc123"


def test_middleware_mints_and_echoes_trace():
    tid = TestClient(app).get("/health").headers["X-Trace-Id"]
    assert len(tid) == 32 and all(c in "0123456789abcdef" for c in tid)


# --- Audit stamping + query --------------------------------------------------

def test_append_audit_stamps_current_trace(session):
    tracing.set_trace_id("trace-xyz")
    row = append_audit(session, "provisioned", reference="REQ-1")
    session.commit()
    assert row.trace_id == "trace-xyz"


def test_hash_chain_unaffected_by_trace(session):
    tracing.set_trace_id("trace-1")
    append_audit(session, "e1", reference="R")
    append_audit(session, "e2", reference="R")
    session.commit()
    assert verify_chain(session)["ok"] is True


def test_traces_endpoint_returns_steps_in_order(client, session):
    tracing.set_trace_id("trace-follow")
    append_audit(session, "approval.approved", reference="REQ-9")
    append_audit(session, "orchestrator.handoff", reference="REQ-9")
    append_audit(session, "provisioned", reference="REQ-9")
    session.commit()
    body = client.get("/api/traces/trace-follow").json()
    assert body["count"] == 3
    assert [s["event"] for s in body["steps"]] == ["approval.approved", "orchestrator.handoff", "provisioned"]


# --- Propagation to the orchestrator + poller --------------------------------

def test_post_to_orchestrator_forwards_trace(monkeypatch):
    cap = {}

    class Resp:
        status_code = 200
        text = ""

    def _post(url, content=None, headers=None, timeout=None):
        cap["headers"] = headers
        return Resp()
    monkeypatch.setattr(main.httpx, "post", _post)
    main.set_trace_id("hop-trace")
    main._post_to_orchestrator(b"{}", "sig", path="/state")
    assert cap["headers"]["X-Trace-Id"] == "hop-trace"


def test_advance_request_sets_fresh_trace(session):
    from db.models import Request
    tracing.set_trace_id("stale")
    req = Request(reference="REQ-D", status="draft", request_type="create", requester="u@x.com")
    session.add(req)
    session.commit()
    main._advance_request(session, req)  # returns early (no approval) but sets a trace first
    assert tracing.current_trace_id() not in (None, "stale") and len(tracing.current_trace_id()) == 32
