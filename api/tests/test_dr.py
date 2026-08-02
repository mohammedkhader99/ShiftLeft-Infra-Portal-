"""Disaster Recovery request type (catalogue expansion).

DR provisions a prod-class REPLICA of a provisioned environment: the create-field
rules for the replica, a valid provisioned source to protect, and the tier must be
'dr'. Create-flavoured, so it flows the proven create path and is charged + quota'd.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import validation
from api.main import app, get_session
from db.models import Request, RequestComponent
from db.seed import seed
from db.session import Base

_META = {
    "business_justification": "Stand up a DR replica of the production environment.",
    "priority": "high",
    "business_criticality": "tier1",
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
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _source(session, *, ref="REQ-PROD", status="provisioned"):
    req = Request(reference=ref, requester="alice@x.com", request_type="create", status=status,
                  project_code="EGATE", environment_name="egate-prod", environment_tier="prod",
                  deployment_target="onprem", data_classification="internal")
    req.components = [RequestComponent(technology_code="postgres16", size="medium")]
    session.add(req)
    session.commit()
    return req


def _dr(**over):
    p = {
        "request_type": "dr",
        "source_reference": "REQ-PROD",
        "project_code": "EGATE",
        "cost_centre_code": "IMD-1001",
        "deployment_target": "azure",   # a different region for the DR replica
        "environment_name": "egate-dr",
        "environment_tier": "dr",
        "data_classification": "internal",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
        **_META,
    }
    p.update(over)
    return p


# --- Validation --------------------------------------------------------------

def test_valid_dr(session):
    _source(session)
    assert validation.validate_submission(_dr(), session) == {}


def test_dr_requires_source(session):
    assert "source_reference" in validation.validate_submission(_dr(source_reference=""), session)


def test_dr_source_must_be_provisioned(session):
    _source(session, status="draft")
    assert "not provisioned" in validation.validate_submission(_dr(), session)["source_reference"]


def test_dr_tier_must_be_dr(session):
    _source(session)
    assert "environment_tier" in validation.validate_submission(_dr(environment_tier="prod"), session)


def test_dr_needs_create_fields(session):
    _source(session)
    assert "environment_name" in validation.validate_submission(_dr(environment_name=""), session)


# --- Environment-creating ----------------------------------------------------

def test_dr_counts_as_environment(session):
    _source(session)
    session.add(Request(reference="REQ-DR", requester="alice@x.com", request_type="dr",
                        status="provisioned", project_code="EGATE", environment_name="egate-dr"))
    session.commit()
    assert main._environment_count(session, "EGATE") == 2  # primary (create) + DR


# --- End-to-end --------------------------------------------------------------

def test_dr_draft_and_submit(client, session):
    _source(session)
    draft = client.post("/api/requests/draft", json=_dr())
    assert draft.status_code == 200
    ref = draft.json()["reference"]
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200
    row = session.scalar(main.select(Request).where(Request.reference == ref))
    assert row.request_type == "dr" and row.source_reference == "REQ-PROD" and row.environment_tier == "dr"
