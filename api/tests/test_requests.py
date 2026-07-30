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
