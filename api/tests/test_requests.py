"""Increment 1.3 checks: draft save/resume and authoritative submit validation.

Runs against a fast in-memory seeded database via a dependency override.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.seed import seed
from db.session import Base


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    session.close()


VALID_CREATE = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "technology_code": "postgres16",
    "size": "medium",
    "environment_name": "egate-uat",
    "data_classification": "internal",
}


def test_save_draft_returns_reference(client):
    resp = client.post("/api/requests/draft", json={"request_type": "create"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reference"].startswith("REQ-")
    assert body["status"] == "draft"
    assert body["requester"]  # stamped with the mock requester


def test_draft_roundtrip_preserves_partial_data(client):
    ref = client.post(
        "/api/requests/draft",
        json={"request_type": "create", "project_code": "EGATE"},
    ).json()["reference"]

    reloaded = client.get(f"/api/requests/{ref}").json()
    assert reloaded["request_type"] == "create"
    assert reloaded["project_code"] == "EGATE"
    # Half-finished is fine for a draft.
    assert reloaded["technology_code"] is None


def test_updating_a_draft_keeps_same_reference(client):
    ref = client.post("/api/requests/draft", json={"request_type": "create"}).json()["reference"]
    updated = client.post(
        "/api/requests/draft", json={"reference": ref, "size": "large"}
    ).json()
    assert updated["reference"] == ref
    assert updated["size"] == "large"


def test_missing_reference_returns_404(client):
    assert client.get("/api/requests/REQ-9999-9999").status_code == 404


def test_submit_valid_create_succeeds(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"


def test_submit_rejects_bad_environment_name_with_rule(client):
    bad = {**VALID_CREATE, "environment_name": "Egate UAT!"}
    ref = client.post("/api/requests/draft", json=bad).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert "environment_name" in errors
    # The message states the rule (F-UX-10).
    assert "lowercase" in errors["environment_name"]


def test_submit_rejects_missing_required_fields(client):
    ref = client.post("/api/requests/draft", json={"request_type": "create"}).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    for field in ("project_code", "technology_code", "size", "environment_name",
                  "data_classification", "cost_centre_code"):
        assert field in errors


def test_submit_unknown_project_is_rejected(client):
    bad = {**VALID_CREATE, "project_code": "NOPE"}
    ref = client.post("/api/requests/draft", json=bad).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "project_code" in errors


def test_submit_add_requires_existing_target(client):
    ref = client.post(
        "/api/requests/draft",
        json={"request_type": "add", "cost_centre_code": "IMD-1001"},
    ).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "target_environment" in errors

    # A real seeded environment passes.
    client.post(
        "/api/requests/draft",
        json={"reference": ref, "target_environment": "egate-prod"},
    )
    ok = client.post(f"/api/requests/{ref}/submit")
    assert ok.status_code == 200
