"""AI cost explanation checks (F-RPT-07).

The explainer narrates the authoritative estimate_cost breakdown: it ranks the
cost drivers deterministically and offers advisory tips. Covers the mock path,
the endpoint (audit + creates nothing), and the live Claude path (SDK mocked).
"""

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_explainer
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


def _payload(**over):
    p = {
        "deployment_target": "onprem",
        "components": [{"technology_code": "postgres16", "size": "large"}],
        "advanced_options": {},
    }
    p.update(over)
    return p


# --- Mock explanation --------------------------------------------------------

def test_mock_ranks_drivers_and_summarises(session):
    out = ai_explainer.explain_cost(_payload(), session)
    assert out["mode"] == "mock"
    assert out["monthly"] > 0 and out["known_target"] is True
    # onprem postgres16 large: compute (8*45 + 64*12 = 1128) > storage (500*1.5 = 750).
    assert out["drivers"][0]["category"] == "compute"
    assert {d["category"] for d in out["drivers"]} >= {"compute", "storage"}
    # Shares are percentages of the monthly total.
    assert abs(sum(d["pct"] for d in out["drivers"]) - 100) < 1.0
    assert str(int(out["monthly"])) in out["summary"] and "Compute" in out["summary"]


def test_mock_tips_are_specific_to_the_options(session):
    out = ai_explainer.explain_cost(_payload(advanced_options={
        "high_availability": True,
        "backup_retention": "90",
        "monitoring_level": "enhanced",
    }), session)
    joined = " ".join(out["tips"]).lower()
    assert "high availability" in joined
    assert "backup" in joined
    assert "postgres16" in joined  # the large component is called out
    assert len(out["tips"]) <= 4


def test_mock_flags_licence_costs(session):
    out = ai_explainer.explain_cost(_payload(
        components=[{"technology_code": "mssql", "size": "large"}],
    ), session)
    assert any(d["category"] == "licence" for d in out["drivers"])
    assert "licence" in " ".join(out["tips"]).lower()


def test_no_cost_yet_without_a_target(session):
    out = ai_explainer.explain_cost(_payload(deployment_target=None), session)
    assert out["known_target"] is False
    assert out["drivers"] == []
    assert "deployment target" in out["summary"].lower()


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_explains_and_creates_nothing(client, session):
    before = session.scalar(select(func.count()).select_from(Request))
    resp = client.post("/api/cost/explain", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "mock" and body["drivers"][0]["category"] == "compute"
    assert session.scalar(select(func.count()).select_from(Request)) == before


def test_endpoint_audits_the_explanation(client, session):
    client.post("/api/cost/explain", json=_payload())
    rows = session.scalars(select(AuditLog).where(AuditLog.event == "ai.explained")).all()
    assert len(rows) == 1 and rows[0].detail["mode"] == "mock"


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


def test_live_mode_uses_the_sdk_narrative(session, monkeypatch):
    captured: dict = {}
    _inject_fake_anthropic(monkeypatch, {
        "summary": "Compute dominates this on-prem database.",
        "tips": ["Drop to medium.", "Skip HA for non-prod."],
    }, captured)
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = ai_explainer.explain_cost(_payload(), session)
    assert out["mode"] == "live"
    assert out["summary"] == "Compute dominates this on-prem database."
    assert out["tips"] == ["Drop to medium.", "Skip HA for non-prod."]
    # Drivers are still computed server-side (numbers stay authoritative).
    assert out["drivers"][0]["category"] == "compute"
    assert "format" in captured["output_config"]


def test_live_falls_back_to_mock_when_there_is_no_cost(session, monkeypatch):
    # Live configured, but nothing to price → deterministic path, no SDK call.
    _inject_fake_anthropic(monkeypatch, {"summary": "should not be used", "tips": []})
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    out = ai_explainer.explain_cost(_payload(deployment_target=None), session)
    assert out["mode"] == "mock"
    assert "deployment target" in out["summary"].lower()
