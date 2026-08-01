"""Just-in-time access + vault credential delivery checks (F-IAM-07 / F-INT-05).

The overriding rule: the portal NEVER holds, stores, logs, or re-serves the actual
credential. It records only grant metadata + a vault handle; the credential is a
one-time vault link, shown once. The vault is mocked so the suite stays offline.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import vault
from api.main import app, get_session
from db.models import AccessGrant, Approval, AuditLog, Request
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


def _prov(session, ref="REQ-UAT"):
    req = Request(reference=ref, status="provisioned", requester="owner@x.com", request_type="create",
                  environment_name=ref.lower(), environment_tier="uat")
    session.add(req)
    session.add(Approval(jira_key=f"INFRA-{ref}", status="approved", request=req))
    session.commit()
    return req


# --- Vault adapter -----------------------------------------------------------

def test_vault_mock_issues_placeholder_not_a_secret():
    cred = vault.issue_credential("REQ-1", "dev@x.com", "ssh", 4)
    assert cred["mock"] is True and cred["link"].startswith("https://vault.mock.local/")
    assert "handle" in cred and "expires_at" in cred


def test_vault_live_is_unconfigured(monkeypatch):
    monkeypatch.setenv("VAULT_MODE", "live")
    with pytest.raises(vault.VaultUnavailable):
        vault.issue_credential("REQ-1", "dev@x.com", "ssh", 4)


# --- Grant -------------------------------------------------------------------

def test_grant_returns_one_time_link_and_stores_no_secret(client, session):
    _prov(session)
    body = client.post("/api/requests/REQ-UAT/access",
                       json={"grantee": "dev@x.com", "scope": "ssh", "ttl_hours": 4}).json()
    # the credential link is returned ONCE, in its own block
    assert body["credential"]["link"].startswith("https://vault.mock.local/")
    assert body["grant"]["grantee"] == "dev@x.com" and body["grant"]["status"] == "active"
    # the stored grant carries NO secret/link — only a handle
    grant = session.scalar(select(AccessGrant).where(AccessGrant.reference == "REQ-UAT"))
    cols = set(grant.__table__.columns.keys())
    assert "link" not in cols and "secret" not in cols and "credential" not in cols
    assert grant.vault_handle and grant.status == "active"
    # audit records the grant but not the credential/link
    ev = session.scalar(select(AuditLog).where(AuditLog.event == "access.granted"))
    assert ev.detail["grantee"] == "dev@x.com" and "link" not in ev.detail and "secret" not in ev.detail


def test_list_and_request_views_never_include_the_link(client, session):
    _prov(session)
    client.post("/api/requests/REQ-UAT/access", json={"grantee": "dev@x.com", "scope": "ssh"})
    grants = client.get("/api/requests/REQ-UAT/access").json()["grants"]
    assert grants and all("link" not in g and "secret" not in g for g in grants)
    row = next(r for r in client.get("/api/requests").json() if r["reference"] == "REQ-UAT")
    assert row["access_grants"] and all("link" not in g for g in row["access_grants"])


def test_grant_rejects_non_provisioned(client, session):
    session.add(Request(reference="REQ-D", status="draft", request_type="create", requester="u@x.com"))
    session.commit()
    assert client.post("/api/requests/REQ-D/access", json={"grantee": "d@x.com"}).status_code == 400


def test_grant_validates_scope(client, session):
    _prov(session)
    assert client.post("/api/requests/REQ-UAT/access",
                       json={"grantee": "d@x.com", "scope": "root"}).status_code == 422


def test_grant_rbac_requester_forbidden(client, session, monkeypatch):
    _prov(session)
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.post("/api/requests/REQ-UAT/access", headers={"X-Requester": "req@x.com"},
                    json={"grantee": "req@x.com", "scope": "ssh"})
    assert r.status_code == 403


def test_grant_live_vault_returns_501(client, session, monkeypatch):
    _prov(session)
    monkeypatch.setenv("VAULT_MODE", "live")
    r = client.post("/api/requests/REQ-UAT/access", json={"grantee": "d@x.com", "scope": "ssh"})
    assert r.status_code == 501
    # nothing recorded without a credential
    assert session.scalars(select(AccessGrant)).all() == []


# --- Revoke + expiry ---------------------------------------------------------

def test_revoke_marks_revoked_and_audits(client, session):
    _prov(session)
    gid = client.post("/api/requests/REQ-UAT/access", json={"grantee": "d@x.com", "scope": "ssh"}).json()["grant"]["id"]
    assert client.post(f"/api/requests/REQ-UAT/access/{gid}/revoke").json()["status"] == "revoked"
    assert session.get(AccessGrant, gid).status == "revoked"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "access.revoked")).all()


def test_sweep_expires_past_grants(session):
    from datetime import datetime, timedelta, timezone
    _prov(session)
    g = AccessGrant(reference="REQ-UAT", grantee="d@x.com", scope="ssh", granted_by="approver",
                    expires_at=datetime.now(timezone.utc) - timedelta(hours=1), status="active",
                    vault_handle="mock-abc")
    session.add(g)
    session.commit()
    main._sweep_access(session)
    assert session.get(AccessGrant, g.id).status == "expired"
    assert session.scalars(select(AuditLog).where(AuditLog.event == "access.expired")).all()
