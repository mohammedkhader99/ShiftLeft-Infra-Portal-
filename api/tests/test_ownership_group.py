"""Group-based ownership checks (F-IAM-09).

An environment can be owned by a directory group, so ownership survives an
individual leaving, and group members can manage it. Group membership is resolved
from a mock GROUP_MAP here (live = Jira groups).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import roles
from api.main import app, get_session
from db.models import AuditLog, Request
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


def _prov(session, ref="REQ-G", requester="owner@x.com", group=None, env_owner=None):
    req = Request(reference=ref, status="provisioned", requester=requester, request_type="create",
                  environment_name=ref.lower(), environment_owner=env_owner, owner_group=group)
    session.add(req)
    session.commit()
    return req


# --- Group resolution --------------------------------------------------------

def test_user_groups_from_mock_map(monkeypatch):
    monkeypatch.setenv("GROUP_MAP", '{"m@x.com": ["team-a", "team-b"]}')
    assert roles.user_groups("m@x.com") == ["team-a", "team-b"]
    assert roles.user_groups("other@x.com") == []
    assert roles.user_groups("") == []


# --- Survives staff movement -------------------------------------------------

def test_group_owned_env_is_not_orphaned(session, monkeypatch):
    req = _prov(session, requester="gone@x.com", env_owner="gone@x.com", group="platform-team")
    monkeypatch.setenv("DEPARTED_OWNERS", "gone@x.com")
    assert main._is_orphan(req) is False          # the group carries ownership
    req.owner_group = None
    assert main._is_orphan(req) is True            # without a group, it IS orphaned


# --- Group members can manage it --------------------------------------------

def test_group_member_counts_as_owner(session, monkeypatch):
    req = _prov(session, requester="orig@x.com", group="platform-team")
    monkeypatch.setenv("ROLE_MAP", '{"member@x.com": ["requester"], "stranger@x.com": ["requester"]}')
    monkeypatch.setenv("GROUP_MAP", '{"member@x.com": ["platform-team"]}')
    assert main._owner_or_admin(req, "member@x.com") is True      # in the owning group
    assert main._owner_or_admin(req, "stranger@x.com") is False   # not admin/member/owner


# --- Endpoint ----------------------------------------------------------------

def test_set_and_clear_owner_group(client, session):
    _prov(session, "REQ-G")
    body = client.put("/api/requests/REQ-G/owner-group", json={"group": "platform-team"}).json()
    assert body["owner_group"] == "platform-team" and body["orphaned"] is False
    assert session.scalars(select(AuditLog).where(AuditLog.event == "ownership.group_set")).all()
    assert client.put("/api/requests/REQ-G/owner-group", json={"group": ""}).json()["owner_group"] is None


def test_set_owner_group_rbac(client, session, monkeypatch):
    _prov(session, "REQ-G", requester="owner@x.com")
    monkeypatch.setenv("ROLE_MAP", '{"nobody@x.com": ["requester"]}')
    r = client.put("/api/requests/REQ-G/owner-group", headers={"X-Requester": "nobody@x.com"},
                   json={"group": "platform-team"})
    assert r.status_code == 403


def test_owner_group_on_request_view(client, session):
    _prov(session, "REQ-G", group="platform-team")
    row = next(r for r in client.get("/api/requests").json() if r["reference"] == "REQ-G")
    assert row["owner_group"] == "platform-team"
