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
    {"ref": "oci/apache-httpd", "target": "oci", "resource_kind": "oci-apache",
     "builds": ["apache"], "description": "Apache on OCI Compute."},
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


# --- The registry drives the catalogue badge ---------------------------------

def test_certifying_a_blueprint_flips_the_catalogue_badge(client, session, shipped):
    """The bug this closes: a blueprint was certified on the Blueprints page and
    the request form still showed the technology as 'manual', because the badge
    was computed from rules in code instead of from the registry."""
    from api import fulfilment

    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["postgres16"]["automated_targets"] == []

    client.post("/api/blueprints", json={"technology_code": "postgres16",
                                         "deployment_target": "oci",
                                         "blueprint_ref": "oci/postgres", "version": "2.0.1"})
    fulfilment.invalidate_cache()

    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["postgres16"]["automated_targets"] == ["oci"]

    # ...and withdrawing it puts the technology back to manual.
    client.delete("/api/blueprints/postgres16/oci")
    fulfilment.invalidate_cache()
    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["postgres16"]["automated_targets"] == []


# --- What actually gets built --------------------------------------------

def test_a_certified_blueprint_decides_the_resource_kind(client, session, shipped):
    """The bug this closes: apache was certified with the oci/apache-httpd
    blueprint, and a request still provisioned an object-storage bucket because
    the derivation read Technology.resource_kind instead of the registry."""
    from api.main import _environment_resource_kind
    from db.models import Request, RequestComponent

    req = Request(reference="REQ-BP-1", status="submitted", requester="u@x.com",
                  request_type="create", deployment_target="oci")
    req.components = [RequestComponent(technology_code="apache", size="small")]
    session.add(req)
    session.flush()

    # Before certification: the legacy derivation, a placeholder bucket.
    assert _environment_resource_kind(session, req) == "oci-bucket"

    client.post("/api/blueprints", json={"technology_code": "apache",
                                         "deployment_target": "oci",
                                         "blueprint_ref": "oci/apache-httpd"})
    # After: what the blueprint actually builds.
    assert _environment_resource_kind(session, req) == "oci-apache"


def test_certification_records_the_manifests_resource_kind(client, session, shipped):
    """Without this the derivation has nothing to read."""
    from db.models import Blueprint as BP
    client.post("/api/blueprints", json={"technology_code": "postgres16",
                                         "deployment_target": "oci",
                                         "blueprint_ref": "oci/postgres"})
    assert session.get(BP, ("postgres16", "oci")).resource_kind == "oci-postgres"


def test_withdrawing_returns_to_the_legacy_derivation(client, session, shipped):
    from api.main import _environment_resource_kind
    from db.models import Request, RequestComponent

    req = Request(reference="REQ-BP-2", status="submitted", requester="u@x.com",
                  request_type="create", deployment_target="oci")
    req.components = [RequestComponent(technology_code="apache", size="small")]
    session.add(req)
    session.flush()
    client.post("/api/blueprints", json={"technology_code": "apache",
                                         "deployment_target": "oci",
                                         "blueprint_ref": "oci/apache-httpd"})
    assert _environment_resource_kind(session, req) == "oci-apache"
    client.delete("/api/blueprints/apache/oci")
    assert _environment_resource_kind(session, req) == "oci-bucket"


# --- Withdrawn by evidence (C1) ----------------------------------------------

def test_a_suspended_blueprint_is_not_shown_as_a_plain_draft(client, session, shipped):
    """The console must distinguish 'nobody certified this' from 'the portal
    withdrew this after it kept failing'.

    Those need different actions from an admin, and collapsing both to 'draft'
    hides the second — which is the state that means something is broken.
    """
    from api import certification

    session.add(Blueprint(technology_code="postgres16", deployment_target="oci",
                          blueprint_ref="oci/postgres", resource_kind="oci-postgres",
                          status=certification.SUSPENDED,
                          certified_by="mohammed.khader@emaratechg.ae",
                          notes="Certification withdrawn automatically after 3 "
                                "consecutive failures: REQ-2026-0155, REQ-2026-0158."))
    session.commit()

    body = client.get("/api/blueprints").json()
    row = next(b for b in body["blueprints"]
               if (b["technology_code"], b["deployment_target"]) == ("postgres16", "oci"))

    assert row["state"] == "suspended", row["state"]
    # And it must carry the reason, or the console has nothing to show.
    assert "REQ-2026-0155" in (row["notes"] or ""), row["notes"]
    assert body["suspended"] == 1
    # It is emphatically not certified any more.
    assert body["certified"] == 0


def test_a_withdrawn_recipe_has_its_own_state(client, session, shipped):
    """'suspended' means the evidence turned against a recipe that is still
    there; 'withdrawn' means the recipe is gone. The admin's next step differs,
    and the sweep treats them differently, so the console must not fold one
    into the other."""
    from api import certification
    session.add(Blueprint(technology_code="mysql", deployment_target="oci",
                          blueprint_ref="oci/service-vm", resource_kind="oci-service-vm",
                          status="certified", certified_by=certification.CERTIFIED_BY_RUNNER))
    session.commit()
    assert certification.withdraw_for_missing_recipe(
        session, "mysql", "oci", "the recipe was the command-line client")
    session.commit()

    body = client.get("/api/blueprints").json()
    row = next(b for b in body["blueprints"]
               if (b["technology_code"], b["deployment_target"]) == ("mysql", "oci"))

    assert row["state"] == "withdrawn", row["state"]
    assert "command-line client" in (row["notes"] or "")
    assert body["withdrawn"] == 1
    assert body["suspended"] == 0
