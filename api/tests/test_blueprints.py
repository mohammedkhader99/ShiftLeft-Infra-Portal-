"""Blueprint registry — technology → build recipe, per cloud (F-CAT-10).

A blueprint is a reviewed recipe in git; this registry is the *pointer* plus the
certification record. Two properties matter:

  * the portal must never advertise a blueprint the ORCHESTRATOR doesn't ship —
    that would promise a capability that cannot run;
  * a blueprint certified here but missing from the orchestrator must be flagged,
    not silently dropped: that is the dangerous direction.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from db.models import AuditLog, Blueprint
from db.seed import seed
from db.session import Base

# What the real orchestrator reports today.
_SHIPPED = [
    {"ref": "oci/compute-instance", "target": "oci", "resource_kind": "oci-instance",
     "builds": ["compute-vm", "rhel9", "win2019"], "description": "OCI Compute VM."},
    {"ref": "oci/object-storage", "target": "oci", "resource_kind": "oci-bucket",
     "builds": ["oci-objectstorage"], "description": "OCI bucket."},
    {"ref": "oci/postgres", "target": "oci", "resource_kind": "oci-postgres",
     "builds": ["postgres16"], "description": "Managed PostgreSQL."},
    {"ref": "aws/s3", "target": "aws", "resource_kind": "aws-bucket",
     "builds": ["aws-s3"], "description": "S3 bucket."},
]


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


@pytest.fixture()
def shipped(monkeypatch):
    monkeypatch.setattr(main, "_orchestrator_blueprints", lambda: list(_SHIPPED))


# --- The matrix --------------------------------------------------------------

def test_uncertified_recipes_show_as_draft_not_automated(client, shipped):
    """Shipping a recipe is not the same as approving it for use."""
    body = client.get("/api/blueprints").json()
    states = {(b["technology_code"], b["deployment_target"]): b["state"] for b in body["blueprints"]}
    assert states[("postgres16", "oci")] == "draft"
    assert body["certified"] == 0


def test_certifying_flips_the_state_and_is_audited(client, session, shipped):
    r = client.post("/api/blueprints", json={
        "technology_code": "postgres16", "deployment_target": "oci",
        "blueprint_ref": "oci/postgres", "version": "2.0.1"})
    assert r.status_code == 200
    row = session.get(Blueprint, ("postgres16", "oci"))
    assert row.status == "certified" and row.certified_by and row.certified_at
    assert session.scalars(select(AuditLog).where(AuditLog.event == "blueprint.certified")).all()

    body = client.get("/api/blueprints").json()
    entry = next(b for b in body["blueprints"]
                 if b["technology_code"] == "postgres16" and b["deployment_target"] == "oci")
    assert entry["state"] == "certified" and entry["version"] == "2.0.1"
    assert body["certified"] == 1


def test_decertifying_returns_it_to_draft(client, session, shipped):
    client.post("/api/blueprints", json={"technology_code": "postgres16",
                                         "deployment_target": "oci", "blueprint_ref": "oci/postgres"})
    assert client.delete("/api/blueprints/postgres16/oci").status_code == 200
    assert session.get(Blueprint, ("postgres16", "oci")) is None
    entry = next(b for b in client.get("/api/blueprints").json()["blueprints"]
                 if b["technology_code"] == "postgres16" and b["deployment_target"] == "oci")
    assert entry["state"] == "draft"  # the recipe still exists, it just isn't approved


# --- The two safety properties ----------------------------------------------

def test_cannot_certify_something_the_orchestrator_does_not_ship(client, shipped):
    """Otherwise the portal would advertise a capability that cannot run."""
    r = client.post("/api/blueprints", json={
        "technology_code": "kafka", "deployment_target": "oci", "blueprint_ref": "oci/kafka"})
    assert r.status_code == 422
    assert "ships no blueprint" in r.json()["detail"]


def test_certified_but_missing_from_the_orchestrator_is_flagged(client, session, monkeypatch):
    """The dangerous direction: it was certified, then the orchestrator lost it.
    That must surface, not silently disappear from the matrix."""
    monkeypatch.setattr(main, "_orchestrator_blueprints", lambda: list(_SHIPPED))
    client.post("/api/blueprints", json={"technology_code": "postgres16",
                                         "deployment_target": "oci", "blueprint_ref": "oci/postgres"})
    # The orchestrator is redeployed without the Postgres module.
    monkeypatch.setattr(main, "_orchestrator_blueprints",
                        lambda: [b for b in _SHIPPED if b["ref"] != "oci/postgres"])
    body = client.get("/api/blueprints").json()
    entry = next(b for b in body["blueprints"]
                 if b["technology_code"] == "postgres16" and b["deployment_target"] == "oci")
    assert entry["state"] == "missing"
    assert body["missing"] == 1


def test_unreachable_orchestrator_refuses_certification(client, monkeypatch):
    """Certifying blind would let someone approve a recipe nobody can confirm."""
    monkeypatch.setattr(main, "_orchestrator_blueprints", lambda: None)
    r = client.post("/api/blueprints", json={"technology_code": "postgres16",
                                            "deployment_target": "oci", "blueprint_ref": "x"})
    assert r.status_code == 503
    assert client.get("/api/blueprints").json()["orchestrator_available"] is False


# --- Validation + access -----------------------------------------------------

def test_unknown_technology_or_target_is_rejected(client, shipped):
    assert client.post("/api/blueprints", json={"technology_code": "nope",
                                                "deployment_target": "oci", "blueprint_ref": "x"}).status_code == 422
    assert client.post("/api/blueprints", json={"technology_code": "postgres16",
                                                "deployment_target": "mars", "blueprint_ref": "x"}).status_code == 422


def test_blueprints_are_admin_only(client, shipped, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@emaratechg.ae": ["read_only"]}')
    h = {"X-Requester": "ro@emaratechg.ae"}
    assert client.get("/api/blueprints", headers=h).status_code == 403
    assert client.post("/api/blueprints", headers=h,
                       json={"technology_code": "postgres16", "deployment_target": "oci",
                             "blueprint_ref": "x"}).status_code == 403


# --- Migration proof ---------------------------------------------------------

def test_registry_agrees_with_todays_honesty_badge(client, session, shipped):
    """Before the catalogue badge can be driven from this table, the two must
    already give identical answers. This is the evidence that switching over
    changes nothing a requester sees."""
    from api import fulfilment
    from db.models import Technology

    # Certify everything the orchestrator ships, as the real registry would hold.
    for bp in _SHIPPED:
        for code in bp["builds"]:
            client.post("/api/blueprints", json={
                "technology_code": code, "deployment_target": bp["target"],
                "blueprint_ref": bp["ref"]})

    from_registry = {(b["technology_code"], b["deployment_target"])
                     for b in client.get("/api/blueprints").json()["blueprints"]
                     if b["state"] == "certified"}
    from_badge = {
        (t.code, target)
        for t in session.scalars(select(Technology)).all()
        for target in ("onprem", "azure", "oci", "aws", "gcp")
        if fulfilment.fulfilment_for(t, target)["mode"] == "automated"
    }
    assert from_registry == from_badge, (
        f"registry and badge disagree:\n  only in registry: {sorted(from_registry - from_badge)}"
        f"\n  only in badge   : {sorted(from_badge - from_registry)}")
