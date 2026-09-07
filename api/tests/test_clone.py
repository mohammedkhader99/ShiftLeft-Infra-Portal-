"""Clone request type (catalogue expansion).

Clone provisions a NEW environment mirroring an existing provisioned one: the
same create-field rules for the new environment, a valid provisioned source to
copy from, and it's charged + quota'd like a create. Validated authoritatively
server-side.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import validation
from api.main import app, get_policy_evaluator, get_session
from db.models import Request, RequestComponent
from db.seed import seed
from db.session import Base

_META = {
    "business_justification": "Clone of the prod stack for a UAT rehearsal environment.",
    "priority": "medium",
    "business_criticality": "tier3",
    "required_delivery_date": "2027-06-01",
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


def _source(session, *, ref="REQ-SRC", status="provisioned", env="egate-prod"):
    req = Request(reference=ref, requester="alice@x.com", request_type="create", status=status,
                  project_code="EGATE", environment_name=env, environment_tier="prod",
                  deployment_target="onprem", data_classification="internal")
    req.components = [RequestComponent(technology_code="postgres16", size="medium")]
    session.add(req)
    session.commit()
    return req


def _clone(**over):
    p = {
        "request_type": "clone",
        "source_reference": "REQ-SRC",
        "project_code": "EGATE",
        "cost_centre_code": "IMD-1001",
        "deployment_target": "onprem",
        "environment_name": "egate-clone",
        "environment_tier": "uat",
        "data_classification": "internal",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
        **_META,
    }
    p.update(over)
    return p


# --- Validation --------------------------------------------------------------

def test_valid_clone_passes(session):
    _source(session)
    assert validation.validate_submission(_clone(), session) == {}


def test_clone_requires_source(session):
    errors = validation.validate_submission(_clone(source_reference=""), session)
    assert "source_reference" in errors


def test_clone_source_must_be_provisioned(session):
    _source(session, status="draft")
    errors = validation.validate_submission(_clone(), session)
    assert "not provisioned" in errors["source_reference"]


def test_clone_needs_create_fields(session):
    _source(session)
    errors = validation.validate_submission(_clone(environment_name=""), session)
    assert "environment_name" in errors  # a clone needs a name for the new env


def test_clone_needs_components(session):
    _source(session)
    errors = validation.validate_submission(_clone(components=[]), session)
    assert "components" in errors


def test_clone_name_must_differ_from_source(session):
    _source(session, env="egate-prod")
    errors = validation.validate_submission(_clone(environment_name="egate-prod"), session)
    assert "environment_name" in errors


# --- Quota / environment-creating --------------------------------------------

def test_clone_counts_as_environment(session):
    _source(session, ref="REQ-SRC", env="egate-prod")
    # A provisioned clone in the same project counts toward the project's env total.
    c = Request(reference="REQ-CLONE", requester="alice@x.com", request_type="clone",
                status="provisioned", project_code="EGATE", environment_name="egate-clone")
    session.add(c)
    session.commit()
    assert main._environment_count(session, "EGATE") == 2  # source (create) + clone


# --- End-to-end: draft + submit flows the create path ------------------------

def test_clone_draft_and_submit(client, session):
    _source(session)
    draft = client.post("/api/requests/draft", json=_clone())
    assert draft.status_code == 200
    ref = draft.json()["reference"]
    submit = client.post(f"/api/requests/{ref}/submit")
    assert submit.status_code == 200  # not 422 — validates + flows like a create
    row = session.scalar(main.select(Request).where(Request.reference == ref))
    assert row.request_type == "clone" and row.source_reference == "REQ-SRC"
    assert [c.technology_code for c in row.components] == ["postgres16"]
