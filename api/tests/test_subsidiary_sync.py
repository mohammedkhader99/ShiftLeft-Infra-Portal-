"""Subsidiary master-data sync from Jira (customfield_36200).

The subsidiary dropdown is populated from a Jira create-screen field, synced
into our Subsidiary table on an interval. Read path (lookups) stays on the DB.
All Jira access is mocked so the suite runs offline.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import jira
from api.main import app, get_session
from db.models import Subsidiary
from db.seed import seed
from db.session import Base

SEEDED = {"EMRTECH", "GDRFAD", "ICP"}  # db/seed.py placeholders (all active)


@pytest.fixture()
def _db():
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
    yield session
    session.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override_get_session():
        yield _db

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _active_codes(session):
    return {s.code for s in session.scalars(select(Subsidiary).where(Subsidiary.active.is_(True)))}


# --- Option parsing ----------------------------------------------------------

def test_option_to_row_handles_select_assets_and_junk():
    assert jira._option_to_row({"id": "10", "value": "Alpha"}) == {"code": "10", "name": "Alpha"}
    # JSM Assets/Insight object shape
    assert jira._option_to_row({"objectKey": "SUB-1", "label": "Beta"}) == {"code": "SUB-1", "name": "Beta"}
    assert jira._option_to_row({"name": "Gamma"}) == {"code": "Gamma", "name": "Gamma"}
    assert jira._option_to_row({"id": "x"}) is None  # no name
    assert jira._option_to_row("nonsense") is None


def test_fetch_reads_allowed_values_from_createmeta(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.test")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "INFRA")
    monkeypatch.setenv("JIRA_ISSUE_TYPE", "Task")

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_get(url, **kw):
        if url.endswith("/issuetypes"):
            return FakeResp({"values": [{"id": "10", "name": "Task"}]})
        if "/issuetypes/10" in url:
            return FakeResp({"values": [{
                "fieldId": "customfield_36200",
                "allowedValues": [
                    {"id": "1", "value": "emaratech FZ-LLC"},
                    {"id": "2", "value": "GDRFA Dubai"},
                    {"id": "1", "value": "dup dropped"},  # duplicate code
                ],
            }]})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(jira.httpx, "get", fake_get)
    rows = jira.fetch_subsidiary_options()
    assert rows == [
        {"code": "1", "name": "emaratech FZ-LLC"},
        {"code": "2", "name": "GDRFA Dubai"},
    ]


def test_fetch_raises_when_field_absent(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.test")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "INFRA")
    monkeypatch.setenv("JIRA_ISSUE_TYPE", "Task")

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_get(url, **kw):
        if url.endswith("/issuetypes"):
            return FakeResp({"values": [{"id": "10", "name": "Task"}]})
        return FakeResp({"values": [{"fieldId": "customfield_99999", "allowedValues": []}]})

    monkeypatch.setattr(jira.httpx, "get", fake_get)
    with pytest.raises(jira.JiraError):
        jira.fetch_subsidiary_options()


# --- Sync reconciliation -----------------------------------------------------

def test_sync_adds_jira_options_and_deactivates_the_rest(session, monkeypatch):
    monkeypatch.setattr(jira, "jira_mode", lambda: "live")
    monkeypatch.setattr(jira, "fetch_subsidiary_options", lambda: [
        {"code": "10001", "name": "emaratech FZ-LLC"},
        {"code": "10002", "name": "GDRFA Dubai"},
    ])
    summary = jira.sync_subsidiaries(session)
    session.commit()

    assert summary["synced"] and summary["added"] == 2 and summary["deactivated"] == len(SEEDED)
    # Only the Jira options are active now; the placeholders are deactivated (not deleted).
    assert _active_codes(session) == {"10001", "10002"}
    assert {s.code for s in session.scalars(select(Subsidiary))} == SEEDED | {"10001", "10002"}


def test_sync_renames_and_reactivates(session, monkeypatch):
    monkeypatch.setattr(jira, "jira_mode", lambda: "live")
    # First sync: one option.
    monkeypatch.setattr(jira, "fetch_subsidiary_options", lambda: [{"code": "10001", "name": "Old Name"}])
    jira.sync_subsidiaries(session)
    session.commit()
    # Second sync: renamed, and a placeholder code comes back (reactivate path).
    monkeypatch.setattr(jira, "fetch_subsidiary_options", lambda: [
        {"code": "10001", "name": "New Name"},
        {"code": "ICP", "name": "ICP"},  # matches a seeded (now-deactivated) code
    ])
    summary = jira.sync_subsidiaries(session)
    session.commit()

    assert summary["updated"] == 1 and summary["reactivated"] == 1
    row = session.scalar(select(Subsidiary).where(Subsidiary.code == "10001"))
    assert row.name == "New Name"
    assert _active_codes(session) == {"10001", "ICP"}


def test_sync_is_a_noop_on_empty_fetch(session, monkeypatch):
    monkeypatch.setattr(jira, "jira_mode", lambda: "live")
    monkeypatch.setattr(jira, "fetch_subsidiary_options", lambda: [])  # Jira returned nothing
    summary = jira.sync_subsidiaries(session)
    session.commit()
    assert summary["synced"] is False and summary["changed"] == 0
    assert _active_codes(session) == SEEDED  # untouched — a blip can't wipe the list


def test_sync_is_a_noop_on_jira_error(session, monkeypatch):
    monkeypatch.setattr(jira, "jira_mode", lambda: "live")

    def boom():
        raise jira.JiraError("unreachable")

    monkeypatch.setattr(jira, "fetch_subsidiary_options", boom)
    summary = jira.sync_subsidiaries(session)
    session.commit()
    assert summary["synced"] is False
    assert _active_codes(session) == SEEDED


def test_sync_is_a_noop_in_mock_mode(session, monkeypatch):
    monkeypatch.setattr(jira, "jira_mode", lambda: "mock")
    summary = jira.sync_subsidiaries(session)
    assert summary["synced"] is False and summary["changed"] == 0
    assert _active_codes(session) == SEEDED


# --- Read path + endpoint ----------------------------------------------------

def test_lookups_only_returns_active_subsidiaries(client, session):
    # Deactivate one placeholder and confirm the dropdown drops it.
    icp = session.scalar(select(Subsidiary).where(Subsidiary.code == "ICP"))
    icp.active = False
    session.commit()
    codes = {s["code"] for s in client.get("/api/lookups").json()["subsidiaries"]}
    assert codes == SEEDED - {"ICP"}


def test_sync_endpoint_requires_platform_admin(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"req@emaratechg.ae": ["requester"]}')
    r = client.post("/api/lookups/subsidiaries/sync", headers={"X-Requester": "req@emaratechg.ae"})
    assert r.status_code == 403


def test_sync_endpoint_runs_for_admin(client):
    # Default mock identity resolves to all roles (incl. platform_admin).
    r = client.post("/api/lookups/subsidiaries/sync")
    assert r.status_code == 200
    assert "synced" in r.json()  # mock mode → {"synced": False, ...}, but the route works
