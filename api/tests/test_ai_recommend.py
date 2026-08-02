"""AI cloud & sizing recommendation checks (E4 — AI Assistant).

Covers the offline mock recommender (keyword→catalogue mapping + eligible-cloud
intersection), the authoritative cross-cloud pricing (computed by the portal, not
the model), the catalogue-constraining safety net, the endpoint (RBAC + audit +
never-submits), and the live Claude adapter with the SDK mocked so the suite
stays offline.
"""

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_recommend
from api.main import app, get_session
from db.models import AuditLog, Request
from db.seed import seed
from db.session import Base

_CLOUDS = {"azure", "oci", "aws", "gcp"}


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


# --- Mock recommender --------------------------------------------------------

def test_mock_recommends_a_stack_and_prices_it_across_clouds(session):
    out = ai_recommend.recommend(
        "a medium PostgreSQL database and a Java API for a new service", session
    )
    assert out["mode"] == "mock"
    codes = [c["technology_code"] for c in out["components"]]
    assert "postgres16" in codes and "java21" in codes
    # A generic stack can run everywhere → every priced target compared.
    assert set(out["eligible_targets"]) >= _CLOUDS
    assert len(out["comparison"]) >= 4
    # Cheapest first, and the recommendation is a cloud that's in the comparison.
    monthlies = [r["monthly"] for r in out["comparison"]]
    assert monthlies == sorted(monthlies)
    assert out["recommended_target"] in _CLOUDS
    assert out["recommended_target"] in out["eligible_targets"]


def test_mock_defaults_size_and_notes_the_assumption(session):
    out = ai_recommend.recommend("a postgres database", session)
    assert out["components"] == [{"technology_code": "postgres16", "size": "medium"}]
    assert any("medium" in n.lower() for n in out["notes"])


def test_recommended_is_the_cheapest_cloud(session):
    out = ai_recommend.recommend("a large postgres database", session)
    cloud_rows = [r for r in out["comparison"] if r["target"] in _CLOUDS]
    cheapest_cloud = min(cloud_rows, key=lambda r: r["monthly"])["target"]
    assert out["recommended_target"] == cheapest_cloud


def test_cloud_scoped_tech_narrows_eligible_targets(session):
    # aws-rds is only offered on AWS, so the whole stack must land on AWS.
    out = ai_recommend.recommend("we want to use aws-rds for storage", session)
    assert [c["technology_code"] for c in out["components"]] == ["aws-rds"]
    assert out["eligible_targets"] == ["aws"]
    assert out["recommended_target"] == "aws"
    assert [r["target"] for r in out["comparison"]] == ["aws"]


def test_mock_warns_when_nothing_matches(session):
    out = ai_recommend.recommend("something vague with no catalogue terms", session)
    assert out["components"] == []
    assert out["comparison"] == []
    assert out["recommended_target"] is None
    assert any("catalogue technology" in w.lower() for w in out["warnings"])


# --- Prices come from the portal, not the model ------------------------------

def test_comparison_matches_the_authoritative_estimator(session):
    from api.pricing import estimate_cost

    out = ai_recommend.recommend("a medium postgres database", session)
    for row in out["comparison"]:
        expected = estimate_cost(out["components"], row["target"], session)
        assert row["monthly"] == expected["totals"]["monthly"]


# --- Catalogue-constraining safety net ---------------------------------------

def test_constrain_drops_tech_not_in_the_catalogue(session):
    catalog = ai_recommend._load_catalog(session)
    raw = [
        {"technology_code": "mysql", "size": "medium"},      # not in catalogue
        {"technology_code": "postgres16", "size": "huge"},   # bad size → medium
        {"technology_code": "java21", "size": "large"},
    ]
    kept, warnings = ai_recommend._constrain_components(raw, catalog)
    assert {"technology_code": "postgres16", "size": "medium"} in kept
    assert {"technology_code": "java21", "size": "large"} in kept
    assert all(c["technology_code"] != "mysql" for c in kept)
    assert any("mysql" in w.lower() for w in warnings)


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_returns_a_recommendation_and_creates_nothing(client, session):
    before = session.scalar(select(func.count()).select_from(Request))
    resp = client.post("/api/ai/recommend", json={
        "description": "a medium postgres database and a java api",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "mock"
    assert body["recommended_target"] in _CLOUDS
    assert body["comparison"]
    # It only recommends — no request row is created.
    after = session.scalar(select(func.count()).select_from(Request))
    assert after == before


def test_endpoint_audits_the_recommendation(client, session):
    client.post("/api/ai/recommend", json={"description": "a redis cache and a node api"})
    rows = session.scalars(
        select(AuditLog).where(AuditLog.event == "ai.recommended")
    ).all()
    assert len(rows) == 1
    assert rows[0].detail["mode"] == "mock"
    assert "recommended_target" in rows[0].detail


def test_endpoint_rejects_too_short(client):
    resp = client.post("/api/ai/recommend", json={"description": "db"})
    assert resp.status_code == 422


def test_endpoint_forbidden_for_read_only_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", json.dumps({"ro@emaratechg.ae": ["read_only"]}))
    resp = client.post(
        "/api/ai/recommend",
        headers={"X-Requester": "ro@emaratechg.ae"},
        json={"description": "a postgres database for a new service"},
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


def test_live_mode_constrains_and_prices_server_side(session, monkeypatch):
    captured: dict = {}
    # The model even quotes a bogus target/price — the portal must ignore both and
    # compute the comparison itself; and it must drop the non-catalogue tech.
    _inject_fake_anthropic(monkeypatch, {
        "components": [
            {"technology_code": "postgres16", "size": "large"},
            {"technology_code": "mysql", "size": "small"},  # not in catalogue → dropped
        ],
        "rationale": "A managed relational database sized for moderate load.",
    }, captured)
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = ai_recommend.recommend("a database for a new service", session)
    assert out["mode"] == "live"
    assert out["components"] == [{"technology_code": "postgres16", "size": "large"}]
    assert out["rationale"].startswith("A managed relational database")
    # The price comparison is the portal's, computed for the constrained stack.
    from api.pricing import estimate_cost
    for row in out["comparison"]:
        assert row["monthly"] == estimate_cost(
            out["components"], row["target"], session
        )["totals"]["monthly"]
    # It really went through the structured-output Claude call.
    assert captured["model"]
    assert "format" in captured["output_config"]


def test_live_mode_without_key_is_unavailable(session, monkeypatch):
    _inject_fake_anthropic(monkeypatch, {})
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ai_recommend.AiUnavailable):
        ai_recommend.recommend("a postgres database", session)
