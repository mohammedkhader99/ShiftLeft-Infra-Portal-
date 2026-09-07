"""Sandbox + Temporary request types — short-lived environments (catalogue).

Both are create-flavoured NON-PROD environments that self-expire: a sandbox after
a short policy default, a temporary on a user-chosen date. Validated like create,
charged + quota'd as new environments, and their TTL is wired via _ttl_for.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import validation
from api.main import app, get_policy_evaluator, get_session
from db.models import Request
from db.seed import seed
from db.session import Base

_FUTURE = (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()
_PAST = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
_META = {
    "business_justification": "Short-lived environment for a UAT rehearsal exercise.",
    "priority": "medium",
    "business_criticality": "tier3",
    "required_delivery_date": _FUTURE,
}


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

    # Default: policy allows (real OPA is exercised in Rego tests + live checks).
    # Without this, the submit gate calls the OPA server at OPA_URL, so a test that
    # reaches it passes only on a machine that happens to be running one.
    def allow_everything():
        return lambda data: {"allow": True, "violations": []}

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_policy_evaluator] = allow_everything
    yield TestClient(app)
    app.dependency_overrides.clear()


def _payload(request_type, **over):
    p = {
        "request_type": request_type,
        "project_code": "EGATE",
        "cost_centre_code": "IMD-1001",
        "deployment_target": "onprem",
        "environment_name": "egate-sbx",
        "environment_tier": "dev",
        "data_classification": "internal",
        "components": [{"technology_code": "postgres16", "size": "small"}],
        **_META,
    }
    p.update(over)
    return p


# --- Validation --------------------------------------------------------------

def test_sandbox_valid(session):
    assert validation.validate_submission(_payload("sandbox"), session) == {}


def test_sandbox_rejects_prod_tier(session):
    errors = validation.validate_submission(_payload("sandbox", environment_tier="prod"), session)
    assert "non-production" in errors["environment_tier"]


def test_temporary_valid(session):
    assert validation.validate_submission(_payload("temporary", expires_on=_FUTURE), session) == {}


def test_temporary_requires_future_expiry(session):
    assert "expires_on" in validation.validate_submission(_payload("temporary"), session)
    assert "expires_on" in validation.validate_submission(_payload("temporary", expires_on=_PAST), session)


def test_temporary_rejects_prod_tier(session):
    errors = validation.validate_submission(
        _payload("temporary", expires_on=_FUTURE, environment_tier="prod"), session)
    assert "environment_tier" in errors


# --- TTL wiring --------------------------------------------------------------

def test_ttl_for_sandbox_is_short(monkeypatch):
    monkeypatch.setenv("TTL_DAYS_SANDBOX", "5")
    exp = main._ttl_for(Request(request_type="sandbox", environment_tier="dev"))
    days = (exp - datetime.now(timezone.utc)).days
    assert 4 <= days <= 5


def test_ttl_for_temporary_uses_chosen_date():
    when = date(2027, 6, 1)
    exp = main._ttl_for(Request(request_type="temporary", environment_tier="dev", expires_on=when))
    assert exp.date() == when


def test_ttl_for_prod_never_expires():
    assert main._ttl_for(Request(request_type="create", environment_tier="prod")) is None


# --- Environment-creating (quota) --------------------------------------------

def test_shortlived_count_as_environments(session):
    for i, rtype in enumerate(("sandbox", "temporary")):
        session.add(Request(reference=f"REQ-{i}", requester="a@x.com", request_type=rtype,
                            status="provisioned", project_code="EGATE", environment_name=f"e{i}"))
    session.commit()
    assert main._environment_count(session, "EGATE") == 2


# --- End-to-end --------------------------------------------------------------

def test_sandbox_draft_and_submit(client, session):
    draft = client.post("/api/requests/draft", json=_payload("sandbox"))
    assert draft.status_code == 200
    ref = draft.json()["reference"]
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200
    row = session.scalar(main.select(Request).where(Request.reference == ref))
    assert row.request_type == "sandbox"
