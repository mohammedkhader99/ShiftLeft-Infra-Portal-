"""Policy catalogue for the admin console (F-GOV-03).

Shows the governance rules OPA currently enforces, with descriptions taken from
each rule's own `# METADATA` annotation.

Two properties matter more than the rendering:
  * descriptions come from the LOADED policy, so they cannot drift from the rule;
  * if OPA is unreachable the page says so, rather than showing an empty list that
    reads as "no rules are enforced".
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import policy
from api.main import app, get_session
from db.seed import seed
from db.session import Base

# A trimmed version of what OPA's /v1/policies actually returns.
_OPA_RESPONSE = {"result": [
    {"id": "policies/infra.rego", "ast": {"annotations": [
        {"scope": "package", "title": "Infrastructure request policy",
         "description": "The governance rules every request is judged against."},
        {"scope": "rule", "title": "Data residency",
         "description": "Restricted data may not leave on-premises.",
         "custom": {"feature": "F-CAT-06", "effect": "block",
                    "applies_to": "restricted data"}},
        {"scope": "rule", "title": "Name an application owner",
         "description": "Advisory only.",
         "custom": {"feature": "F-LCM-10", "effect": "advise",
                    "applies_to": "all provisioning requests"}},
    ]}},
    # The policy's own unit tests are not governance rules and must be filtered.
    {"id": "policies/infra_test.rego", "ast": {"annotations": [
        {"scope": "rule", "title": "test helper", "custom": {"effect": "block"}}]}},
]}


@pytest.fixture()
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)

    def override():
        yield s
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    s.close()


def _fake_opa(monkeypatch, payload=None, fail=False):
    import types

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            if fail:
                raise RuntimeError("connection refused")

        def json(self):
            return payload if payload is not None else _OPA_RESPONSE

    def _get(url, **kw):
        if fail:
            raise RuntimeError("connection refused")
        return _Resp()

    # Stands in for the shared client the policy module asks for, rather than
    # for the httpx module itself — the calls go through one client now.
    monkeypatch.setattr(policy, "http_client",
                        lambda: types.SimpleNamespace(get=_get))


# --- Reading the loaded policy ----------------------------------------------

def test_describes_rules_from_their_annotations(monkeypatch):
    _fake_opa(monkeypatch)
    out = policy.describe_policies()
    titles = [r["title"] for r in out["rules"]]
    assert "Data residency" in titles
    residency = next(r for r in out["rules"] if r["title"] == "Data residency")
    assert residency["feature"] == "F-CAT-06"
    assert residency["effect"] == "block"
    assert "on-premises" in residency["description"]


def test_package_annotation_becomes_the_overview(monkeypatch):
    _fake_opa(monkeypatch)
    out = policy.describe_policies()
    assert out["overview"]["title"] == "Infrastructure request policy"
    # The package annotation is not also listed as a rule.
    assert "Infrastructure request policy" not in [r["title"] for r in out["rules"]]


def test_policy_unit_tests_are_not_shown_as_rules(monkeypatch):
    """infra_test.rego contains the policy's own tests — not governance."""
    _fake_opa(monkeypatch)
    out = policy.describe_policies()
    assert all("_test" not in r["module"] for r in out["rules"])
    assert "test helper" not in [r["title"] for r in out["rules"]]


def test_blocking_rules_are_listed_first_and_counted(monkeypatch):
    _fake_opa(monkeypatch)
    out = policy.describe_policies()
    assert out["blocking"] == 1 and out["advisory"] == 1
    assert out["rules"][0]["effect"] == "block"  # what stops a request comes first


# --- The honesty property ----------------------------------------------------

def test_unreachable_opa_raises_rather_than_reporting_no_rules(monkeypatch):
    _fake_opa(monkeypatch, fail=True)
    with pytest.raises(policy.PolicyUnavailable):
        policy.describe_policies()


def test_endpoint_says_unavailable_instead_of_showing_an_empty_policy_set(client, monkeypatch):
    """An empty list would read as 'nothing is enforced' — the most dangerous
    thing a governance page could imply. It must say it cannot tell."""
    _fake_opa(monkeypatch, fail=True)
    body = client.get("/api/admin/policies").json()
    assert body["available"] is False
    assert body["rules"] == [] and body["blocking"] == 0
    assert body["error"]


# --- Endpoint + access -------------------------------------------------------

def test_endpoint_returns_the_catalogue(client, monkeypatch):
    _fake_opa(monkeypatch)
    body = client.get("/api/admin/policies").json()
    assert body["available"] is True
    assert body["overview"]["title"]
    assert any(r["title"] == "Data residency" for r in body["rules"])


def test_endpoint_is_platform_admin_only(client, monkeypatch):
    _fake_opa(monkeypatch)
    monkeypatch.setenv("ROLE_MAP", '{"ro@emaratechg.ae": ["read_only"]}')
    r = client.get("/api/admin/policies", headers={"X-Requester": "ro@emaratechg.ae"})
    assert r.status_code == 403


# --- Guard: the real policy file must stay annotated ------------------------

def test_every_real_rule_that_emits_a_message_is_annotated():
    """The catalogue is only as complete as the annotations. If someone adds a
    violation/warning rule without a METADATA block, it would enforce silently and
    never appear on the page — so fail here instead."""
    from pathlib import Path
    rego = (Path(__file__).resolve().parents[2] / "policy" / "infra.rego").read_text(encoding="utf-8")
    lines = rego.splitlines()
    unannotated = []
    for i, line in enumerate(lines):
        if line.startswith(("violations contains msg if", "warnings contains msg if")):
            # Walk back over the annotation block directly above the rule.
            j = i - 1
            while j >= 0 and lines[j].startswith("#"):
                j -= 1
            block = "\n".join(lines[j + 1:i])
            if "# METADATA" not in block:
                unannotated.append(f"line {i + 1}: {line}")
    assert not unannotated, (
        "These policy rules emit a message but carry no # METADATA block, so they "
        "would enforce invisibly:\n  " + "\n  ".join(unannotated))
