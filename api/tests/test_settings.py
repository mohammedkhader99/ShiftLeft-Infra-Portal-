"""Runtime settings store + Admin settings API (F-OPS-09).

The store overrides non-secret governance/FinOps/AI settings from the DB, with
precedence DB -> .env -> default. Secrets are never editable. Covers the accessor
precedence, value validation, and the admin endpoints (RBAC, audit, allow-list).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import settings
from api.main import app, get_session
from db.models import AuditLog, Setting
from db.seed import seed
from db.session import Base


@pytest.fixture()
def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)
    yield session
    session.close()


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


# --- Accessor precedence -----------------------------------------------------

def test_env_precedence_db_over_environment(monkeypatch):
    # DB override wins over .env for an allow-listed key.
    monkeypatch.setenv("AI_MODE", "mock")
    monkeypatch.setattr(settings, "_load_overrides", lambda: {"AI_MODE": "live"})
    assert settings.env("AI_MODE", "mock") == "live"


def test_env_falls_back_to_environment_then_default(monkeypatch):
    monkeypatch.setattr(settings, "_load_overrides", lambda: {})
    monkeypatch.setenv("APPROVAL_QUORUM", "3")
    assert settings.env("APPROVAL_QUORUM", "1") == "3"
    monkeypatch.delenv("APPROVAL_QUORUM", raising=False)
    assert settings.env("APPROVAL_QUORUM", "1") == "1"


def test_non_allowlisted_key_ignores_db_override(monkeypatch):
    # A DB override must only apply to allow-listed keys — never leak elsewhere.
    monkeypatch.setattr(settings, "_load_overrides", lambda: {"AUTH_MODE": "mock"})
    monkeypatch.setenv("AUTH_MODE", "live")
    assert settings.env("AUTH_MODE", "x") == "live"  # from env, override ignored


def test_source_of(monkeypatch):
    monkeypatch.setattr(settings, "_load_overrides", lambda: {"SOD_ENFORCED": "false"})
    assert settings.source_of("SOD_ENFORCED") == "database"
    monkeypatch.setattr(settings, "_load_overrides", lambda: {})
    monkeypatch.setenv("SOD_ENFORCED", "false")
    assert settings.source_of("SOD_ENFORCED") == "env"
    monkeypatch.delenv("SOD_ENFORCED", raising=False)
    assert settings.source_of("SOD_ENFORCED") == "default"


# --- Validation --------------------------------------------------------------

def test_coerce_validates_each_type():
    assert settings.coerce("SOD_ENFORCED", "yes") == "true"
    assert settings.coerce("SOD_ENFORCED", "off") == "false"
    assert settings.coerce("AI_MODE", "live") == "live"
    assert settings.coerce("APPROVAL_QUORUM", "3") == "3"
    assert settings.coerce("BUDGET_WARN_PCT", "80") == "80"

    with pytest.raises(ValueError):
        settings.coerce("AI_MODE", "turbo")             # not a choice
    with pytest.raises(ValueError):
        settings.coerce("APPROVAL_QUORUM", "0")         # below min 1
    with pytest.raises(ValueError):
        settings.coerce("BUDGET_WARN_PCT", "150")       # above max 100
    with pytest.raises(ValueError):
        settings.coerce("APPROVAL_QUORUM", "lots")      # not a number


# --- Admin API ---------------------------------------------------------------

def test_list_settings_separates_editable_from_readonly(client):
    body = client.get("/api/admin/settings").json()
    editable_keys = {e["key"] for e in body["editable"]}
    readonly_keys = {r["key"] for r in body["read_only"]}
    assert "SOD_ENFORCED" in editable_keys and "AI_MODE" in editable_keys
    assert "AUTH_MODE" in readonly_keys and "PROVISION_MODE" in readonly_keys
    # No secret ever appears in either list.
    all_keys = editable_keys | readonly_keys
    for secret in ("ANTHROPIC_API_KEY", "JIRA_PAT", "OIDC_CLIENT_SECRET",
                   "SESSION_SECRET", "AWS_SECRET_ACCESS_KEY", "POSTGRES_PASSWORD"):
        assert secret not in all_keys


def test_put_setting_persists_and_audits(client, session):
    r = client.put("/api/admin/settings/SOD_ENFORCED", json={"value": "false"})
    assert r.status_code == 200
    row = session.get(Setting, "SOD_ENFORCED")
    assert row is not None and row.value == "false"
    audits = session.scalars(select(AuditLog).where(AuditLog.event == "setting.changed")).all()
    assert len(audits) == 1 and audits[0].detail["to"] == "false"


def test_put_rejects_bad_value_and_unknown_key(client, session):
    assert client.put("/api/admin/settings/AI_MODE", json={"value": "turbo"}).status_code == 422
    # A non-editable / unknown key is refused (never writable).
    assert client.put("/api/admin/settings/AUTH_MODE", json={"value": "mock"}).status_code == 404
    assert session.get(Setting, "AUTH_MODE") is None


def test_delete_reverts_override_and_audits(client, session):
    client.put("/api/admin/settings/AI_MODE", json={"value": "live"})
    r = client.delete("/api/admin/settings/AI_MODE")
    assert r.status_code == 200
    assert session.get(Setting, "AI_MODE") is None
    assert session.scalars(select(AuditLog).where(AuditLog.event == "setting.reset")).all()


def test_settings_api_is_platform_admin_only(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@emaratechg.ae": ["read_only"]}')
    h = {"X-Requester": "ro@emaratechg.ae"}
    assert client.get("/api/admin/settings", headers=h).status_code == 403
    assert client.put("/api/admin/settings/SOD_ENFORCED", headers=h,
                      json={"value": "false"}).status_code == 403
