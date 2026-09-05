"""AI request drafting checks (F-RPT-06).

Covers the offline mock drafter (keyword→catalog mapping), the catalog-
constraining safety net, the endpoint (RBAC + audit + never-submits), and the
live Claude adapter with the SDK mocked so the suite stays offline.
"""

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_drafter
from api.main import app, get_session
from db.models import AuditLog, Request
from db.seed import seed
from db.session import Base


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


# --- Mock drafter ------------------------------------------------------------

def test_mock_draft_maps_a_full_description_to_the_catalog(session):
    out = ai_drafter.draft_request(
        "I need a medium PostgreSQL database for the eGate UAT environment, "
        "on-prem, internal data.",
        session,
    )
    assert out["mode"] == "mock"
    d = out["draft"]
    assert d["request_type"] == "create"
    assert d["project_code"] == "EGATE"          # matched by the 'egate' token
    assert d["deployment_target"] == "onprem"    # 'on-prem'
    # Drafted as "UAT": the prose says "uat" and the portal's tier is UAT. The
    # drafter produces canonical values, because a draft that needs normalising
    # later is a draft that can fail validation the requester never saw.
    assert d["environment_tier"] == "UAT"        # 'uat' in the description
    assert d["data_classification"] == "internal"
    assert d["environment_name"] == "egate-uat"  # derived from project + tier
    assert d["components"] == [{"technology_code": "postgres16", "size": "medium"}]
    assert d["business_justification"].startswith("I need a medium")


def test_mock_draft_defaults_size_and_notes_the_assumption(session):
    out = ai_drafter.draft_request("a postgres database on azure", session)
    assert out["draft"]["components"] == [{"technology_code": "postgres16", "size": "medium"}]
    assert any("medium" in n.lower() for n in out["notes"])


def test_mock_draft_warns_when_it_cannot_match(session):
    out = ai_drafter.draft_request("something vague with no catalog terms", session)
    assert out["draft"]["components"] == []
    text = " ".join(out["warnings"]).lower()
    assert "technology" in text and "deployment target" in text and "cost centre" in text


def test_mock_draft_matches_multiple_technologies(session):
    out = ai_drafter.draft_request(
        "large kafka and redis cache on OCI for the Visa platform", session
    )
    codes = [c["technology_code"] for c in out["draft"]["components"]]
    assert "kafka" in codes and "redis7" in codes
    assert all(c["size"] == "large" for c in out["draft"]["components"])
    assert out["draft"]["deployment_target"] == "oci"
    assert out["draft"]["project_code"] == "VISA"


# --- Catalog-constraining safety net -----------------------------------------

def test_constrain_drops_values_not_in_the_catalog(session):
    catalog = ai_drafter._load_catalog(session)
    raw = {
        "request_type": "create",
        "project_code": "NOPE",
        "cost_centre_code": "IMD-1001",          # real
        "deployment_target": "onprem",
        "environment_name": "Bad Name!!",        # messy — coerced to the standard
        "environment_tier": "uat",
        "data_classification": "internal",
        "priority": "high",
        "business_criticality": "tier2",
        "business_justification": "x",
        "components": [
            {"technology_code": "obviously-not-a-technology", "size": "medium"},     # not in catalog
            {"technology_code": "postgres16", "size": "huge"},  # bad size
        ],
    }
    out = ai_drafter._constrain(raw, catalog, "mock", [])
    d = out["draft"]
    assert d["project_code"] is None                 # unknown project dropped
    assert d["cost_centre_code"] == "IMD-1001"       # real one kept
    assert d["environment_name"] == "bad-name"       # coerced to the naming standard
    assert d["components"] == [{"technology_code": "postgres16", "size": None}]  # bad size nulled, obviously-not-a-technology dropped
    joined = " ".join(out["warnings"]).lower()
    assert "obviously-not-a-technology" in joined and "nope" in joined


def test_constrain_drops_a_name_it_cannot_coerce(session):
    catalog = ai_drafter._load_catalog(session)
    raw = {"request_type": "create", "environment_name": "!!", "components": []}
    out = ai_drafter._constrain(raw, catalog, "mock", [])
    assert out["draft"]["environment_name"] is None  # nothing valid left after cleanup


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_returns_a_draft_and_creates_nothing(client, session):
    before = session.scalar(select(func.count()).select_from(Request))
    resp = client.post("/api/ai/draft", json={
        "description": "medium postgres for the egate uat environment on-prem, internal",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "mock"
    assert body["draft"]["project_code"] == "EGATE"
    # It only recommends — no request row is created by drafting.
    after = session.scalar(select(func.count()).select_from(Request))
    assert after == before


def test_endpoint_audits_the_draft(client, session):
    client.post("/api/ai/draft", json={"description": "redis cache on azure"})
    rows = session.scalars(select(AuditLog).where(AuditLog.event == "ai.drafted")).all()
    assert len(rows) == 1
    assert rows[0].detail["mode"] == "mock"


def test_endpoint_rejects_too_short(client):
    resp = client.post("/api/ai/draft", json={"description": "db"})
    assert resp.status_code == 422


def test_endpoint_forbidden_for_read_only_role(client, monkeypatch):
    # A read-only user's identity can view but not author requests.
    monkeypatch.setenv("ROLE_MAP", json.dumps({"ro@emaratechg.ae": ["read_only"]}))
    resp = client.post(
        "/api/ai/draft",
        headers={"X-Requester": "ro@emaratechg.ae"},
        json={"description": "postgres on-prem for egate uat"},
    )
    assert resp.status_code == 403


# --- Live adapter (Claude SDK mocked) ----------------------------------------

def _inject_fake_anthropic(monkeypatch, payload: dict, captured: dict | None = None):
    class FakeText:
        type = "text"

        def __init__(self, text):
            self.text = text

    class FakeResp:
        def __init__(self, content):
            self.content = content

    class FakeMessages:
        def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return FakeResp([FakeText(json.dumps(payload))])

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)


def test_live_mode_calls_the_sdk_and_constrains_the_result(session, monkeypatch):
    captured: dict = {}
    _inject_fake_anthropic(monkeypatch, {
        "request_type": "create",
        "project_code": "EGATE",
        "cost_centre_code": "IMD-1001",
        "deployment_target": "onprem",
        "environment_name": "egate-uat",
        "environment_tier": "uat",
        "data_classification": "internal",
        "priority": "high",
        "business_criticality": "tier2",
        "business_justification": "eGate UAT database.",
        "components": [
            {"technology_code": "postgres16", "size": "large"},
            {"technology_code": "obviously-not-a-technology", "size": "small"},  # not in catalog → dropped
        ],
    }, captured)
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = ai_drafter.draft_request("anything", session)
    assert out["mode"] == "live"
    assert out["draft"]["project_code"] == "EGATE"
    # Even the model's output is re-checked against the catalog.
    assert out["draft"]["components"] == [{"technology_code": "postgres16", "size": "large"}]
    # It really went through the structured-output Claude call.
    assert captured["model"]  # a model id was chosen
    assert "format" in captured["output_config"]


def test_live_mode_without_key_is_unavailable(session, monkeypatch):
    _inject_fake_anthropic(monkeypatch, {})
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ai_drafter.AiUnavailable):
        ai_drafter.draft_request("postgres on-prem", session)
