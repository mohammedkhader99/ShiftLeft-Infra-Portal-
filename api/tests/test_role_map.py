"""Group -> role mapping admin panel (E1, F-IAM-01).

The map is DB-backed so admins manage it from the portal (it takes precedence
over the JIRA_ROLE_MAP env), which unblocks turning live RBAC enforcement on. A
resolver preview shows what a user would resolve to before the switch is flipped.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import roles as roles_mod
from api.main import app, get_session
from db.models import AuditLog, RoleMapping
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


# --- Map precedence + resolution ---------------------------------------------

def test_db_map_takes_precedence_over_env(monkeypatch):
    monkeypatch.setattr(roles_mod, "_load_db_role_map", lambda: {"imd-approvers": "approver"})
    monkeypatch.setenv("JIRA_ROLE_MAP", '{"imd-approvers": "requester"}')
    assert roles_mod._group_role_map() == {"imd-approvers": "approver"}  # DB wins


def test_env_fallback_when_db_empty(monkeypatch):
    monkeypatch.setattr(roles_mod, "_load_db_role_map", lambda: None)
    monkeypatch.setenv("JIRA_ROLE_MAP", '{"imd-finance": "finops"}')
    assert roles_mod._group_role_map() == {"imd-finance": "finops"}


def test_live_resolve_uses_db_map(monkeypatch):
    roles_mod.clear_cache()
    monkeypatch.setenv("ROLE_SOURCE", "jira")
    monkeypatch.setattr(roles_mod, "_jira_groups", lambda email: ["imd-approvers"])
    monkeypatch.setattr(roles_mod, "_load_db_role_map", lambda: {"imd-approvers": "approver"})
    assert roles_mod.resolve_roles("bob@x.com") == {"approver"}
    roles_mod.clear_cache()


def test_load_db_role_map_reads_rows(session, monkeypatch):
    session.add(RoleMapping(jira_group="imd-admins", role="platform_admin"))
    session.commit()
    monkeypatch.setattr(roles_mod, "SessionLocal", sessionmaker(bind=session.get_bind(), expire_on_commit=False))
    assert roles_mod._load_db_role_map() == {"imd-admins": "platform_admin"}


# --- Endpoints ---------------------------------------------------------------

def test_crud_map(client, session):
    r = client.put("/api/access/role-map", json={"jira_group": "imd-approvers", "role": "approver"})
    assert r.status_code == 200 and r.json()["role"] == "approver"
    listed = client.get("/api/access/role-map").json()
    assert listed["mappings"][0]["jira_group"] == "imd-approvers"
    assert "platform_admin" in listed["roles"] and "role_source" in listed
    # update in place
    client.put("/api/access/role-map", json={"jira_group": "imd-approvers", "role": "finops"})
    assert client.get("/api/access/role-map").json()["mappings"][0]["role"] == "finops"
    # delete
    assert client.delete("/api/access/role-map/imd-approvers").json()["deleted"] == "imd-approvers"
    assert client.get("/api/access/role-map").json()["mappings"] == []
    assert session.scalars(select(AuditLog).where(AuditLog.event == "rolemap.set")).all()


def test_put_validates_role(client):
    assert client.put("/api/access/role-map", json={"jira_group": "g", "role": "wizard"}).status_code == 422
    assert client.put("/api/access/role-map", json={"jira_group": "", "role": "approver"}).status_code == 422


def test_delete_unknown_404(client):
    assert client.delete("/api/access/role-map/nope").status_code == 404


def test_rbac_requester_forbidden(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    h = {"X-Requester": "req@x.com"}
    assert client.get("/api/access/role-map", headers=h).status_code == 403
    assert client.put("/api/access/role-map", headers=h, json={"jira_group": "g", "role": "approver"}).status_code == 403


def test_resolve_preview(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"alice@x.com": ["finops"]}')
    monkeypatch.setenv("GROUP_MAP", '{"alice@x.com": ["imd-finance"]}')
    body = client.get("/api/access/resolve", params={"email": "alice@x.com"}).json()
    assert body["email"] == "alice@x.com" and body["roles"] == ["finops"] and body["groups"] == ["imd-finance"]
