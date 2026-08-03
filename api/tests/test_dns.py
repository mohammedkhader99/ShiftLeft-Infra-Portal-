"""DNS as a governed request type (GAP-ANALYSIS step 5).

Naming an environment is one of the most frequent things a requester needs an
infrastructure administrator for. A `dns` request references a PROVISIONED
environment and creates a record for it, through the normal
validate -> approve -> orchestrator path.

The record is planned into the target environment's own Terraform workspace, so
it shares that environment's lifecycle rather than outliving it.

HONEST LIMIT: these tests prove validation, routing, the handoff and the
refusals. No real DNS record has been created — that needs a real zone.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from api.validation import validate_submission
from db.models import Request
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


def _provisioned(session, reference="REQ-2026-0042", status="provisioned"):
    req = Request(reference=reference, status=status, requester="u@x.com",
                  request_type="create", environment_name="egate-uat",
                  deployment_target="oci")
    session.add(req)
    session.commit()
    return req


def _dns_request(**over):
    return {"request_type": "dns", "source_reference": "REQ-2026-0042",
            "dns_name": "egate-uat", "dns_type": "A", **over}


# --- Validation --------------------------------------------------------------

def test_valid_dns_request(session):
    _provisioned(session)
    assert validate_submission(_dns_request(), session) == {}


def test_target_must_exist_and_be_provisioned(session):
    assert "source_reference" in validate_submission(_dns_request(), session)
    _provisioned(session, status="submitted")
    errors = validate_submission(_dns_request(), session)
    assert "not provisioned" in errors["source_reference"]


def test_host_name_is_required_and_shape_checked(session):
    _provisioned(session)
    assert "dns_name" in validate_submission(_dns_request(dns_name=""), session)
    for bad in ("-leading", "trailing-", "has space", "under_score", "a" * 121):
        assert "dns_name" in validate_submission(_dns_request(dns_name=bad), session), bad


def test_multi_label_names_are_allowed(session):
    _provisioned(session)
    assert validate_submission(_dns_request(dns_name="api.egate"), session) == {}


def test_record_type_is_constrained(session):
    _provisioned(session)
    assert "dns_type" in validate_submission(_dns_request(dns_type="TXT"), session)
    assert validate_submission(_dns_request(dns_type="CNAME", dns_value="a.b.com"), session) == {}


def test_cname_requires_a_target(session):
    """An A record can fall back to the environment's address; a CNAME cannot."""
    _provisioned(session)
    errors = validate_submission(_dns_request(dns_type="CNAME"), session)
    assert "dns_value" in errors


def test_a_record_may_omit_the_value(session):
    """Blank value means 'point at the environment', which is the common case."""
    _provisioned(session)
    assert validate_submission(_dns_request(dns_value=""), session) == {}


def test_dns_does_not_require_cost_centre_or_components(session):
    """It operates on an existing environment, so it inherits that context."""
    _provisioned(session)
    assert validate_submission(_dns_request(), session) == {}


# --- Draft round-trip --------------------------------------------------------

def test_dns_fields_persist_through_a_draft(client, session):
    _provisioned(session)
    resp = client.post("/api/requests/draft", json=_dns_request(dns_value="10.0.0.5"))
    assert resp.status_code == 200
    ref = resp.json()["reference"]
    got = client.get(f"/api/requests/{ref}").json()
    assert got["dns_name"] == "egate-uat"
    assert got["dns_type"] == "A"
    assert got["dns_value"] == "10.0.0.5"


# --- Executor ----------------------------------------------------------------

def test_executor_builds_a_signed_handoff_naming_the_target(session):
    from api.main import _dns_handoff
    from db.models import Approval
    target = _provisioned(session)
    req = Request(reference="REQ-2026-0101", status="submitted", requester="u@x.com",
                  request_type="dns", source_reference=target.reference,
                  dns_name="egate-uat", dns_type="A")
    session.add(req)
    session.add(Approval(jira_key="SDIMD-9", status="approved", request=req))
    session.commit()

    body, signature = _dns_handoff(req, target, "oci-instance")
    import json
    payload = json.loads(body)
    assert payload["operation"] == "dns"
    assert payload["target"]["reference"] == "REQ-2026-0042"
    assert payload["target"]["resource_kind"] == "oci-instance"
    assert payload["record"] == {"name": "egate-uat", "type": "A", "value": ""}
    assert signature  # signed, so the orchestrator can verify authenticity
