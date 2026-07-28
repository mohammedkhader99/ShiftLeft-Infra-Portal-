"""Increment 1.3 checks: draft save/resume and authoritative submit validation.

Runs against a fast in-memory seeded database via a dependency override.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from api.policy import PolicyUnavailable, get_policy_evaluator
from db.seed import seed
from db.session import Base


@pytest.fixture()
def client():
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

    def override_get_session():
        yield session

    # Default: policy allows (real OPA is exercised in Rego tests + live checks).
    def allow_everything():
        return lambda data: {"allow": True, "violations": []}

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_policy_evaluator] = allow_everything
    yield TestClient(app)
    app.dependency_overrides.clear()
    session.close()


def test_submit_blocked_by_policy_returns_reasons(client):
    def deny():
        return lambda data: {"allow": False, "violations": ["Restricted data must stay on-prem."]}

    app.dependency_overrides[get_policy_evaluator] = deny
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422
    assert resp.json()["policy_violations"] == ["Restricted data must stay on-prem."]
    # Still a draft — a blocked request is not submitted.
    assert client.get(f"/api/requests/{ref}").json()["status"] == "draft"


def test_submit_policy_unavailable_returns_503(client):
    def broken():
        def _raise(data):
            raise PolicyUnavailable("connection refused")
        return _raise

    app.dependency_overrides[get_policy_evaluator] = broken
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["policy_error"].lower()


VALID_CREATE = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "deployment_target": "onprem",
    "environment_name": "egate-uat",
    "data_classification": "internal",
    "components": [{"technology_code": "postgres16", "size": "medium"}],
}


def test_save_draft_returns_reference(client):
    resp = client.post("/api/requests/draft", json={"request_type": "create"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reference"].startswith("REQ-")
    assert body["status"] == "draft"
    assert body["requester"]  # stamped with the mock requester


def test_draft_requester_from_header(client):
    # The portal passes the signed-in user via X-Requester (2.3a).
    resp = client.post(
        "/api/requests/draft",
        json={"request_type": "create"},
        headers={"X-Requester": "alice@example.com"},
    )
    assert resp.json()["requester"] == "alice@example.com"


def test_draft_roundtrip_preserves_partial_data(client):
    ref = client.post(
        "/api/requests/draft",
        json={"request_type": "create", "project_code": "EGATE"},
    ).json()["reference"]

    reloaded = client.get(f"/api/requests/{ref}").json()
    assert reloaded["request_type"] == "create"
    assert reloaded["project_code"] == "EGATE"
    # Half-finished is fine for a draft — no components added yet.
    assert reloaded["components"] == []


def test_updating_a_draft_keeps_same_reference(client):
    ref = client.post("/api/requests/draft", json={"request_type": "create"}).json()["reference"]
    updated = client.post(
        "/api/requests/draft", json={"reference": ref, "project_code": "VISA"}
    ).json()
    assert updated["reference"] == ref
    assert updated["project_code"] == "VISA"
    # Not sending components leaves the earlier ones untouched.
    assert updated["request_type"] == "create"


def test_multiple_components_are_saved_and_submit(client):
    payload = {
        **VALID_CREATE,
        "components": [
            {"technology_code": "postgres16", "size": "small"},
            {"technology_code": "nginx", "size": "small"},
            {"technology_code": "redis7", "size": "medium"},
        ],
    }
    ref = client.post("/api/requests/draft", json=payload).json()["reference"]
    reloaded = client.get(f"/api/requests/{ref}").json()
    assert len(reloaded["components"]) == 3
    assert {c["technology_code"] for c in reloaded["components"]} == {
        "postgres16", "nginx", "redis7"
    }
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200


def test_create_with_no_components_is_rejected(client):
    payload = {**VALID_CREATE, "components": []}
    ref = client.post("/api/requests/draft", json=payload).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "components" in errors


def test_component_with_bad_size_is_rejected(client):
    payload = {
        **VALID_CREATE,
        "components": [{"technology_code": "postgres16", "size": "huge"}],
    }
    ref = client.post("/api/requests/draft", json=payload).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "component_0_size" in errors


def test_missing_reference_returns_404(client):
    assert client.get("/api/requests/REQ-9999-9999").status_code == 404


def test_submit_valid_create_succeeds(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"


def test_submit_persists_estimate(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    submitted = client.post(f"/api/requests/{ref}/submit").json()
    # The estimate is returned and, crucially, persisted on the request.
    assert submitted["estimate"]["monthly"] == 672.0  # onprem postgres16 medium
    reloaded = client.get(f"/api/requests/{ref}").json()
    assert reloaded["estimate"] is not None
    assert reloaded["estimate"]["one_time"] == 500.0
    assert reloaded["estimate"]["annual"] == 672.0 * 12
    assert reloaded["estimate"]["currency"] == "AED"


def test_approve_fires_handoff_and_provisions_with_audit(client, monkeypatch):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    approval = client.post(f"/api/requests/{ref}/submit").json()["approval"]
    jira_key = approval["jira_key"]

    # Mock the orchestrator's response to the signed handoff.
    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True, "reference": ref, "verified": {"approval": True, "policy": True}}

    import api.main as main
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())

    result = client.post(f"/api/approvals/{jira_key}/approve").json()
    assert result["provisioned"] is True

    # Request is now provisioned and the audit trail records the chain of events.
    assert client.get(f"/api/requests/{ref}").json()["status"] == "provisioned"
    events = [e["event"] for e in client.get(f"/api/requests/{ref}/audit").json()["entries"]]
    assert events == ["approval.approved", "orchestrator.handoff", "provisioned"]


def test_apply_starts_provisioning_in_progress(client, monkeypatch):
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    class Resp:
        def __init__(self, code, data):
            self.status_code = code
            self._data = data
            self.text = ""
            self.headers = {"content-type": "application/json"}

        def json(self):
            return self._data

    monkeypatch.setattr(
        main.httpx, "post",
        lambda url, *a, **k: Resp(200, {"provisioned": False, "planned": True,
                                        "plan_summary": "Plan: 1 to add"})
        if url.endswith("/provision") else Resp(200, {}),
    )
    # Don't run the real background apply in this unit test.
    monkeypatch.setattr(main, "_provision_in_background", lambda *a, **k: None)

    client.post(f"/api/approvals/{key}/approve")
    assert client.get(f"/api/requests/{ref}").json()["status"] == "planned"

    # Apply returns immediately with in-progress; Jira set to In Progress.
    applied = client.post(f"/api/requests/{ref}/apply").json()
    assert applied["status"] == "in-progress"
    assert client.get(f"/api/requests/{ref}").json()["status"] == "in-progress"
    events = [e["event"] for e in client.get(f"/api/requests/{ref}/audit").json()["entries"]]
    assert "provisioning.started" in events


def test_apply_refused_before_plan(client):
    # A brand-new draft can't be applied — it must be approved+planned first.
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    client.post(f"/api/requests/{ref}/submit")
    resp = client.post(f"/api/requests/{ref}/apply")
    assert resp.status_code == 409


def test_plan_mode_marks_request_planned_not_provisioned(client, monkeypatch):
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": False, "planned": True,
                    "plan_summary": "Plan: 1 to add, 0 to change, 0 to destroy.",
                    "message": "Terraform plan — nothing created."}

    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())
    result = client.post(f"/api/approvals/{key}/approve").json()
    assert result["planned"] is True
    assert result["provisioned"] is False
    # Recorded as 'planned', NOT 'provisioned' — nothing was created.
    assert client.get(f"/api/requests/{ref}").json()["status"] == "planned"
    events = [e["event"] for e in client.get(f"/api/requests/{ref}/audit").json()["entries"]]
    assert "plan.previewed" in events and "provisioned" not in events


def test_approve_is_idempotent(client, monkeypatch):
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    calls = {"n": 0}

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True}

    def counting_post(*a, **k):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(main.httpx, "post", counting_post)
    first = client.post(f"/api/approvals/{key}/approve").json()
    assert first["provisioned"] is True
    # Second approve must NOT hit the orchestrator again.
    second = client.post(f"/api/approvals/{key}/approve").json()
    assert second.get("idempotent") is True
    assert calls["n"] == 1


def test_handoff_retries_transient_failures(client, monkeypatch):
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    calls = {"n": 0}

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "err"

        def json(self):
            return {"provisioned": True}

    def flaky(*a, **k):
        calls["n"] += 1
        return Resp(500) if calls["n"] < 3 else Resp(200)

    monkeypatch.setattr(main.httpx, "post", flaky)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)  # no real backoff wait
    result = client.post(f"/api/approvals/{key}/approve").json()
    assert result["provisioned"] is True
    assert calls["n"] == 3  # two 500s then success


def test_live_approve_blocks_until_jira_approves(client, monkeypatch):
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    # Behave as live Jira; approval status comes from Jira, not our button.
    monkeypatch.setattr(main, "jira_mode", lambda: "live")
    monkeypatch.setattr(main, "get_status", lambda k: "pending")
    pending = client.post(f"/api/approvals/{key}/approve").json()
    assert pending["provisioned"] is False
    assert "not approved" in pending["message"].lower()
    assert client.get(f"/api/requests/{ref}").json()["status"] == "submitted"

    # Once the manager approves in Jira, the handoff proceeds.
    monkeypatch.setattr(main, "get_status", lambda k: "approved")

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True}

    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())
    # get_approval must also reflect the real Jira status now.
    assert client.get(f"/api/approvals/{key}").json()["status"] == "approved"
    done = client.post(f"/api/approvals/{key}/approve").json()
    assert done["provisioned"] is True


def test_audit_chain_hashes_link(client, monkeypatch):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    jira_key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True}

    import api.main as main
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())
    client.post(f"/api/approvals/{jira_key}/approve")

    entries = client.get(f"/api/requests/{ref}/audit").json()["entries"]
    # Each entry (after the first overall) chains to a previous hash.
    for entry in entries:
        assert entry["entry_hash"]


def test_resubmit_does_not_duplicate_the_ticket(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    first = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]
    # A retry (e.g. after a portal timeout) returns the same ticket, not a new one.
    second = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]
    assert first == second


def test_submit_raises_jira_ticket_with_config_and_cost(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    submitted = client.post(f"/api/requests/{ref}/submit").json()
    approval = submitted["approval"]
    assert approval is not None
    assert approval["jira_key"].startswith("INFRA-")
    assert approval["status"] == "pending"
    # The ticket body shows configuration AND cost together (cost-before-approval).
    body = approval["ticket_body"]
    assert ref in body
    assert "postgres16" in body
    assert "Estimated cost" in body
    assert "Plan preview" in body
    # It survives a reload (persisted).
    assert client.get(f"/api/requests/{ref}").json()["approval"]["jira_key"] == approval["jira_key"]


def test_submit_rejects_bad_environment_name_with_rule(client):
    bad = {**VALID_CREATE, "environment_name": "Egate UAT!"}
    ref = client.post("/api/requests/draft", json=bad).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert "environment_name" in errors
    # The message states the rule (F-UX-10).
    assert "lowercase" in errors["environment_name"]


def test_submit_rejects_missing_required_fields(client):
    ref = client.post("/api/requests/draft", json={"request_type": "create"}).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    for field in ("project_code", "environment_name", "data_classification",
                  "cost_centre_code", "components"):
        assert field in errors


def test_submit_unknown_project_is_rejected(client):
    bad = {**VALID_CREATE, "project_code": "NOPE"}
    ref = client.post("/api/requests/draft", json=bad).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "project_code" in errors


def test_submit_add_requires_existing_target(client):
    ref = client.post(
        "/api/requests/draft",
        json={
            "request_type": "add",
            "cost_centre_code": "IMD-1001",
            "deployment_target": "onprem",
            "components": [{"technology_code": "redis7", "size": "small"}],
        },
    ).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "target_environment" in errors

    # A real seeded environment passes.
    client.post(
        "/api/requests/draft",
        json={"reference": ref, "target_environment": "egate-prod"},
    )
    ok = client.post(f"/api/requests/{ref}/submit")
    assert ok.status_code == 200
