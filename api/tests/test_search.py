"""Header quick-search over the caller's own requests (Portal UI polish).

Server-side, case-insensitive contains match on reference / environment / project
/ cost centre, scoped to the caller. Also covers the reference filter added to
/api/requests so a search result lands on the one request.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.models import Request
from db.seed import seed
from db.session import Base

ALICE = {"X-Requester": "alice@x.com"}


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


def _req(session, ref, requester, *, env=None, project=None, tier=None, status="provisioned"):
    session.add(Request(reference=ref, requester=requester, request_type="create", status=status,
                        environment_name=env, project_code=project, environment_tier=tier))
    session.commit()


def test_matches_reference(client, session):
    _req(session, "REQ-2026-0042", "alice@x.com", env="egate-uat", tier="uat")
    body = client.get("/api/search", params={"q": "0042"}, headers=ALICE).json()
    assert [r["reference"] for r in body["results"]] == ["REQ-2026-0042"]
    assert body["results"][0]["environment"] == "egate-uat" and body["results"][0]["tier"] == "uat"


def test_matches_environment_case_insensitive(client, session):
    _req(session, "REQ-1", "alice@x.com", env="eGate-UAT")
    body = client.get("/api/search", params={"q": "egate"}, headers=ALICE).json()
    assert [r["reference"] for r in body["results"]] == ["REQ-1"]


def test_matches_project(client, session):
    _req(session, "REQ-1", "alice@x.com", env="e", project="EGATE")
    assert client.get("/api/search", params={"q": "egat"}, headers=ALICE).json()["results"]


def test_scoped_to_caller(client, session):
    _req(session, "REQ-BOB", "bob@x.com", env="bobs-env")
    body = client.get("/api/search", params={"q": "REQ-BOB"}, headers=ALICE).json()
    assert body["results"] == []  # not alice's, so hidden


def test_short_query_returns_nothing(client, session):
    _req(session, "REQ-1", "alice@x.com", env="egate")
    assert client.get("/api/search", params={"q": ""}, headers=ALICE).json()["results"] == []
    assert client.get("/api/search", params={"q": "e"}, headers=ALICE).json()["results"] == []


def test_reference_filter_on_requests(client, session):
    _req(session, "REQ-1", "alice@x.com", env="a")
    _req(session, "REQ-2", "alice@x.com", env="b")
    rows = client.get("/api/requests", params={"requester": "alice@x.com", "reference": "REQ-2"}).json()
    assert [r["reference"] for r in rows] == ["REQ-2"]
