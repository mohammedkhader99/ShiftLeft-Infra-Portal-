"""A request must not report success for work it did not do (F-CAT-10).

REQ-2026-0094 asked for Apache and a compute VM. It built Apache, dropped the VM,
set the request to 'provisioned' and closed its Jira ticket as Resolved. Nothing
anywhere said a component had been skipped.

Two separate failures live here:

  * the stack collapsed to ONE resource kind — covered by the derivation tests;
  * components with no automated recipe at all were provisioned "successfully"
    as a placeholder bucket — covered by the partial-fulfilment tests.

Plus the retry halt: a request whose failure is permanent used to re-attempt
every poll cycle forever.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from db.models import AuditLog, Blueprint, Request, RequestComponent
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


@pytest.fixture()
def client(session):
    def override():
        yield session
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _certify(session, code, kind, ref):
    session.add(Blueprint(technology_code=code, deployment_target="oci",
                          blueprint_ref=ref, resource_kind=kind, status="certified",
                          version="1.0.0"))
    session.commit()
    from api import fulfilment
    fulfilment.invalidate_cache()


def _request(session, codes, reference="REQ-T-1"):
    req = Request(reference=reference, status="submitted", requester="t@x.com",
                  request_type="create", deployment_target="oci")
    req.components = [RequestComponent(technology_code=c, size="small") for c in codes]
    session.add(req)
    session.commit()
    return req


# --- The stack is every resource, not the alphabetically first ---------------

def test_a_stack_derives_every_certified_resource(session):
    """The bug: sorted(kinds)[0] picked 'oci-apache' over 'oci-instance' because
    'a' sorts before 'i'. The VM was dropped on alphabetical order."""
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    _certify(session, "compute-vm", "oci-instance", "oci/compute-instance")
    req = _request(session, ["apache", "compute-vm"])
    assert main._environment_resource_kinds(session, req) == ["oci-apache", "oci-instance"]


def test_the_primary_kind_is_still_the_first(session):
    """Operations that name one resource (DNS, resize) keep working."""
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    _certify(session, "compute-vm", "oci-instance", "oci/compute-instance")
    req = _request(session, ["apache", "compute-vm"])
    assert main._environment_resource_kind(session, req) == "oci-apache"


def test_a_single_component_is_unchanged(session):
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    req = _request(session, ["apache"])
    assert main._environment_resource_kinds(session, req) == ["oci-apache"]


def test_no_certified_blueprint_falls_back_to_the_legacy_derivation(session):
    req = _request(session, ["kafka"])
    kinds = main._environment_resource_kinds(session, req)
    assert len(kinds) == 1  # the legacy path yields exactly one


def test_the_handoff_carries_the_whole_stack(session):
    """The orchestrator cannot build what it is not told about."""
    import json
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    _certify(session, "compute-vm", "oci-instance", "oci/compute-instance")
    req = _request(session, ["apache", "compute-vm"])
    from db.models import Approval
    req.approval = Approval(jira_key="K-1", status="approved")
    req.resource_kind = main._environment_resource_kind(session, req)  # set at submit
    session.commit()
    body, _sig = main._handoff_payload(req)
    payload = json.loads(body)
    assert payload["resource_kinds"] == ["oci-apache", "oci-instance"]
    assert payload["resource_kind"] == "oci-apache"  # older orchestrators still work


# --- Components with no recipe are reported, not silently swapped ------------

def test_a_component_with_no_automated_recipe_is_named(session):
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    req = _request(session, ["apache", "kafka", "mongodb"])
    assert main._unautomated_components(session, req) == ["kafka", "mongodb"]


def test_a_fully_automated_stack_reports_nothing_outstanding(session):
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    req = _request(session, ["apache"])
    assert main._unautomated_components(session, req) == []
    assert main._record_unautomated(session, req, None) is None


def test_partial_fulfilment_is_audited_and_surfaced(session, monkeypatch):
    """The requester has to be told, or they wait for something nobody will build."""
    comments = []
    monkeypatch.setattr(main, "add_comment", lambda key, text: comments.append((key, text)))
    _certify(session, "apache", "oci-apache", "oci/apache-httpd")
    req = _request(session, ["apache", "kafka"])

    detail = main._record_unautomated(session, req, "K-9")
    session.commit()

    assert detail and "kafka" in detail and "manual" in detail.lower()
    events = session.scalars(
        select(AuditLog).where(AuditLog.event == "fulfilment.partial")).all()
    assert events and events[0].detail["not_automated"] == ["kafka"]
    # ...and the approver sees it on the ticket, not just in the portal.
    assert comments and "kafka" in comments[0][1]


# --- Recording every resource the orchestrator created -----------------------

def test_every_resource_in_a_response_is_recorded():
    result = {"resources": [{"kind": "oci-apache", "name": "a"},
                            {"kind": "oci-instance", "name": "b"}]}
    assert [r["kind"] for r in main._result_resources(result)] == ["oci-apache", "oci-instance"]


def test_a_single_resource_response_still_works():
    """Backward compatibility with an orchestrator that reports one resource."""
    assert main._result_resources({"resource": {"kind": "oci-bucket", "name": "b"}}) == [
        {"kind": "oci-bucket", "name": "b"}]
    assert main._result_resources({}) == []
    assert main._result_resources({"resource": None}) == []


# --- The retry halt ----------------------------------------------------------

def test_the_attempt_limit_is_a_setting_with_a_safe_default(monkeypatch):
    monkeypatch.delenv("PROVISION_MAX_ATTEMPTS", raising=False)
    assert main._max_provision_attempts() == 3
    monkeypatch.setenv("PROVISION_MAX_ATTEMPTS", "not-a-number")
    assert main._max_provision_attempts() == 3  # never zero: that would halt everything
    monkeypatch.setenv("PROVISION_MAX_ATTEMPTS", "0")
    assert main._max_provision_attempts() == 1


def test_retry_clears_the_hold(client, session):
    req = _request(session, ["apache"], reference="REQ-T-HELD")
    req.provision_attempts = 3
    req.status_detail = "Held after 3 failed attempts"
    session.commit()

    r = client.post("/api/requests/REQ-T-HELD/retry")
    assert r.status_code == 200
    session.refresh(req)
    assert req.provision_attempts == 0
    assert req.status_detail is None
    assert session.scalars(
        select(AuditLog).where(AuditLog.event == "provision.retry")).all()


def test_retrying_a_request_that_is_not_held_changes_nothing(client, session):
    _request(session, ["apache"], reference="REQ-T-OK")
    r = client.post("/api/requests/REQ-T-OK/retry")
    assert r.status_code == 200 and "not being held" in r.json()["message"]
    assert not session.scalars(
        select(AuditLog).where(AuditLog.event == "provision.retry")).all()
