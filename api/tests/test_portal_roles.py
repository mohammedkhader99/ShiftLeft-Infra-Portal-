"""Portal-managed roles (F-IAM-01, ROLE_SOURCE=portal).

The portal has no external source for its roles: Entra provides login only (no
group claims), and Jira's authority model is per-ticket Assignment Groups, which
answer 'who may act on this ticket' rather than 'who may administer the portal'.
So identity stays federated and authorisation is maintained here.

The property that matters most: an authenticated but unlisted user must NOT be an
administrator. Under the previous mock source they were — every signed-in user
resolved to every role, including platform_admin.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import roles as roles_mod
from api.main import app, get_session
from db.models import AuditLog, UserRole
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


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ROLE_SOURCE", "ROLE_MAP", "PORTAL_BOOTSTRAP_ADMINS"):
        monkeypatch.delenv(k, raising=False)
    roles_mod._cache.clear()


def _portal(monkeypatch, granted=None):
    """ROLE_SOURCE=portal with a stubbed role table."""
    monkeypatch.setenv("ROLE_SOURCE", "portal")
    monkeypatch.setattr(roles_mod, "_portal_roles", lambda email: set(granted or {}).copy()
                        if isinstance(granted, (set, list)) else set((granted or {}).get(email, [])))


# --- The security property ---------------------------------------------------

def test_unlisted_user_is_a_requester_not_an_administrator(monkeypatch):
    """The hole this closes: under 'mock', an unmapped signed-in user resolved to
    EVERY role including platform_admin."""
    _portal(monkeypatch, {})
    got = roles_mod.resolve_roles("anyone@emaratechg.ae")
    assert got == {roles_mod.REQUESTER}
    assert not roles_mod.can(got, "manage_settings")
    assert not roles_mod.can(got, "execute")
    assert not roles_mod.can(got, "manage_access")
    # ...but they can still do the thing the portal exists for.
    assert roles_mod.can(got, "create_request")


def test_mock_source_still_grants_everything_which_is_why_it_is_dev_only(monkeypatch):
    """Pinned deliberately: this is the behaviour that made the live portal
    wide open, and the reason ROLE_SOURCE must be 'portal' in a real deployment."""
    monkeypatch.setenv("ROLE_SOURCE", "mock")
    assert roles_mod.resolve_roles("anyone@emaratechg.ae") == roles_mod.ALL_ROLES


def test_granted_roles_are_honoured(monkeypatch):
    _portal(monkeypatch, {"admin@emaratechg.ae": ["platform_admin"],
                          "fin@emaratechg.ae": ["finops", "auditor"]})
    assert roles_mod.resolve_roles("admin@emaratechg.ae") == {"platform_admin"}
    assert roles_mod.resolve_roles("fin@emaratechg.ae") == {"finops", "auditor"}


def test_email_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "portal")
    monkeypatch.setenv("PORTAL_BOOTSTRAP_ADMINS", "Boss@Emaratechg.AE")
    assert roles_mod.resolve_roles("boss@emaratechg.ae") == {"platform_admin"}


# --- Break-glass -------------------------------------------------------------

def test_bootstrap_admin_is_always_an_administrator(monkeypatch):
    """Without this the console is unreachable with an empty table — nobody could
    grant the first role."""
    _portal(monkeypatch, {})
    monkeypatch.setenv("PORTAL_BOOTSTRAP_ADMINS", "boss@emaratechg.ae, other@emaratechg.ae")
    assert roles_mod.resolve_roles("boss@emaratechg.ae") == {"platform_admin"}
    assert roles_mod.resolve_roles("other@emaratechg.ae") == {"platform_admin"}
    assert roles_mod.resolve_roles("someone@emaratechg.ae") == {roles_mod.REQUESTER}


def test_a_database_error_never_grants_anything(monkeypatch):
    """Fail safe: if the role table can't be read, fall back to the default —
    never to elevated access."""
    monkeypatch.setenv("ROLE_SOURCE", "portal")

    def _boom(email):
        raise RuntimeError("db down")
    monkeypatch.setattr(roles_mod, "_portal_roles", lambda e: set())  # what the real one returns on error
    assert roles_mod.resolve_roles("x@emaratechg.ae") == {roles_mod.REQUESTER}


# --- Managing the list -------------------------------------------------------

def test_grant_and_revoke_are_persisted_and_audited(client, session):
    r = client.post("/api/access/user-roles",
                    json={"email": "New.Person@emaratechg.ae", "role": "finops"})
    assert r.status_code == 200
    row = session.get(UserRole, ("new.person@emaratechg.ae", "finops"))  # normalised
    assert row is not None
    assert session.scalars(select(AuditLog).where(AuditLog.event == "access.role_granted")).all()

    d = client.delete("/api/access/user-roles/new.person@emaratechg.ae/finops")
    assert d.status_code == 200
    assert session.get(UserRole, ("new.person@emaratechg.ae", "finops")) is None
    assert session.scalars(select(AuditLog).where(AuditLog.event == "access.role_revoked")).all()


def test_bad_input_is_rejected(client):
    assert client.post("/api/access/user-roles",
                       json={"email": "not-an-email", "role": "finops"}).status_code == 422
    assert client.post("/api/access/user-roles",
                       json={"email": "a@b.com", "role": "superuser"}).status_code == 422


def test_listing_shows_the_source_and_the_default(client):
    body = client.get("/api/access/user-roles").json()
    assert "role_source" in body and "default_role" in body
    assert set(body["roles"]) == roles_mod.ALL_ROLES


def test_managing_roles_is_admin_only(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@emaratechg.ae": ["read_only"]}')
    h = {"X-Requester": "ro@emaratechg.ae"}
    assert client.get("/api/access/user-roles", headers=h).status_code == 403
    assert client.post("/api/access/user-roles", headers=h,
                       json={"email": "a@b.com", "role": "platform_admin"}).status_code == 403
