"""Increment 1.4 checks: /api/sizing resolves anchors and sums totals."""

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


def test_single_component_resolves_seeded_anchor(client):
    # Seed: medium = 4 vCPU / 16 GB / 200 GB.
    resp = client.post(
        "/api/sizing",
        json={"components": [{"technology_code": "postgres16", "size": "medium"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = body["components"][0]
    assert row["resolved"] is True
    assert (row["vcpu"], row["memory_gb"], row["storage_gb"]) == (4, 16, 200)
    assert body["totals"] == {"vcpu": 4, "memory_gb": 16, "storage_gb": 200}


def test_multiple_components_sum_totals(client):
    # small = 2/4/50, medium = 4/16/200  ->  totals 6/20/250
    resp = client.post(
        "/api/sizing",
        json={
            "components": [
                {"technology_code": "postgres16", "size": "small"},
                {"technology_code": "nginx", "size": "medium"},
            ]
        },
    )
    assert resp.json()["totals"] == {"vcpu": 6, "memory_gb": 20, "storage_gb": 250}


def test_blank_and_unknown_components_are_unresolved_not_errors(client):
    resp = client.post(
        "/api/sizing",
        json={
            "components": [
                {"technology_code": "", "size": ""},
                {"technology_code": "postgres16", "size": ""},
                {"technology_code": "does-not-exist", "size": "small"},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(c["resolved"] is False for c in body["components"])
    assert body["totals"] == {"vcpu": 0, "memory_gb": 0, "storage_gb": 0}
