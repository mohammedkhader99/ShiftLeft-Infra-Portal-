"""Compute (VM) resource type checks.

A request whose technology is compute (RHEL/Windows/compute-vm) provisions a
stoppable oci-instance instead of an object-storage bucket, so the control plane
can stop/start it. Covers the catalogue classification, the resource-kind
derivation, the handoff, and mock registration (which makes it demoable offline).
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from db.models import Approval, ProvisionedResource, Request, RequestComponent, Technology
from db.seed import seed
from db.session import Base


@pytest.fixture()
def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
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
    def override():
        yield _db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


class FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


# --- Catalogue classification ------------------------------------------------

def test_compute_technologies_classified_as_instance(session):
    kinds = {t.code: t.resource_kind for t in session.scalars(select(Technology))}
    assert kinds["compute-vm"] == "oci-instance"
    assert kinds["rhel9"] == "oci-instance"
    assert kinds["win2019"] == "oci-instance"
    assert kinds["kafka"] == "oci-bucket"  # a service, not a stoppable VM
    # Postgres is delivered as a managed database system, not a VM or a bucket.
    assert kinds["postgres16"] == "oci-postgres"


# --- Resource-kind derivation ------------------------------------------------

def _req(session, components, reference="REQ-C"):
    req = Request(reference=reference, status="submitted", requester="u@x.com",
                  request_type="create", environment_name="env")
    req.components = [RequestComponent(technology_code=c, size="medium") for c in components]
    session.add(req)
    session.commit()
    return req


def test_derivation_compute_component(session):
    req = _req(session, ["compute-vm"])
    assert main._environment_resource_kind(session, req) == "oci-instance"


def test_derivation_storage_only(session):
    # A service with no dedicated module still falls back to a placeholder bucket.
    req = _req(session, ["kafka"])
    assert main._environment_resource_kind(session, req) == "oci-bucket"


def test_derivation_mixed_prefers_instance(session):
    req = _req(session, ["kafka", "rhel9"])
    assert main._environment_resource_kind(session, req) == "oci-instance"


def test_derivation_aws_target_is_bucket(session):
    # AWS provisions an S3 bucket regardless of components (AWS compute is a follow-on).
    req = _req(session, ["postgres16", "rhel9"])
    req.deployment_target = "aws"
    session.commit()
    assert main._environment_resource_kind(session, req) == "aws-bucket"


# --- Handoff carries the kind ------------------------------------------------

def test_handoff_payload_includes_resource_kind(session):
    req = _req(session, ["compute-vm"])
    req.resource_kind = "oci-instance"
    req.approval = Approval(jira_key="INFRA-1", status="approved")
    session.commit()
    body, _sig = main._handoff_payload(req)
    assert json.loads(body)["resource_kind"] == "oci-instance"


# --- Mock provisioning registers the instance (control plane demoable) --------

def test_mock_provision_registers_instance_and_shows_power(session, monkeypatch):
    req = _req(session, ["compute-vm"])
    req.resource_kind = "oci-instance"
    req.approval = Approval(jira_key="INFRA-1", status="approved")
    session.commit()

    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "jira_mode", lambda: "mock")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (
        FakeResp({"provisioned": True,
                  "resource": {"kind": "oci-instance", "name": "env-REQ-C",
                               "region": None, "outputs": {}}}), None))

    assert main._advance_request(session, req) == "provisioned"
    res = session.scalar(select(ProvisionedResource).where(ProvisionedResource.reference == "REQ-C"))
    assert res is not None and res.kind == "oci-instance"
    assert main._power_for(session, "REQ-C") == "running"


def test_bucket_resource_has_no_power(session):
    """A bucket gets a power_state column but must not offer stop/start."""
    session.add(ProvisionedResource(reference="REQ-B", kind="oci-bucket", name="b1",
                                    lifecycle_state="active", power_state="running"))
    session.commit()
    assert main._power_for(session, "REQ-B") is None
