"""Target-aware technology catalogue (F-CAT).

Each technology declares which deployment targets it's offered on; submit
validation rejects a technology not available on the chosen target, and the
lookups API exposes the targets so the form can filter the catalogue.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from api.validation import validate_submission
from db.models import Technology
from db.seed import seed
from db.session import Base

_META = {
    "business_justification": "Testing the target-aware technology catalogue behaviour.",
    "priority": "medium", "business_criticality": "tier3", "required_delivery_date": "2027-06-01",
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


def _create(target, tech):
    return {"request_type": "create", "project_code": "EGATE", "cost_centre_code": "IMD-1001",
            "deployment_target": target, "environment_name": "egate-cat", "environment_tier": "uat",
            "data_classification": "internal",
            "components": [{"technology_code": tech, "size": "medium"}], **_META}


# --- Catalogue targets seeded ------------------------------------------------

def test_generic_tech_on_all_targets(session):
    pg = session.scalar(select(Technology).where(Technology.code == "postgres16"))
    assert set(pg.targets.split(",")) == {"onprem", "azure", "oci", "aws", "gcp"}


def test_cloud_service_scoped_to_its_cloud(session):
    assert session.scalar(select(Technology).where(Technology.code == "aws-rds")).targets == "aws"
    assert session.scalar(select(Technology).where(Technology.code == "oci-adb")).targets == "oci"


# --- Validation --------------------------------------------------------------

def test_generic_tech_valid_on_any_target(session):
    assert validate_submission(_create("aws", "postgres16"), session) == {}
    assert validate_submission(_create("oci", "postgres16"), session) == {}


def test_aws_service_valid_on_aws(session):
    assert validate_submission(_create("aws", "aws-rds"), session) == {}


def test_aws_service_rejected_on_other_target(session):
    errors = validate_submission(_create("oci", "aws-rds"), session)
    assert any("technology" in k for k in errors)  # aws-rds not available on OCI


def test_oci_service_rejected_on_aws(session):
    errors = validate_submission(_create("aws", "oci-adb"), session)
    assert any("technology" in k for k in errors)


# --- Lookups exposes targets -------------------------------------------------

def test_lookups_exposes_targets(client):
    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert set(by_code["postgres16"]["targets"]) == {"onprem", "azure", "oci", "aws", "gcp"}
    assert by_code["aws-rds"]["targets"] == ["aws"]
    assert by_code["gcp-gke"]["targets"] == ["gcp"]
