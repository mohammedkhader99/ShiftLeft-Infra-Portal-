"""Increment 1.3 checks: draft save/resume and authoritative submit validation.

Runs against a fast in-memory seeded database via a dependency override.
"""

from datetime import date, timedelta

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


# Governance metadata required on submit for create/add/resize (increment 6.1).
_FUTURE_DATE = (date.today() + timedelta(days=30)).isoformat()
VALID_METADATA = {
    "business_justification": "Needed to run the eGate UAT load tests before go-live.",
    "priority": "high",
    "business_criticality": "tier2",
    "required_delivery_date": _FUTURE_DATE,
    "application_owner": "app.owner@emaratechg.ae",
    "technical_owner": "tech.owner@emaratechg.ae",
}

VALID_CREATE = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "deployment_target": "onprem",
    "environment_name": "egate-uat",
    "environment_tier": "uat",
    "data_classification": "internal",
    "components": [{"technology_code": "postgres16", "size": "medium"}],
    **VALID_METADATA,
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
            **VALID_METADATA,
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


# --- Automatic poller (increment 2.7) ----------------------------------------

@pytest.fixture()
def poller():
    """A client plus direct access to its DB session, for driving the poller's
    _advance_request the way the background thread would."""
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

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_policy_evaluator] = lambda: (
        lambda data: {"allow": True, "violations": []}
    )
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.clear()
        session.close()


def _submit_request(client, session):
    """Create + submit a request; return the (Request, jira_key)."""
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]
    req = session.scalar(main.select(main.Request).where(main.Request.reference == ref))
    return req, key


def _events(session, reference):
    import api.main as main

    return [
        e.event
        for e in session.scalars(
            main.select(main.AuditLog)
            .where(main.AuditLog.reference == reference)
            .order_by(main.AuditLog.id)
        )
    ]


class _PlanResp:
    status_code = 200
    text = ""

    def json(self):
        return {"provisioned": False, "planned": True, "plan_summary": "Plan: 1 to add"}


def test_poller_auto_approves_and_plans(poller, monkeypatch):
    """Assigned in Jira -> poller marks approved and runs the plan (plan mode)."""
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)

    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")  # stop at the plan

    assert main._advance_request(session, req) == "planned"
    session.refresh(req)
    assert req.status == "planned"
    events = _events(session, req.reference)
    assert "approval.approved" in events and "plan.previewed" in events


def test_poller_marks_rejected(poller, monkeypatch):
    """Rejected in Jira -> poller stops the request, creates nothing."""
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)

    monkeypatch.setattr(main, "get_status", lambda k: "rejected")

    assert main._advance_request(session, req) == "rejected"
    session.refresh(req)
    assert req.status == "rejected"
    assert "approval.rejected" in _events(session, req.reference)


def test_poller_leaves_pending_untouched(poller, monkeypatch):
    """Not yet approved -> poller leaves the request submitted and audits nothing."""
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)

    monkeypatch.setattr(main, "get_status", lambda k: "pending")

    assert main._advance_request(session, req) == "submitted"
    session.refresh(req)
    assert req.status == "submitted"
    assert "approval.approved" not in _events(session, req.reference)


def test_poller_auto_applies_in_apply_mode(poller, monkeypatch):
    """In apply mode the poller goes all the way: plan -> In Progress -> provision."""
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)

    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "apply")
    monkeypatch.setattr(main, "jira_mode", lambda: "mock")  # transition is a no-op audit
    started = {}
    monkeypatch.setattr(main, "_provision_in_background",
                        lambda ref, *a, **k: started.setdefault("ref", ref))

    main._advance_request(session, req)
    assert started.get("ref") == req.reference  # real apply was kicked off
    session.refresh(req)
    assert req.status == "in-progress"  # bg is stubbed, so it stays here
    assert "provisioning.started" in _events(session, req.reference)


def test_poller_disabled_by_default():
    """The master switch is off unless AUTO_PROVISION is explicitly set true."""
    import api.main as main

    assert main.auto_provision_enabled() is False


# --- Apply-failure hardening (increment 2.10) --------------------------------

def test_short_reason_extracts_terraform_error():
    import api.main as main

    blob = ('{"detail":"Terraform apply failed: terraform apply failed: '
            '\\nError: 409-BucketAlreadyExists, the bucket \'test\' already exists'
            '\\nSuggestion: retry\\n"}')
    reason = main._short_reason(blob)
    assert reason.startswith("Error: 409-BucketAlreadyExists")
    assert "already exists" in reason


def test_short_reason_falls_back_to_plain_text():
    import api.main as main

    assert main._short_reason("unreachable: connection refused").startswith("unreachable")
    assert main._short_reason("") == "Provisioning failed."


# --- RBAC enforcement (increment E1.1, F-IAM-01) -----------------------------

def test_me_returns_resolved_roles(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"aud@x.com": ["auditor"]}')
    resp = client.get("/api/me", headers={"X-Requester": "aud@x.com"})
    assert resp.status_code == 200
    assert resp.json() == {"email": "aud@x.com", "roles": ["auditor"]}


def test_apply_refused_without_platform_admin(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    resp = client.post("/api/requests/REQ-2026-0001/apply",
                       headers={"X-Requester": "dev@x.com"})
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"]


def test_execute_allowed_for_platform_admin(client, monkeypatch):
    # A platform admin passes the role gate; the request just doesn't exist yet,
    # so we get 404 (not 403) — proving the gate was cleared.
    monkeypatch.setenv("ROLE_MAP", '{"ops@x.com": ["platform_admin"]}')
    resp = client.post("/api/requests/REQ-9999-9999/apply",
                       headers={"X-Requester": "ops@x.com"})
    assert resp.status_code == 404


def test_create_refused_for_read_only(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@x.com": ["read_only"]}')
    resp = client.post("/api/requests/draft", json=VALID_CREATE,
                       headers={"X-Requester": "ro@x.com"})
    assert resp.status_code == 403


def test_audit_refused_without_audit_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@x.com": ["read_only"]}')
    resp = client.get("/api/requests/REQ-2026-0001/audit",
                      headers={"X-Requester": "ro@x.com"})
    assert resp.status_code == 403


# --- Estate overview stats (increment 2.12, F-RPT-01) ------------------------

def test_stats_refused_without_oversight_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    resp = client.get("/api/stats", headers={"X-Requester": "dev@x.com"})
    assert resp.status_code == 403


def test_stats_aggregates_for_oversight_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"aud@x.com": ["auditor"]}')
    # Two requests (created by the default all-roles mock user).
    client.post("/api/requests/draft", json=VALID_CREATE)
    client.post("/api/requests/draft", json=VALID_CREATE)

    resp = client.get("/api/stats", headers={"X-Requester": "aud@x.com"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["kpis"]["total"] >= 2
    assert {"by_status", "by_type", "by_technology", "by_target", "trend"} <= set(data)
    techs = {b["key"] for b in data["by_technology"]}
    assert "postgres16" in techs


# --- Submitter name + subsidiary (increment 2.14) ----------------------------

def test_draft_captures_requester_name(client):
    resp = client.post("/api/requests/draft", json={"request_type": "create"},
                       headers={"X-Requester-Name": "Mohammed Khader"})
    assert resp.json()["requester_name"] == "Mohammed Khader"


def test_valid_subsidiary_accepted_and_stored(client):
    ref = client.post("/api/requests/draft",
                      json={**VALID_CREATE, "subsidiary": "EMRTECH"}).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert resp.json()["subsidiary"] == "EMRTECH"


def test_unknown_subsidiary_rejected(client):
    ref = client.post("/api/requests/draft",
                      json={**VALID_CREATE, "subsidiary": "NOPE"}).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "subsidiary" in errors


def test_stats_includes_requester_and_subsidiary(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"aud@x.com": ["auditor"]}')
    client.post("/api/requests/draft", json={**VALID_CREATE, "subsidiary": "EMRTECH"},
                headers={"X-Requester-Name": "Mohammed Khader"})
    data = client.get("/api/stats", headers={"X-Requester": "aud@x.com"}).json()
    assert "by_requester" in data and "by_subsidiary" in data
    assert "EMRTECH" in {b["key"] for b in data["by_subsidiary"]}
    assert "Mohammed Khader" in {b["key"] for b in data["by_requester"]}


def test_list_filter_by_subsidiary(client):
    client.post("/api/requests/draft", json={**VALID_CREATE, "subsidiary": "EMRTECH"})
    res = client.get("/api/requests", params={"subsidiary": "EMRTECH"}).json()
    assert res and all(r["subsidiary"] == "EMRTECH" for r in res)


# --- 'My requests' dashboard list endpoint (increment 2.8) --------------------

def test_list_requests_newest_first_and_filtered_by_requester(client):
    r1 = client.post("/api/requests/draft", json=VALID_CREATE,
                     headers={"X-Requester": "alice@example.com"}).json()["reference"]
    r2 = client.post("/api/requests/draft", json=VALID_CREATE,
                     headers={"X-Requester": "bob@example.com"}).json()["reference"]
    r3 = client.post("/api/requests/draft", json=VALID_CREATE,
                     headers={"X-Requester": "alice@example.com"}).json()["reference"]

    # Unfiltered: everyone's, newest first.
    all_refs = [r["reference"] for r in client.get("/api/requests").json()]
    assert all_refs == [r3, r2, r1]

    # Filtered to one requester: only theirs, still newest first.
    alice = client.get("/api/requests", params={"requester": "alice@example.com"}).json()
    assert [r["reference"] for r in alice] == [r3, r1]
    assert all(r["requester"] == "alice@example.com" for r in alice)


def test_list_requests_empty_when_none(client):
    assert client.get("/api/requests", params={"requester": "nobody@example.com"}).json() == []


def test_list_requests_filters_for_drilldown(client):
    client.post("/api/requests/draft", json=VALID_CREATE)  # create · postgres16 · onprem
    client.post("/api/requests/draft", json={
        "request_type": "add", "cost_centre_code": "IMD-1001", "deployment_target": "azure",
        "target_environment": "egate-prod", "components": [{"technology_code": "redis7", "size": "small"}],
    })

    creates = client.get("/api/requests", params={"request_type": "create"}).json()
    assert creates and all(r["request_type"] == "create" for r in creates)

    az = client.get("/api/requests", params={"deployment_target": "azure"}).json()
    assert az and all(r["deployment_target"] == "azure" for r in az)

    pg = client.get("/api/requests", params={"technology": "postgres16"}).json()
    assert pg and all(any(c["technology_code"] == "postgres16" for c in r["components"]) for r in pg)

    # Comma-separated status accepts a set.
    drafts = client.get("/api/requests", params={"status": "draft,provisioned"}).json()
    assert drafts and all(r["status"] == "draft" for r in drafts)


def test_list_requests_status_filter(client, monkeypatch):
    _provision_a_request(client, monkeypatch)  # one provisioned
    client.post("/api/requests/draft", json=VALID_CREATE)  # one draft
    provisioned = client.get("/api/requests", params={"status": "provisioned"}).json()
    assert len(provisioned) == 1
    assert provisioned[0]["status"] == "provisioned"


# --- Decommission by reference (increment 2.9) --------------------------------

def _provision_a_request(client, monkeypatch):
    """Create + submit + approve a request so it ends up provisioned. Returns ref."""
    import api.main as main

    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True, "reference": ref, "verified": {}}

    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())
    client.post(f"/api/approvals/{key}/approve")
    assert client.get(f"/api/requests/{ref}").json()["status"] == "provisioned"
    return ref


def test_decommission_requires_provisioned_source(client):
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission",
        "source_reference": "REQ-9999-9999",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
    }).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "source_reference" in errors


def test_decommission_rejects_tech_not_in_source(client, monkeypatch):
    source = _provision_a_request(client, monkeypatch)  # provisioned with postgres16
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission",
        "source_reference": source,
        "components": [{"technology_code": "redis7", "size": "small"}],  # not in source
    }).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "components" in errors


def test_decommission_needs_at_least_one_technology(client, monkeypatch):
    source = _provision_a_request(client, monkeypatch)
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission", "source_reference": source, "components": [],
    }).json()["reference"]
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "components" in errors


def test_decommission_tears_down_source(client, monkeypatch):
    import api.main as main

    source = _provision_a_request(client, monkeypatch)  # provisioned, postgres16 medium
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission",
        "source_reference": source,
        "components": [{"technology_code": "postgres16", "size": "medium"}],
    }).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit").json()["approval"]["jira_key"]

    class Resp:
        status_code = 200

        def json(self):
            return {"destroyed": True, "reference": source,
                    "summary": "Destroy complete! Resources: 1 destroyed."}

    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())

    result = client.post(f"/api/approvals/{key}/approve").json()
    assert result["decommissioned"] is True
    assert result["source"] == source
    # Both the decommission request and the source it targets are decommissioned.
    assert client.get(f"/api/requests/{ref}").json()["status"] == "decommissioned"
    assert client.get(f"/api/requests/{source}").json()["status"] == "decommissioned"
    events = [e["event"] for e in client.get(f"/api/requests/{ref}/audit").json()["entries"]]
    assert "destroy.handoff" in events and "decommissioned" in events
    # The ticket walks Assigned -> In Progress -> Resolved, like provisioning, so
    # the Resolve transition is reachable (no direct Assigned -> Resolved hop).
    assert "jira.in_progress" in events and "jira.resolved" in events


def test_decommission_ticket_body_lists_technologies(client, monkeypatch):
    source = _provision_a_request(client, monkeypatch)
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission",
        "source_reference": source,
        "components": [{"technology_code": "postgres16", "size": "medium"}],
    }).json()["reference"]
    submitted = client.post(f"/api/requests/{ref}/submit").json()
    body = submitted["approval"]["ticket_body"]
    assert "Decommissioning provisioned request" in body
    assert source in body
    assert "Technologies to decommission" in body


# --- Governance metadata (increment 6.1) -------------------------------------

def _draft_ref(client, payload):
    return client.post("/api/requests/draft", json=payload).json()["reference"]


def test_submit_requires_business_justification(client):
    ref = _draft_ref(client, {**VALID_CREATE, "business_justification": "too short"})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "business_justification" in errors


def test_submit_rejects_unknown_priority(client):
    ref = _draft_ref(client, {**VALID_CREATE, "priority": "urgent"})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "priority" in errors


def test_submit_rejects_unknown_criticality(client):
    ref = _draft_ref(client, {**VALID_CREATE, "business_criticality": "tier9"})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "business_criticality" in errors


def test_submit_rejects_past_delivery_date(client):
    past = (date.today() - timedelta(days=1)).isoformat()
    ref = _draft_ref(client, {**VALID_CREATE, "required_delivery_date": past})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "required_delivery_date" in errors


def test_submit_requires_delivery_date(client):
    ref = _draft_ref(client, {**VALID_CREATE, "required_delivery_date": None})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "required_delivery_date" in errors


def test_metadata_persisted_and_returned(client):
    ref = _draft_ref(client, VALID_CREATE)
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200
    row = client.get(f"/api/requests/{ref}").json()
    assert row["priority"] == "high"
    assert row["business_criticality"] == "tier2"
    assert row["required_delivery_date"] == _FUTURE_DATE
    assert row["application_owner"] == "app.owner@emaratechg.ae"
    assert row["business_justification"].startswith("Needed to run")


def test_ticket_body_includes_request_details(client):
    ref = _draft_ref(client, VALID_CREATE)
    body = client.post(f"/api/requests/{ref}/submit").json()["approval"]["ticket_body"]
    assert "Request details" in body
    assert "Priority: high" in body
    assert "Needed to run" in body


def test_decommission_submits_without_metadata(client, monkeypatch):
    # Decommission carries no governance metadata and must still submit cleanly.
    source = _provision_a_request(client, monkeypatch)
    ref = client.post("/api/requests/draft", json={
        "request_type": "decommission",
        "source_reference": source,
        "components": [{"technology_code": "postgres16", "size": "medium"}],
    }).json()["reference"]
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200


# --- Richer catalog (increment 6.2) ------------------------------------------

def _cost_monthly(client, tech, size="medium", target="onprem"):
    resp = client.post("/api/cost", json={
        "deployment_target": target,
        "components": [{"technology_code": tech, "size": size}],
    })
    assert resp.status_code == 200
    return resp.json()["totals"]["monthly"]


def test_catalog_new_technology_prices(client):
    # A newly-added catalog technology prices via the per-resource rates.
    assert _cost_monthly(client, "mongodb") > 0


def test_catalog_xlarge_prices_more_than_large(client):
    assert _cost_monthly(client, "mongodb", "xlarge") > _cost_monthly(client, "mongodb", "large")


def test_catalog_oracle_licence_adds_to_cost(client):
    # Oracle carries a licence, so it costs more than an unlicensed tech, same size.
    assert _cost_monthly(client, "oracle-db") > _cost_monthly(client, "mongodb")


def test_xlarge_size_is_accepted_on_submit(client):
    ref = _draft_ref(client, {**VALID_CREATE,
                              "components": [{"technology_code": "mongodb", "size": "xlarge"}]})
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200


def test_create_requires_environment_tier(client):
    payload = {k: v for k, v in VALID_CREATE.items() if k != "environment_tier"}
    ref = _draft_ref(client, payload)
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "environment_tier" in errors


def test_create_rejects_unknown_environment_tier(client):
    ref = _draft_ref(client, {**VALID_CREATE, "environment_tier": "staging"})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "environment_tier" in errors


def test_environment_tier_persisted_and_returned(client):
    ref = _draft_ref(client, VALID_CREATE)
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200
    assert client.get(f"/api/requests/{ref}").json()["environment_tier"] == "uat"


# --- Cost breakdown + Excel cost sheet (increment 6.4) -----------------------

def _cost(client, tech, size="medium", target="onprem", advanced=None):
    return client.post("/api/cost", json={
        "deployment_target": target,
        "components": [{"technology_code": tech, "size": size}],
        "advanced_options": advanced or {},
    }).json()


def test_cost_line_splits_compute_and_storage(client):
    # postgres16 medium onprem: compute 4*45 + 16*12 = 372, storage 200*1.5 = 300.
    line = _cost(client, "postgres16")["lines"][0]
    assert line["compute_monthly"] == 372.0
    assert line["storage_monthly"] == 300.0
    assert line["monthly"] == 672.0  # unchanged total


def test_cost_by_category_sums_to_monthly(client):
    body = _cost(client, "postgres16")
    cat = body["by_category"]
    assert cat == {"compute": 372.0, "storage": 300.0, "licence": 0.0,
                   "backup": 0.0, "monitoring": 0.0, "support": 0.0}
    assert round(sum(cat.values()), 2) == body["totals"]["monthly"]


def test_cost_by_category_includes_licence(client):
    # Oracle carries a 1800 licence, surfaced in the licence category.
    assert _cost(client, "oracle-db")["by_category"]["licence"] == 1800.0


def test_costsheet_xlsx_downloads(client):
    ref = _draft_ref(client, VALID_CREATE)
    client.post(f"/api/requests/{ref}/submit")
    resp = client.get(f"/api/requests/{ref}/costsheet.xlsx")
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"  # an .xlsx is a zip archive
    assert "spreadsheetml" in resp.headers["content-type"]
    assert "cost-" in resp.headers.get("content-disposition", "")


def test_costsheet_works_for_a_draft(client):
    # No estimate yet — the endpoint recomputes so drafts still download.
    ref = _draft_ref(client, VALID_CREATE)
    resp = client.get(f"/api/requests/{ref}/costsheet.xlsx")
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"


# --- Advanced options (increment 6.5) ----------------------------------------

def test_defaults_unchanged_without_advanced_options(client):
    # A request with no advanced options keeps the base total.
    assert _cost(client, "postgres16")["totals"]["monthly"] == 672.0


def test_ha_doubles_compute(client):
    base = _cost(client, "postgres16")["by_category"]
    ha = _cost(client, "postgres16", advanced={"high_availability": True})["by_category"]
    assert ha["compute"] == base["compute"] * 2      # 744
    assert ha["storage"] == base["storage"]          # unchanged


def test_backup_retention_adds_backup_line(client):
    base = _cost(client, "postgres16")
    b30 = _cost(client, "postgres16", advanced={"backup_retention": "30"})
    assert b30["by_category"]["backup"] == round(base["by_category"]["storage"] * 0.30, 2)  # 90.0
    assert b30["totals"]["monthly"] == round(base["totals"]["monthly"] + 90.0, 2)


def test_monitoring_and_support_add_cost(client):
    cat = _cost(client, "postgres16",
                advanced={"monitoring_level": "enhanced", "support_tier": "business"})["by_category"]
    assert cat["monitoring"] == 500.0
    # support = 10% of (compute 372 + storage 300 + licence 0 + backup 0 + monitoring 500)
    assert cat["support"] == round((372 + 300 + 500) * 0.10, 2)  # 117.2


def test_advanced_options_persist(client):
    payload = {**VALID_CREATE,
               "advanced_options": {"high_availability": True, "region": "uae-north", "backup_retention": "30"}}
    ref = _draft_ref(client, payload)
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200
    row = client.get(f"/api/requests/{ref}").json()
    assert row["advanced_options"]["region"] == "uae-north"
    assert row["advanced_options"]["high_availability"] is True
    # The captured estimate reflects the HA + backup add-ons.
    assert row["estimate"]["monthly"] > 672.0


def test_invalid_advanced_option_rejected(client):
    ref = _draft_ref(client, {**VALID_CREATE, "advanced_options": {"backup_retention": "999"}})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "advanced.backup_retention" in errors


def test_unknown_advanced_option_rejected(client):
    ref = _draft_ref(client, {**VALID_CREATE, "advanced_options": {"made_up_key": "x"}})
    errors = client.post(f"/api/requests/{ref}/submit").json()["errors"]
    assert "advanced.made_up_key" in errors


# --- Segregation of duties (increment E1.2, F-IAM-03) ------------------------

ALICE = {"X-Requester": "alice@example.com"}
BOB = {"X-Requester": "bob@example.com"}


def _submit_as(client, headers):
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=headers).json()["reference"]
    key = client.post(f"/api/requests/{ref}/submit", headers=headers).json()["approval"]["jira_key"]
    return ref, key


def _orchestrator_ok(monkeypatch, ref):
    import api.main as main

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True, "reference": ref, "verified": {}}

    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: Resp())


def test_sod_blocks_self_approval(client, monkeypatch):
    monkeypatch.setenv("SOD_ENFORCED", "true")
    ref, key = _submit_as(client, ALICE)
    resp = client.post(f"/api/approvals/{key}/approve", headers=ALICE)
    assert resp.status_code == 403
    assert "segregation of duties" in resp.json()["detail"].lower()
    # The block is recorded in the tamper-evident audit trail.
    events = [e["event"] for e in client.get(f"/api/requests/{ref}/audit", headers=ALICE).json()["entries"]]
    assert "sod.blocked" in events


def test_sod_allows_a_different_approver(client, monkeypatch):
    monkeypatch.setenv("SOD_ENFORCED", "true")
    ref, key = _submit_as(client, ALICE)
    _orchestrator_ok(monkeypatch, ref)
    resp = client.post(f"/api/approvals/{key}/approve", headers=BOB)
    assert resp.status_code == 200
    assert resp.json()["provisioned"] is True


def test_sod_blocks_self_apply(client, monkeypatch):
    monkeypatch.setenv("SOD_ENFORCED", "true")
    ref, _ = _submit_as(client, ALICE)
    resp = client.post(f"/api/requests/{ref}/apply", headers=ALICE)
    assert resp.status_code == 403


def test_sod_blocks_self_destroy(client, monkeypatch):
    monkeypatch.setenv("SOD_ENFORCED", "true")
    ref, _ = _submit_as(client, ALICE)
    resp = client.post(f"/api/requests/{ref}/destroy", headers=ALICE)
    assert resp.status_code == 403


def test_sod_off_allows_self_approval(client, monkeypatch):
    monkeypatch.setenv("SOD_ENFORCED", "false")
    ref, key = _submit_as(client, ALICE)
    _orchestrator_ok(monkeypatch, ref)
    resp = client.post(f"/api/approvals/{key}/approve", headers=ALICE)
    assert resp.status_code == 200  # self-approval allowed when SoD is off


def test_poller_is_exempt_from_sod(poller, monkeypatch):
    # The autonomous poller runs _advance_request directly (no human actor), so
    # it is never blocked by SoD even though it advances the requester's own request.
    monkeypatch.setenv("SOD_ENFORCED", "true")
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")
    assert main._advance_request(session, req) == "planned"


# --- Audit chain verification (F-SEC-01) -------------------------------------

def test_audit_verify_endpoint_reports_intact(client, monkeypatch):
    # Provisioning writes several audit events; the chain must verify intact.
    _provision_a_request(client, monkeypatch)
    result = client.get("/api/audit/verify").json()
    assert result["ok"] is True
    assert result["checked"] >= 3  # approval.approved, orchestrator.handoff, provisioned


# --- Approval SLA + escalation (F-GOV-01) -------------------------------------

def test_approval_sla_on_time_after_submit(client):
    ref = _draft_ref(client, VALID_CREATE)
    client.post(f"/api/requests/{ref}/submit")
    sla = client.get(f"/api/requests/{ref}").json()["approval_sla"]
    assert sla is not None
    assert sla["status"] == "on-time"
    assert sla["sla_hours"] == 24.0


def test_approval_sla_breached_when_hours_zero(client, monkeypatch):
    monkeypatch.setenv("APPROVAL_SLA_HOURS", "0")
    ref = _draft_ref(client, VALID_CREATE)
    client.post(f"/api/requests/{ref}/submit")
    assert client.get(f"/api/requests/{ref}").json()["approval_sla"]["status"] == "breached"


def test_approval_sla_absent_for_a_draft(client):
    ref = _draft_ref(client, VALID_CREATE)  # never submitted
    assert client.get(f"/api/requests/{ref}").json()["approval_sla"] is None


def test_stats_reports_breaching_sla(client, monkeypatch):
    monkeypatch.setenv("APPROVAL_SLA_HOURS", "0")
    ref = _draft_ref(client, VALID_CREATE)
    client.post(f"/api/requests/{ref}/submit")
    assert client.get("/api/stats").json()["kpis"]["breaching_sla"] >= 1


def test_sla_escalation_fires_once(poller, monkeypatch):
    monkeypatch.setenv("APPROVAL_SLA_HOURS", "0")  # immediate breach
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)
    session.refresh(req)
    main._escalate_sla(session, req)
    assert req.sla_escalated_at is not None
    assert _events(session, req.reference).count("sla.breached") == 1
    # A second sweep must not escalate again.
    main._escalate_sla(session, req)
    assert _events(session, req.reference).count("sla.breached") == 1


# --- Four-eyes on the Jira approver (F-GOV-08) -------------------------------

def _simulate_live_approval(main, monkeypatch, author):
    """Pretend Jira is live and the ticket is approved by `author` ({email,name})."""
    monkeypatch.setenv("FOUR_EYES_ENFORCED", "true")  # baseline pins it off
    monkeypatch.setattr(main, "jira_mode", lambda: "live")
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "get_approval_author", lambda k: author)
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)  # no real Jira
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")


def test_four_eyes_blocks_self_approval(poller, monkeypatch):
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "self@example.com"
    session.commit()
    _simulate_live_approval(main, monkeypatch, {"email": "self@example.com", "name": None})
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "submitted"  # blocked — not advanced/provisioned
    assert "four-eyes" in (req.status_detail or "").lower()
    events = _events(session, req.reference)
    assert "four_eyes.blocked" in events and "approval.approved" not in events
    # Re-evaluated each cycle, but audited/notified only once.
    main._advance_request(session, req)
    assert _events(session, req.reference).count("four_eyes.blocked") == 1


def test_four_eyes_allows_a_different_approver(poller, monkeypatch):
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "self@example.com"
    session.commit()
    _simulate_live_approval(main, monkeypatch, {"email": "manager@example.com", "name": None})
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "planned"  # a different approver -> proceeds
    events = _events(session, req.reference)
    assert "approval.approved" in events and "four_eyes.blocked" not in events


def test_four_eyes_skipped_in_mock(poller, monkeypatch):
    # Mock mode has no changelog author, so four-eyes is not enforced.
    import api.main as main

    client, session = poller
    req, key = _submit_request(client, session)
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "planned"


# --- Governance evidence pack (F-GOV-10) -------------------------------------

def test_evidence_pack_downloads(client, monkeypatch):
    ref = _provision_a_request(client, monkeypatch)  # a full audit trail
    resp = client.get(f"/api/requests/{ref}/evidence.pdf")
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"
    assert len(resp.content) > 500
    assert "application/pdf" in resp.headers["content-type"]
    assert "evidence-" in resp.headers.get("content-disposition", "")


def test_evidence_pack_works_for_a_draft(client):
    # No estimate/approval yet — the endpoint recomputes cost + reads the trail.
    ref = _draft_ref(client, VALID_CREATE)
    resp = client.get(f"/api/requests/{ref}/evidence.pdf")
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


# --- Change windows (F-GOV-05) -----------------------------------------------

_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_change_window_disabled_is_always_open(monkeypatch):
    import api.main as main
    monkeypatch.setenv("CHANGE_WINDOW_ENABLED", "false")
    assert main.change_window_status()["open"] is True


def test_change_window_respects_days(monkeypatch):
    import api.main as main
    from datetime import datetime, timezone
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)  # noon, inside 08-18
    monkeypatch.setenv("CHANGE_WINDOW_ENABLED", "true")
    monkeypatch.setenv("CHANGE_WINDOW_TZ", "UTC")
    monkeypatch.setenv("CHANGE_WINDOW_START", "08:00")
    monkeypatch.setenv("CHANGE_WINDOW_END", "18:00")
    monkeypatch.setenv("CHANGE_WINDOW_DAYS", _DAY[now.weekday()])       # today allowed
    assert main.change_window_status(now)["open"] is True
    monkeypatch.setenv("CHANGE_WINDOW_DAYS", _DAY[(now.weekday() + 1) % 7])  # not today
    assert main.change_window_status(now)["open"] is False


def test_change_window_respects_hours(monkeypatch):
    import api.main as main
    from datetime import datetime, timezone
    now = datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc)  # 22:00, after hours
    monkeypatch.setenv("CHANGE_WINDOW_ENABLED", "true")
    monkeypatch.setenv("CHANGE_WINDOW_TZ", "UTC")
    monkeypatch.setenv("CHANGE_WINDOW_DAYS", _DAY[now.weekday()])  # today allowed
    monkeypatch.setenv("CHANGE_WINDOW_START", "08:00")
    monkeypatch.setenv("CHANGE_WINDOW_END", "18:00")
    s = main.change_window_status(now)
    assert s["open"] is False
    assert "change window" in s["reason"]


def test_advance_held_outside_change_window(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "change_window_status",
                        lambda now=None: {"open": False, "reason": "outside the change window (allowed mon-fri 08:00-18:00 UTC)"})
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "submitted"  # held — not provisioned
    assert "change window" in (req.status_detail or "")
    events = _events(session, req.reference)
    assert "change_window.held" in events and "provisioned" not in events
    # Re-evaluated each poll, but audited once.
    main._advance_request(session, req)
    assert _events(session, req.reference).count("change_window.held") == 1


def test_advance_provisions_when_change_window_open(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "change_window_status", lambda now=None: {"open": True, "reason": ""})

    class Resp:
        status_code = 200

        def json(self):
            return {"provisioned": True, "reference": req.reference, "verified": {}}

    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (Resp(), None))
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "provisioned"


# --- Policy waivers (F-GOV-02) -----------------------------------------------

def _deny_policy():
    return lambda data: {"allow": False, "violations": ["Restricted data must stay on-prem."]}


def _audit_events(client, ref):
    return [e["event"] for e in client.get(f"/api/requests/{ref}/audit", headers=ALICE).json()["entries"]]


def test_waiver_lets_a_policy_blocked_request_submit(client):
    app.dependency_overrides[get_policy_evaluator] = _deny_policy
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    # Without a waiver the request is blocked.
    assert client.post(f"/api/requests/{ref}/submit", headers=ALICE).status_code == 422
    # A *different* approver documents the exception...
    granted = client.post(f"/api/requests/{ref}/waiver", headers=BOB,
                          json={"reason": "Approved exception CR-1234 for the UAT deadline."})
    assert granted.status_code == 200
    # ...and now the same request submits, with the exception audited.
    resp = client.post(f"/api/requests/{ref}/submit", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"
    assert "policy.waived" in _audit_events(client, ref)


def test_waiver_self_grant_is_blocked(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/waiver", headers=ALICE,
                       json={"reason": "I promise it is fine, let me through."})
    assert resp.status_code == 403
    assert "different approver" in resp.json()["detail"].lower()
    assert "waiver.blocked" in _audit_events(client, ref)


def test_expired_waiver_still_blocks(client):
    app.dependency_overrides[get_policy_evaluator] = _deny_policy
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    past = (date.today() - timedelta(days=1)).isoformat()
    granted = client.post(f"/api/requests/{ref}/waiver", headers=BOB,
                          json={"reason": "Exception that has already lapsed.", "expires_at": past})
    assert granted.status_code == 200
    resp = client.post(f"/api/requests/{ref}/submit", headers=ALICE)
    assert resp.status_code == 422
    assert "policy_violations" in resp.json()


def test_waiver_requires_a_privileged_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@x.com": ["read_only"]}')
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/waiver", headers={"X-Requester": "ro@x.com"},
                       json={"reason": "A read-only user should not be able to do this."})
    assert resp.status_code == 403


def test_waiver_requires_a_meaningful_reason(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/waiver", headers=BOB, json={"reason": "no"})
    assert resp.status_code == 422


def test_waiver_is_recorded_and_visible_on_the_request(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    client.post(f"/api/requests/{ref}/waiver", headers=BOB,
                json={"reason": "Documented exception for the audit trail.",
                      "expires_at": "2026-12-31"})
    got = client.get(f"/api/requests/{ref}").json()
    assert got["waiver"]["granted_by"] == "bob@example.com"
    assert got["waiver"]["reason"] == "Documented exception for the audit trail."
    assert got["waiver"]["expires_at"] == "2026-12-31"
    assert "waiver.granted" in _audit_events(client, ref)


# --- Approval quorum (F-GOV-06) ----------------------------------------------

def _simulate_live_quorum(main, monkeypatch, approvers, quorum):
    """Live + approved in Jira, with `approvers` the distinct people who approved.
    Four-eyes off so quorum is exercised in isolation; stops at the plan."""
    monkeypatch.setenv("APPROVAL_QUORUM", str(quorum))
    monkeypatch.setenv("FOUR_EYES_ENFORCED", "false")
    monkeypatch.setattr(main, "jira_mode", lambda: "live")
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "get_approvers", lambda k: approvers)
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")


def test_quorum_holds_with_too_few_approvers(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "requester@example.com"
    session.commit()
    _simulate_live_quorum(main, monkeypatch,
                          [{"email": "one@example.com", "name": None}], quorum=2)
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "submitted"  # held — only one of two approvers
    assert "1 of 2" in (req.status_detail or "")
    events = _events(session, req.reference)
    assert "quorum.blocked" in events and "provisioned" not in events
    # Re-checked each poll, but audited only once.
    main._advance_request(session, req)
    assert _events(session, req.reference).count("quorum.blocked") == 1


def test_quorum_met_with_enough_distinct_approvers(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "requester@example.com"
    session.commit()
    _simulate_live_quorum(
        main, monkeypatch,
        [{"email": "one@example.com", "name": None},
         {"email": "two@example.com", "name": None}], quorum=2)
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "planned"  # quorum met -> proceeds
    assert "approval.approved" in _events(session, req.reference)


def test_quorum_ignores_duplicate_and_requester_approvals(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "requester@example.com"
    session.commit()
    # Three rows, one distinct non-requester approver: a duplicate + the
    # requester's own approval (which never counts toward quorum).
    _simulate_live_quorum(
        main, monkeypatch,
        [{"email": "one@example.com", "name": None},
         {"email": "one@example.com", "name": None},
         {"email": "requester@example.com", "name": None}], quorum=2)
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "submitted"  # only 1 distinct non-requester approver
    assert "1 of 2" in (req.status_detail or "")


def test_quorum_recovers_when_second_approver_signs(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    req.requester = "requester@example.com"
    session.commit()
    approvers = [{"email": "one@example.com", "name": None}]
    _simulate_live_quorum(main, monkeypatch, approvers, quorum=2)
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "submitted"  # held
    # A second approver signs off -> the next poll proceeds + records quorum.met.
    approvers.append({"email": "two@example.com", "name": None})
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "planned"
    assert "quorum.met" in _events(session, req.reference)


def test_quorum_default_one_is_unaffected(poller, monkeypatch):
    import api.main as main
    client, session = poller
    req, key = _submit_request(client, session)
    # Default quorum (1): no Jira approver read, provisions exactly as before.
    monkeypatch.setattr(main, "get_status", lambda k: "approved")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_PlanResp(), None))
    monkeypatch.setattr(main, "provision_mode", lambda: "plan")
    main._advance_request(session, req)
    session.refresh(req)
    assert req.status == "planned"


# --- Policy-as-code depth: advisory warnings (F-GOV-03) ----------------------

def test_evaluate_policy_surfaces_warnings(monkeypatch):
    import api.policy as policy

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"allow": True, "violations": [],
                               "warnings": ["Name a technical owner."]}}

    monkeypatch.setattr(policy.httpx, "post", lambda *a, **k: Resp())
    verdict = policy.evaluate_policy({"request_type": "create"})
    assert verdict["allow"] is True
    assert verdict["violations"] == []
    assert verdict["warnings"] == ["Name a technical owner."]


def test_policy_warnings_surface_on_submit_without_blocking(client):
    def warn():
        return lambda data: {"allow": True, "violations": [],
                             "warnings": ["Name a technical owner."]}

    app.dependency_overrides[get_policy_evaluator] = warn
    ref = client.post("/api/requests/draft", json=VALID_CREATE, headers=ALICE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"  # warnings never block
    assert resp.json()["policy_warnings"] == ["Name a technical owner."]
    # Recorded in the tamper-evident trail (so it shows in the evidence pack).
    assert "policy.warnings" in _audit_events(client, ref)


def test_submit_without_warnings_returns_empty_list(client):
    # The default allow_everything evaluator returns no warnings.
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert resp.json()["policy_warnings"] == []


# --- Showback & chargeback (E3.2, F-FIN-03) ----------------------------------

def _add_priced(session, ref, *, cost_centre, status, monthly, project="EGATE",
                owner=None, env="env-a"):
    """Insert a priced request (with its captured estimate) directly, so showback
    aggregation can be checked against known figures."""
    import api.main as main

    req = main.Request(reference=ref, status=status, requester="u@x.com",
                       requester_name="U", cost_centre_code=cost_centre,
                       project_code=project, environment_name=env,
                       application_owner=owner)
    req.estimate = main.Estimate(deployment_target="onprem", currency="AED",
                                 one_time=0, monthly=monthly, annual=monthly * 12,
                                 breakdown={})
    session.add(req)
    session.commit()
    return req


def test_showback_sums_monthly_by_cost_centre(poller):
    client, session = poller
    _add_priced(session, "REQ-SB-1", cost_centre="CC-1", status="provisioned", monthly=100)
    _add_priced(session, "REQ-SB-2", cost_centre="CC-1", status="provisioned", monthly=50)
    _add_priced(session, "REQ-SB-3", cost_centre="CC-2", status="provisioned", monthly=200)
    body = client.get("/api/showback?group_by=cost_centre").json()
    by_key = {r["key"]: r for r in body["rows"]}
    assert by_key["CC-1"]["monthly"] == 150.0 and by_key["CC-1"]["count"] == 2
    assert by_key["CC-2"]["monthly"] == 200.0
    assert body["total"]["monthly"] == 350.0
    assert body["rows"][0]["key"] == "CC-2"  # sorted by monthly desc


def test_showback_active_excludes_draft_and_decommissioned(poller):
    client, session = poller
    _add_priced(session, "REQ-SB-4", cost_centre="CC-1", status="provisioned", monthly=100)
    _add_priced(session, "REQ-SB-5", cost_centre="CC-1", status="draft", monthly=999)
    _add_priced(session, "REQ-SB-6", cost_centre="CC-1", status="decommissioned", monthly=999)
    assert client.get("/api/showback?scope=active").json()["total"]["monthly"] == 100.0


def test_showback_committed_includes_in_flight(poller):
    client, session = poller
    _add_priced(session, "REQ-SB-7", cost_centre="CC-1", status="provisioned", monthly=100)
    _add_priced(session, "REQ-SB-8", cost_centre="CC-1", status="submitted", monthly=30)
    assert client.get("/api/showback?scope=active").json()["total"]["monthly"] == 100.0
    assert client.get("/api/showback?scope=committed").json()["total"]["monthly"] == 130.0


def test_showback_groups_by_owner_falling_back_to_requester(poller):
    client, session = poller
    _add_priced(session, "REQ-SB-9", cost_centre="CC-1", status="provisioned", monthly=10, owner="app@x.com")
    _add_priced(session, "REQ-SB-10", cost_centre="CC-1", status="provisioned", monthly=20, owner=None)
    keys = {r["key"] for r in client.get("/api/showback?group_by=owner").json()["rows"]}
    assert "app@x.com" in keys  # application owner used when present
    assert "U" in keys          # requester_name fallback otherwise


def test_showback_reconciles_with_stats_active_cost(poller):
    client, session = poller
    _add_priced(session, "REQ-SB-11", cost_centre="CC-1", status="provisioned", monthly=100)
    _add_priced(session, "REQ-SB-12", cost_centre="CC-2", status="provisioned", monthly=250)
    stats_cost = client.get("/api/stats").json()["active_monthly_cost"]["amount"]
    showback_total = client.get("/api/showback?group_by=cost_centre").json()["total"]["monthly"]
    assert showback_total == stats_cost == 350.0


def test_showback_rejects_bad_group_by(poller):
    client, session = poller
    assert client.get("/api/showback?group_by=nonsense").status_code == 422


def test_showback_requires_oversight_role(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.get("/api/showback", headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- Environment TTL & renewal (E3.1, F-FIN-07) ------------------------------

def _provisioned_with_ttl(session, ref, *, tier="uat", days, owner="owner@x.com"):
    """A provisioned request whose active resource expires in `days` days."""
    import api.main as main
    from datetime import datetime, timezone, timedelta

    req = main.Request(reference=ref, status="provisioned", requester="u@x.com",
                       requester_name="U", environment_tier=tier, environment_name="e1",
                       environment_owner=owner)
    req.approval = main.Approval(jira_key=f"J-{ref}", status="approved")
    req.components = [main.RequestComponent(technology_code="postgres16", size="small")]
    req.estimate = main.Estimate(deployment_target="onprem", currency="AED", one_time=0,
                                 monthly=10, annual=120, breakdown={})
    session.add(req)
    session.add(main.ProvisionedResource(
        reference=ref, kind="oci-bucket", name="b1",
        ttl_expiry=datetime.now(timezone.utc) + timedelta(days=days),
        lifecycle_state="active"))
    session.commit()
    return req


def test_ttl_for_nonprod_gets_expiry_prod_exempt():
    import api.main as main
    from db.models import Request
    assert main._ttl_for(Request(reference="a", requester="u", environment_tier="uat")) is not None
    assert main._ttl_for(Request(reference="b", requester="u", environment_tier="prod")) is None
    assert main._ttl_for(Request(reference="c", requester="u", environment_tier="dr")) is None
    assert main._ttl_for(Request(reference="d", requester="u", environment_tier=None)) is None


def test_ttl_sweep_warns_owner_within_window(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    req = _provisioned_with_ttl(session, "REQ-TTL-1", days=3)  # within the 7-day warn window
    main._sweep_ttls(session)
    session.refresh(req)
    assert req.ttl_notified == "expiring"
    assert "xpir" in (req.status_detail or "")
    assert "ttl.expiring" in _events(session, "REQ-TTL-1")
    # Warned once — a second sweep must not add another event.
    main._sweep_ttls(session)
    assert _events(session, "REQ-TTL-1").count("ttl.expiring") == 1


def test_ttl_sweep_flags_expired_without_enforce(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    req = _provisioned_with_ttl(session, "REQ-TTL-2", days=-1)  # already expired
    main._sweep_ttls(session)
    session.refresh(req)
    assert req.ttl_notified == "expired"
    assert req.status == "provisioned"  # not destroyed — enforcement is off
    assert "ttl.expired" in _events(session, "REQ-TTL-2")
    assert "ttl.decommissioned" not in _events(session, "REQ-TTL-2")


def test_ttl_enforce_decommissions_expired(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setenv("TTL_ENFORCE", "true")
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"summary": "destroyed"}

    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (Resp(), None))
    req = _provisioned_with_ttl(session, "REQ-TTL-3", days=-1)
    main._sweep_ttls(session)
    session.refresh(req)
    assert req.status == "decommissioned"
    assert "ttl.decommissioned" in _events(session, "REQ-TTL-3")


def test_ttl_renew_extends_and_clears_flag(poller):
    import api.main as main
    from datetime import datetime, timezone
    client, session = poller
    req = _provisioned_with_ttl(session, "REQ-TTL-4", days=-1)
    req.ttl_notified = "expired"
    req.status_detail = "Expired on 2026-01-01."
    session.commit()
    resp = client.post("/api/requests/REQ-TTL-4/renew?days=30")
    assert resp.status_code == 200
    assert resp.json()["renewed"] is True
    session.refresh(req)
    assert req.ttl_notified is None
    assert req.status_detail is None
    assert "ttl.renewed" in _events(session, "REQ-TTL-4")
    # The active resource's expiry now sits in the future.
    res = session.scalar(main.select(main.ProvisionedResource).where(
        main.ProvisionedResource.reference == "REQ-TTL-4"))
    exp = res.ttl_expiry
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp > datetime.now(timezone.utc)


def test_ttl_exposed_on_request(poller):
    client, session = poller
    _provisioned_with_ttl(session, "REQ-TTL-5", days=3)
    ttl = client.get("/api/requests/REQ-TTL-5").json()["ttl"]
    assert ttl is not None and ttl["status"] == "expiring"
    row = next(r for r in client.get("/api/requests").json() if r["reference"] == "REQ-TTL-5")
    assert row["ttl"]["status"] == "expiring"


def test_renew_requires_execute_role(poller, monkeypatch):
    client, session = poller
    _provisioned_with_ttl(session, "REQ-TTL-6", days=3)
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    resp = client.post("/api/requests/REQ-TTL-6/renew", headers={"X-Requester": "dev@x.com"})
    assert resp.status_code == 403


def test_renew_nothing_to_renew(poller):
    client, session = poller
    # A provisioned request with no TTL resource (e.g. a prod env).
    import api.main as main
    req = main.Request(reference="REQ-TTL-7", status="provisioned", requester="u@x.com")
    session.add(req)
    session.commit()
    assert client.post("/api/requests/REQ-TTL-7/renew").status_code == 400


# --- Budget guardrails (E3.3, F-FIN-02) --------------------------------------

def test_budget_status_thresholds(poller):
    import api.main as main
    from db.models import Budget
    client, session = poller
    session.add(Budget(cost_centre_code="CC-B", monthly_limit=1000, currency="AED"))
    session.commit()
    assert main._budget_status(session, "CC-B", 500)["status"] == "ok"
    assert main._budget_status(session, "CC-B", 950)["status"] == "near"  # >= 90%
    over = main._budget_status(session, "CC-B", 1200)
    assert over["status"] == "over" and over["remaining"] == -200.0
    assert main._budget_status(session, "NO-BUDGET", 999) is None  # ungated


def test_committed_spend_counts_committed_only(poller):
    import api.main as main
    client, session = poller
    _add_priced(session, "REQ-B-1", cost_centre="CC-B", status="provisioned", monthly=100)
    _add_priced(session, "REQ-B-2", cost_centre="CC-B", status="submitted", monthly=40)
    _add_priced(session, "REQ-B-3", cost_centre="CC-B", status="draft", monthly=999)
    _add_priced(session, "REQ-B-4", cost_centre="CC-B", status="decommissioned", monthly=999)
    assert main._committed_spend(session, "CC-B") == 140.0
    assert main._committed_spend(session, "CC-B", exclude_ref="REQ-B-2") == 100.0


def test_budget_set_list_delete(client):
    assert client.post("/api/budgets",
                       json={"cost_centre_code": "IMD-1001", "monthly_limit": 5000}).status_code == 200
    row = next(b for b in client.get("/api/budgets").json()["budgets"] if b["cost_centre"] == "IMD-1001")
    assert row["limit"] == 5000 and row["remaining"] == 5000  # no committed spend yet
    assert client.delete("/api/budgets/IMD-1001").status_code == 200
    assert client.get("/api/budgets").json()["budgets"] == []
    assert client.delete("/api/budgets/IMD-1001").status_code == 404


def test_budget_over_blocks_when_enforced(client, monkeypatch):
    monkeypatch.setenv("BUDGET_ENFORCE", "true")
    client.post("/api/budgets", json={"cost_centre_code": "IMD-1001", "monthly_limit": 1})
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422
    assert "budget" in resp.json()["budget_error"].lower()
    assert client.get(f"/api/requests/{ref}").json()["status"] == "draft"  # not submitted


def test_budget_over_warns_when_not_enforced(client, monkeypatch):
    monkeypatch.setenv("BUDGET_ENFORCE", "false")
    client.post("/api/budgets", json={"cost_centre_code": "IMD-1001", "monthly_limit": 1})
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200  # warned, not blocked
    assert any("budget" in w.lower() for w in resp.json()["policy_warnings"])


def test_budget_undefined_cost_centre_ungated(client):
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert not any("budget" in w.lower() for w in resp.json()["policy_warnings"])


def test_budget_set_requires_admin(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    resp = client.post("/api/budgets", json={"cost_centre_code": "X", "monthly_limit": 10},
                       headers={"X-Requester": "dev@x.com"})
    assert resp.status_code == 403


def test_budgets_list_requires_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.get("/api/budgets", headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- Quota management (E3.9, F-FIN-08) ----------------------------------------

def _create_env(session, ref, *, project, status="provisioned", request_type="create"):
    import api.main as main
    req = main.Request(reference=ref, status=status, requester="u@x.com",
                       request_type=request_type, project_code=project,
                       environment_name=ref.lower())
    session.add(req)
    session.commit()
    return req


def test_quota_status_thresholds(poller):
    import api.main as main
    from db.models import Quota
    client, session = poller
    session.add(Quota(project_code="P1", max_environments=2))
    session.commit()
    assert main._quota_status(session, "P1", 1)["status"] == "ok"
    assert main._quota_status(session, "P1", 2)["status"] == "near"
    over = main._quota_status(session, "P1", 3)
    assert over["status"] == "over" and over["remaining"] == -1
    assert main._quota_status(session, "NOPE", 5) is None  # ungated


def test_environment_count_create_committed_only(poller):
    import api.main as main
    client, session = poller
    _create_env(session, "E1", project="P1", status="provisioned")
    _create_env(session, "E2", project="P1", status="submitted")
    _create_env(session, "E3", project="P1", status="draft")            # not committed
    _create_env(session, "E4", project="P1", status="decommissioned")   # not committed
    _create_env(session, "E5", project="P1", status="provisioned", request_type="add")  # not create
    assert main._environment_count(session, "P1") == 2


def test_quota_set_list_delete(client):
    assert client.post("/api/quotas",
                       json={"project_code": "EGATE", "max_environments": 5}).status_code == 200
    row = next(q for q in client.get("/api/quotas").json()["quotas"] if q["project"] == "EGATE")
    assert row["limit"] == 5 and row["current"] == 0 and row["remaining"] == 5
    assert client.delete("/api/quotas/EGATE").status_code == 200
    assert client.get("/api/quotas").json()["quotas"] == []
    assert client.delete("/api/quotas/EGATE").status_code == 404


def test_quota_over_blocks_when_enforced(poller, monkeypatch):
    client, session = poller
    monkeypatch.setenv("QUOTA_ENFORCE", "true")
    client.post("/api/quotas", json={"project_code": "EGATE", "max_environments": 1})
    _create_env(session, "EX-1", project="EGATE", status="provisioned")  # fills the quota
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422 and "quota" in resp.json()["quota_error"].lower()
    assert client.get(f"/api/requests/{ref}").json()["status"] == "draft"


def test_quota_over_warns_when_not_enforced(poller, monkeypatch):
    client, session = poller
    monkeypatch.setenv("QUOTA_ENFORCE", "false")
    client.post("/api/quotas", json={"project_code": "EGATE", "max_environments": 1})
    _create_env(session, "EX-2", project="EGATE", status="provisioned")
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert any("quota" in w.lower() for w in resp.json()["policy_warnings"])


def test_quota_undefined_project_ungated(poller):
    client, session = poller
    ref = client.post("/api/requests/draft", json=VALID_CREATE).json()["reference"]  # EGATE, no quota
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 200
    assert not any("quota" in w.lower() for w in resp.json()["policy_warnings"])


def test_quota_set_requires_admin(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.post("/api/quotas", json={"project_code": "X", "max_environments": 1},
                       headers={"X-Requester": "dev@x.com"}).status_code == 403


def test_quotas_list_requires_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.get("/api/quotas", headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- Actual-vs-estimate variance (E3.5, F-FIN-01) ----------------------------

def _priced_provisioned(session, ref, *, monthly, cost_centre="CC-V"):
    import api.main as main
    req = main.Request(reference=ref, status="provisioned", requester="u@x.com",
                       requester_name="U", cost_centre_code=cost_centre, environment_name="e1")
    req.approval = main.Approval(jira_key=f"J-{ref}", status="approved")
    req.estimate = main.Estimate(deployment_target="onprem", currency="AED", one_time=0,
                                 monthly=monthly, annual=monthly * 12, breakdown={})
    session.add(req)
    session.commit()
    return req


def test_variance_status_helper():
    import api.main as main
    assert main._variance_status(100, 120)["status"] == "over"       # +20% > 15
    assert main._variance_status(100, 80)["status"] == "under"       # -20% < -15
    assert main._variance_status(100, 105)["status"] == "on-track"   # +5%
    assert main._variance_status(100, 120)["variance_pct"] == 20.0
    assert main._variance_status(None, 100) is None
    assert main._variance_status(100, None) is None


def test_record_actual_computes_and_exposes_variance(poller):
    client, session = poller
    _priced_provisioned(session, "REQ-V-1", monthly=100)
    resp = client.post("/api/requests/REQ-V-1/actual", json={"billed_monthly": 110})
    assert resp.status_code == 200
    assert resp.json()["variance_pct"] == 10.0 and resp.json()["status"] == "on-track"
    assert "actual.recorded" in _events(session, "REQ-V-1")
    v = client.get("/api/requests/REQ-V-1").json()["variance"]
    assert v["actual"] == 110.0 and v["estimate"] == 100.0


def test_variance_alert_fires_once_over_threshold(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    _priced_provisioned(session, "REQ-V-2", monthly=100)
    client.post("/api/requests/REQ-V-2/actual", json={"billed_monthly": 130})  # +30%
    assert _events(session, "REQ-V-2").count("variance.alert") == 1
    req = session.scalar(main.select(main.Request).where(main.Request.reference == "REQ-V-2"))
    assert "variance" in (req.status_detail or "").lower()
    # Same figure again -> no second alert; a changed figure re-arms it.
    client.post("/api/requests/REQ-V-2/actual", json={"billed_monthly": 130})
    assert _events(session, "REQ-V-2").count("variance.alert") == 1
    client.post("/api/requests/REQ-V-2/actual", json={"billed_monthly": 150})
    assert _events(session, "REQ-V-2").count("variance.alert") == 2


def test_record_actual_under_estimate_is_under(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    _priced_provisioned(session, "REQ-V-3", monthly=100)
    assert client.post("/api/requests/REQ-V-3/actual", json={"billed_monthly": 70}).json()["status"] == "under"


def test_variance_report_totals(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    _priced_provisioned(session, "REQ-V-4", monthly=100)
    _priced_provisioned(session, "REQ-V-5", monthly=200)
    client.post("/api/requests/REQ-V-4/actual", json={"billed_monthly": 150})  # +50%
    client.post("/api/requests/REQ-V-5/actual", json={"billed_monthly": 210})  # +5%
    rep = client.get("/api/variance").json()
    assert rep["total"]["estimate"] == 300.0 and rep["total"]["actual"] == 360.0
    assert rep["rows"][0]["reference"] == "REQ-V-4"  # biggest drift first (+50%)


def test_record_actual_requires_estimate(poller):
    import api.main as main
    client, session = poller
    session.add(main.Request(reference="REQ-V-6", status="provisioned", requester="u@x.com"))
    session.commit()
    assert client.post("/api/requests/REQ-V-6/actual", json={"billed_monthly": 10}).status_code == 400


def test_variance_endpoints_require_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    h = {"X-Requester": "dev@x.com"}
    assert client.get("/api/variance", headers=h).status_code == 403
    assert client.post("/api/requests/X/actual", json={"billed_monthly": 1}, headers=h).status_code == 403


# --- Ownership transfer & orphan detection (E3.7, F-LCM-10) ------------------

def _provisioned_owner(session, ref, *, environment_owner=None, application_owner=None,
                       requester="req@x.com"):
    import api.main as main
    req = main.Request(reference=ref, status="provisioned", requester=requester,
                       environment_owner=environment_owner, application_owner=application_owner,
                       environment_name="e1")
    req.approval = main.Approval(jira_key=f"J-{ref}", status="approved")
    session.add(req)
    session.commit()
    return req


def test_resolve_owner_and_orphan(monkeypatch):
    import api.main as main
    from db.models import Request
    r1 = Request(reference="a", requester="req@x.com", application_owner="app@x.com",
                 environment_owner="env@x.com")
    assert main._resolve_owner(r1) == "env@x.com"       # environment owner wins
    r2 = Request(reference="b", requester="req@x.com")
    assert main._resolve_owner(r2) == "req@x.com"       # falls back to requester
    assert main._is_orphan(r2) is False                 # has a resolvable owner
    monkeypatch.setenv("DEPARTED_OWNERS", "req@x.com, gone@x.com")
    assert main._is_orphan(r2) is True                  # effective owner has left


def test_transfer_owner_reassigns_and_audits(poller):
    client, session = poller
    _provisioned_owner(session, "REQ-O-1", environment_owner="old@x.com")
    resp = client.post("/api/requests/REQ-O-1/transfer-owner", json={"new_owner": "new@x.com"})
    assert resp.status_code == 200
    assert resp.json()["from"] == "old@x.com" and resp.json()["to"] == "new@x.com"
    assert "ownership.transferred" in _events(session, "REQ-O-1")
    assert client.get("/api/requests/REQ-O-1").json()["owner"] == "new@x.com"


def test_orphans_lists_departed_owner_environments(poller, monkeypatch):
    client, session = poller
    monkeypatch.setenv("DEPARTED_OWNERS", "gone@x.com")
    _provisioned_owner(session, "REQ-O-2", environment_owner="gone@x.com")
    _provisioned_owner(session, "REQ-O-3", environment_owner="here@x.com")
    refs = {o["reference"] for o in client.get("/api/orphans").json()["orphans"]}
    assert "REQ-O-2" in refs and "REQ-O-3" not in refs


def test_orphan_sweep_flags_once_and_transfer_clears(poller, monkeypatch):
    import api.main as main
    client, session = poller
    monkeypatch.setenv("DEPARTED_OWNERS", "gone@x.com")
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    req = _provisioned_owner(session, "REQ-O-4", environment_owner="gone@x.com")
    main._sweep_orphans(session)
    session.refresh(req)
    assert req.orphaned_at is not None and "orphan" in (req.status_detail or "").lower()
    assert _events(session, "REQ-O-4").count("ownership.orphaned") == 1
    main._sweep_orphans(session)  # flagged once only
    assert _events(session, "REQ-O-4").count("ownership.orphaned") == 1
    # Reassigning to a present owner clears the flag.
    client.post("/api/requests/REQ-O-4/transfer-owner", json={"new_owner": "new@x.com"})
    session.refresh(req)
    assert req.orphaned_at is None


def test_owner_and_orphaned_exposed(poller, monkeypatch):
    client, session = poller
    monkeypatch.setenv("DEPARTED_OWNERS", "gone@x.com")
    _provisioned_owner(session, "REQ-O-5", environment_owner="gone@x.com")
    row = client.get("/api/requests/REQ-O-5").json()
    assert row["owner"] == "gone@x.com" and row["orphaned"] is True


def test_transfer_owner_requires_admin(poller, monkeypatch):
    client, session = poller
    _provisioned_owner(session, "REQ-O-6", environment_owner="old@x.com")
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    resp = client.post("/api/requests/REQ-O-6/transfer-owner", json={"new_owner": "new@x.com"},
                       headers={"X-Requester": "dev@x.com"})
    assert resp.status_code == 403


def test_orphans_requires_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.get("/api/orphans", headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- Admin console (E1, F-OPS-09) --------------------------------------------

def test_config_returns_posture(client):
    cfg = client.get("/api/config").json()
    assert set(cfg) >= {"modes", "governance", "change_window", "finops"}
    assert cfg["governance"]["approval_quorum"] == 1     # conftest default
    assert cfg["finops"]["budget_enforce"] is False      # conftest pin
    assert cfg["finops"]["ttl_enforce"] is False         # conftest pin
    assert "variance_alert_pct" in cfg["finops"]


def test_config_requires_platform_admin(client, monkeypatch):
    # finops is an oversight role but not platform_admin -> refused.
    monkeypatch.setenv("ROLE_MAP", '{"fin@x.com": ["finops"]}')
    assert client.get("/api/config", headers={"X-Requester": "fin@x.com"}).status_code == 403
    monkeypatch.setenv("ROLE_MAP", '{"ops@x.com": ["platform_admin"]}')
    assert client.get("/api/config", headers={"X-Requester": "ops@x.com"}).status_code == 200


# --- Environment health score (E3.8, F-LCM-08) -------------------------------

def _prov_health(session, ref, *, application_owner="app@x.com", advanced_options=None,
                 requester="req@x.com"):
    import api.main as main
    req = main.Request(reference=ref, status="provisioned", requester=requester,
                       application_owner=application_owner, environment_name="e1",
                       advanced_options=advanced_options or {})
    session.add(req)
    session.commit()
    return req


_GOVERNED = {"backup_retention": "30", "monitoring_level": "enhanced"}


def test_health_clean_env_scores_a(poller):
    client, session = poller
    _prov_health(session, "REQ-H-1", advanced_options=_GOVERNED)
    h = client.get("/api/requests/REQ-H-1").json()["health"]
    assert h["score"] == 100 and h["grade"] == "A" and h["factors"] == []


def test_health_deducts_for_governance_gaps(poller):
    client, session = poller
    _prov_health(session, "REQ-H-2", application_owner=None, advanced_options={})
    h = client.get("/api/requests/REQ-H-2").json()["health"]
    signals = {f["signal"] for f in h["factors"]}
    assert signals >= {"no-backup", "weak-monitoring", "no-owner-named"}
    assert h["score"] == 80 and h["grade"] == "B"  # -10 -5 -5


def test_health_orphan_and_expired_ttl(poller, monkeypatch):
    import api.main as main
    from datetime import datetime, timezone, timedelta
    client, session = poller
    monkeypatch.setenv("DEPARTED_OWNERS", "gone@x.com")
    req = main.Request(reference="REQ-H-3", status="provisioned", requester="req@x.com",
                       environment_owner="gone@x.com", application_owner="app@x.com",
                       environment_name="e1", advanced_options=_GOVERNED)
    session.add(req)
    session.add(main.ProvisionedResource(reference="REQ-H-3", kind="b", name="b",
        ttl_expiry=datetime.now(timezone.utc) - timedelta(days=1), lifecycle_state="active"))
    session.commit()
    h = client.get("/api/requests/REQ-H-3").json()["health"]
    signals = {f["signal"] for f in h["factors"]}
    assert "orphaned" in signals and "ttl-expired" in signals
    assert h["score"] == 55 and h["grade"] == "D"  # -20 -25


def test_health_deducts_for_high_iac_findings(poller):
    import api.main as main
    client, session = poller
    _prov_health(session, "REQ-H-4", advanced_options=_GOVERNED)
    main.append_audit(session, "scan.findings", reference="REQ-H-4",
                      detail={"counts": {"high": 2, "medium": 0, "low": 0}})
    session.commit()
    h = client.get("/api/requests/REQ-H-4").json()["health"]
    assert any(f["signal"] == "iac-high" for f in h["factors"]) and h["score"] == 80


def test_health_deducts_for_cost_overrun(poller):
    import api.main as main
    client, session = poller
    req = main.Request(reference="REQ-H-5", status="provisioned", requester="req@x.com",
                       application_owner="app@x.com", environment_name="e1",
                       advanced_options=_GOVERNED)
    req.estimate = main.Estimate(deployment_target="onprem", currency="AED", one_time=0,
                                 monthly=100, annual=1200, breakdown={})
    session.add(req)
    session.add(main.ActualCost(reference="REQ-H-5", billed_monthly=150))  # +50%
    session.commit()
    h = client.get("/api/requests/REQ-H-5").json()["health"]
    assert any(f["signal"] == "cost-over" for f in h["factors"]) and h["score"] == 90


def test_health_scores_endpoint_orders_worst_first(poller):
    client, session = poller
    _prov_health(session, "REQ-H-6", advanced_options=_GOVERNED)               # 100
    _prov_health(session, "REQ-H-7", application_owner=None, advanced_options={})  # 80
    rep = client.get("/api/health-scores").json()
    assert rep["count"] == 2 and rep["average"] == 90.0
    assert rep["scores"][0]["reference"] == "REQ-H-7"  # worst first


def test_health_none_for_non_provisioned(poller):
    import api.main as main
    client, session = poller
    session.add(main.Request(reference="REQ-H-8", status="draft", requester="req@x.com"))
    session.commit()
    assert client.get("/api/requests/REQ-H-8").json()["health"] is None


def test_health_scores_requires_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.get("/api/health-scores", headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- Drift detection (E3.10, F-LCM-09) ---------------------------------------

def _provisioned_for_drift(session, ref):
    import api.main as main
    req = main.Request(reference=ref, status="provisioned", requester="u@x.com",
                       project_code="EGATE", environment_name="e1")
    req.approval = main.Approval(jira_key=f"J-{ref}", status="approved")
    req.estimate = main.Estimate(deployment_target="onprem", currency="AED", one_time=0,
                                 monthly=10, annual=120, breakdown={})
    session.add(req)
    session.commit()
    return req


class _DriftResp:
    status_code = 200

    def __init__(self, drift):
        self._drift = drift

    def json(self):
        if self._drift:
            return {"drift": True, "count": 1, "summary": "Plan: 0 to add, 1 to change, 0 to destroy.",
                    "changes": [{"address": "oci_objectstorage_bucket.env", "actions": ["update"]}]}
        return {"drift": False, "count": 0, "changes": [], "summary": "No changes."}


def test_drift_check_detects_and_exposes(poller, monkeypatch):
    import api.main as main
    client, session = poller
    _provisioned_for_drift(session, "REQ-D-1")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_DriftResp(True), None))
    resp = client.post("/api/requests/REQ-D-1/drift-check")
    assert resp.status_code == 200 and resp.json()["drift"] is True
    assert "drift.detected" in _events(session, "REQ-D-1")
    got = client.get("/api/requests/REQ-D-1").json()
    assert got["drift"]["detected"] is True and got["drift"]["checked_at"]
    assert "drift" in (got["status_detail"] or "").lower()


def test_drift_check_none(poller, monkeypatch):
    import api.main as main
    client, session = poller
    _provisioned_for_drift(session, "REQ-D-2")
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (_DriftResp(False), None))
    resp = client.post("/api/requests/REQ-D-2/drift-check")
    assert resp.status_code == 200 and resp.json()["drift"] is False
    assert "drift.none" in _events(session, "REQ-D-2")
    assert client.get("/api/requests/REQ-D-2").json()["drift"]["detected"] is False


def test_drift_check_requires_provisioned(poller):
    import api.main as main
    client, session = poller
    session.add(main.Request(reference="REQ-D-3", status="draft", requester="u@x.com"))
    session.commit()
    assert client.post("/api/requests/REQ-D-3/drift-check").status_code == 400


def test_drift_check_requires_oversight(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"dev@x.com": ["requester"]}')
    assert client.post("/api/requests/X/drift-check",
                       headers={"X-Requester": "dev@x.com"}).status_code == 403


# --- API keys / programmatic access (E1, F-INT-01) ---------------------------

def test_api_key_authenticates_as_issuer(client):
    created = client.post("/api/api-keys", json={"label": "ci"}, headers=ALICE)
    assert created.status_code == 200
    key = created.json()["key"]
    assert key.startswith("sk-infra-")
    # Using the key (no X-Requester) authenticates as alice.
    me = client.get("/api/me", headers={"X-API-Key": key})
    assert me.status_code == 200 and me.json()["email"] == "alice@example.com"


def test_api_key_listing_never_shows_the_secret(client):
    client.post("/api/api-keys", json={"label": "x"}, headers=ALICE)
    listing = client.get("/api/api-keys", headers=ALICE).json()["keys"]
    assert listing and all("key" not in k for k in listing)


def test_api_key_inherits_owner_rbac(client, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"ro@x.com": ["read_only"]}')
    ro = {"X-Requester": "ro@x.com"}
    key = client.post("/api/api-keys", json={"label": "ro"}, headers=ro).json()["key"]
    # The key acts as a read-only user -> can't view the estate overview.
    assert client.get("/api/stats", headers={"X-API-Key": key}).status_code == 403


def test_invalid_api_key_is_rejected(client):
    assert client.get("/api/me", headers={"X-API-Key": "sk-infra-nope"}).status_code == 401


def test_revoked_api_key_is_rejected(client):
    created = client.post("/api/api-keys", json={"label": "x"}, headers=ALICE).json()
    assert client.delete(f"/api/api-keys/{created['id']}", headers=ALICE).status_code == 200
    assert client.get("/api/me", headers={"X-API-Key": created["key"]}).status_code == 401


def test_api_keys_scoped_to_owner(client):
    kid = client.post("/api/api-keys", json={"label": "a"}, headers=ALICE).json()["id"]
    # Bob can neither revoke nor see Alice's key.
    assert client.delete(f"/api/api-keys/{kid}", headers=BOB).status_code == 404
    assert all(k["identity"] == "bob@example.com"
               for k in client.get("/api/api-keys", headers=BOB).json()["keys"])
