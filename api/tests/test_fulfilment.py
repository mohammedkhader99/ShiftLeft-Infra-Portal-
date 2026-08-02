"""Catalogue honesty: which technologies are genuinely automated (F-CAT).

The catalogue advertises 46 technologies; the orchestrator builds three resource
kinds. These tests pin the classifier that tells a requester the difference, and
guard it against drifting away from what the provisioner actually does.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import fulfilment
from api.main import _environment_resource_kind, app, get_session
from db.models import Request, RequestComponent, Technology
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


def _tech(session, code) -> Technology:
    return session.scalar(select(Technology).where(Technology.code == code))


# --- Automated cases ---------------------------------------------------------

def test_compute_on_oci_is_automated(session):
    for code in ("compute-vm", "rhel9", "win2019"):
        out = fulfilment.fulfilment_for(_tech(session, code), "oci")
        assert out["mode"] == "automated", code
        assert "Compute instance" in out["reason"]


def test_oci_object_storage_is_automated(session):
    out = fulfilment.fulfilment_for(_tech(session, "oci-objectstorage"), "oci")
    assert out["mode"] == "automated" and "bucket" in out["reason"]


def test_aws_s3_is_automated_with_a_credentials_caveat(session):
    out = fulfilment.fulfilment_for(_tech(session, "aws-s3"), "aws")
    assert out["mode"] == "automated"
    assert "credentials" in out["reason"]  # honest about the gate


# --- Manual cases (the majority) ---------------------------------------------

def test_postgres_on_oci_is_automated(session):
    # Step 2: requesting Postgres on OCI now provisions a real managed database.
    out = fulfilment.fulfilment_for(_tech(session, "postgres16"), "oci")
    assert out["mode"] == "automated"
    assert "PostgreSQL" in out["reason"] and "enabled" in out["reason"]


def test_other_databases_on_oci_are_still_manual(session):
    # Only Postgres has a module; the rest are still fulfilled by hand.
    for code in ("mongodb", "mssql", "oracle-db"):
        out = fulfilment.fulfilment_for(_tech(session, code), "oci")
        assert out["mode"] == "manual", code
        assert "infrastructure team" in out["reason"]


def test_postgres_is_manual_off_oci(session):
    # The managed module is OCI-only; elsewhere Postgres is still manual.
    for target in ("onprem", "azure", "aws", "gcp"):
        assert fulfilment.fulfilment_for(_tech(session, "postgres16"), target)["mode"] == "manual"


def test_targets_without_any_provisioning_path_are_manual(session):
    tech = _tech(session, "compute-vm")  # automated on OCI, nowhere else
    for target in ("onprem", "azure", "gcp"):
        out = fulfilment.fulfilment_for(tech, target)
        assert out["mode"] == "manual", target
        assert "no automated provisioning path" in out["reason"]


def test_compute_is_not_automated_on_aws(session):
    # AWS compute is a documented follow-on; only S3 is automated there.
    assert fulfilment.fulfilment_for(_tech(session, "compute-vm"), "aws")["mode"] == "manual"


def test_most_of_the_catalogue_is_manual_on_oci(session):
    techs = session.scalars(select(Technology)).all()
    automated = [t for t in techs
                 if fulfilment.fulfilment_for(t, "oci")["mode"] == "automated"]
    # The three compute technologies + OCI object storage + managed Postgres.
    assert {t.code for t in automated} == {
        "compute-vm", "rhel9", "win2019", "oci-objectstorage", "postgres16"}
    # Still a small minority of the catalogue — the honest picture.
    assert len(automated) < len(techs) / 4


def test_unknown_target_or_missing_attrs_do_not_raise():
    class Bare:
        code = ""
        resource_kind = ""
    assert fulfilment.fulfilment_for(Bare(), None)["mode"] == "manual"
    assert fulfilment.fulfilment_for(Bare(), "nonsense")["mode"] == "manual"


# --- automated_targets helper ------------------------------------------------

def test_automated_targets_filters_the_list(session):
    tech = _tech(session, "compute-vm")
    assert fulfilment.automated_targets(tech, ["onprem", "azure", "oci", "aws", "gcp"]) == ["oci"]
    pg = _tech(session, "postgres16")
    assert fulfilment.automated_targets(pg, ["onprem", "oci", "aws"]) == ["oci"]
    kafka = _tech(session, "kafka")
    assert fulfilment.automated_targets(kafka, ["onprem", "oci", "aws"]) == []


# --- Drift guard: the classifier must agree with the provisioner -------------

def test_classifier_agrees_with_the_provisioners_resource_kind(session):
    """If a technology is 'automated' on OCI, _environment_resource_kind must map
    it to a real module — compute to oci-instance, object storage to oci-bucket.
    This fails loudly if someone adds a module on one side only."""
    for code in ("compute-vm", "rhel9", "win2019", "oci-objectstorage",
                 "postgres16", "kafka", "mongodb"):
        tech = _tech(session, code)
        req = Request(reference=f"REQ-F-{code}", status="draft", requester="t@x.com",
                      request_type="create", deployment_target="oci")
        req.components = [RequestComponent(technology_code=code, size="medium")]
        session.add(req)
        session.flush()
        kind = _environment_resource_kind(session, req)
        mode = fulfilment.fulfilment_for(tech, "oci")["mode"]
        if tech.resource_kind == "oci-postgres":
            assert kind == "oci-postgres" and mode == "automated", code
        elif tech.resource_kind == "oci-instance":
            assert kind == "oci-instance" and mode == "automated", code
        elif code == "oci-objectstorage":
            assert kind == "oci-bucket" and mode == "automated", code
        else:
            # A placeholder bucket stands in for the real technology -> manual.
            assert kind == "oci-bucket" and mode == "manual", code
        session.delete(req)
        session.flush()


def test_managed_database_wins_over_compute_in_a_mixed_stack(session):
    """A stack with both Postgres and a VM must provision the database system —
    the most specific delivery — not fall back to compute."""
    req = Request(reference="REQ-F-MIX", status="draft", requester="t@x.com",
                  request_type="create", deployment_target="oci")
    req.components = [RequestComponent(technology_code="postgres16", size="medium"),
                      RequestComponent(technology_code="compute-vm", size="medium")]
    session.add(req)
    session.flush()
    assert _environment_resource_kind(session, req) == "oci-postgres"


# --- Exposed through the API -------------------------------------------------

def test_lookups_exposes_automated_targets(client):
    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["compute-vm"]["automated_targets"] == ["oci"]
    assert by_code["oci-objectstorage"]["automated_targets"] == ["oci"]
    assert by_code["aws-s3"]["automated_targets"] == ["aws"]
    # Postgres: offered on five targets, genuinely automated on OCI only.
    assert by_code["postgres16"]["automated_targets"] == ["oci"]
    assert len(by_code["postgres16"]["targets"]) == 5
    # The catalogue's majority: available everywhere, automated nowhere.
    assert by_code["kafka"]["automated_targets"] == []
