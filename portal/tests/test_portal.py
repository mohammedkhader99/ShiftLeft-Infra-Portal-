"""Increment 0.3 checks: the portal page renders and shows a live API value.

These do not need Docker: the API call in /panel is simulated so the test is
fast and self-contained.
"""

from fastapi.testclient import TestClient

from portal.main import app

client = TestClient(app)


def test_index_page_renders():
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Portal is running" in body
    # HTMX is vendored locally, not loaded from a CDN.
    assert "/static/htmx.min.js" in body
    # The panel is wired to fetch live data from the portal's /panel route.
    assert 'hx-get="/panel"' in body


def test_static_htmx_is_served():
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text[:200]


def test_panel_shows_live_api_value(monkeypatch):
    # Simulate the API's /health response so no live API is needed.
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "mock": True}

    monkeypatch.setattr("portal.main.httpx.get", lambda *a, **k: FakeResponse())

    response = client.get("/panel")
    assert response.status_code == 200
    body = response.text
    assert "ok" in body
    assert "Fetched live at" in body


def test_panel_reports_api_failure_plainly(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("portal.main.httpx.get", boom)

    response = client.get("/panel")
    assert response.status_code == 200
    assert "Could not reach the API" in response.text
    assert "connection refused" in response.text


# --- Increment 1.2: guided-request form scaffolding --------------------------

FAKE_LOOKUPS = {
    "projects": [{"code": "EGATE", "name": "eGate Modernisation"}],
    "cost_centres": [{"code": "IMD-1001", "name": "Infrastructure Management"}],
    "technologies": [
        {"code": "postgres16", "name": "PostgreSQL 16", "lifecycle_state": "certified"}
    ],
    "environments": [{"name": "egate-prod", "environment_class": "prod"}],
}


def _fake_lookups_response(*args, **kwargs):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return FAKE_LOOKUPS

    return FakeResponse()


def test_request_new_populates_dropdowns(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", _fake_lookups_response)
    response = client.get("/request/new")
    assert response.status_code == 200
    body = response.text
    # Dropdown values come from the (simulated) API lookups, not placeholders.
    assert "eGate Modernisation" in body
    assert "Infrastructure Management" in body
    assert "PostgreSQL 16 (certified)" in body
    assert "egate-prod (prod)" in body
    # The four request types are present.
    for value in ("create", "add", "resize", "decommission"):
        assert f'value="{value}"' in body


def test_request_new_reports_api_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("portal.main.httpx.get", boom)
    response = client.get("/request/new")
    assert response.status_code == 200
    assert "connection refused" in response.text


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_request_save_shows_reference(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", _fake_lookups_response)  # lookups re-fetch

    def fake_post(url, *args, **kwargs):
        return _FakeResp({"reference": "REQ-2026-0001", "status": "draft", **FAKE_LOOKUPS})

    monkeypatch.setattr("portal.main.httpx.post", fake_post)
    response = client.post("/request/save", data={"request_type": "create"})
    assert response.status_code == 200
    assert "Draft saved as REQ-2026-0001" in response.text


def test_request_submit_shows_field_errors(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", _fake_lookups_response)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/submit"):
            return _FakeResp(
                {"errors": {"environment_name": "must be lowercase letters..."}}, status=422
            )
        return _FakeResp({"reference": "REQ-2026-0002", "status": "draft"})

    monkeypatch.setattr("portal.main.httpx.post", fake_post)
    response = client.post("/request/submit", data={"request_type": "create"})
    assert response.status_code == 200
    assert "Please fix the highlighted fields" in response.text
    assert "must be lowercase letters" in response.text


def test_request_submit_success_banner(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", _fake_lookups_response)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/submit"):
            return _FakeResp({"reference": "REQ-2026-0003", "status": "submitted"})
        return _FakeResp({"reference": "REQ-2026-0003", "status": "draft"})

    monkeypatch.setattr("portal.main.httpx.post", fake_post)
    response = client.post("/request/submit", data={"request_type": "create"})
    assert response.status_code == 200
    assert "submitted" in response.text.lower()


# --- Increment 1.4: live sizing panel ----------------------------------------

FAKE_SIZING = {
    "components": [
        {"technology_name": "PostgreSQL 16", "size": "medium", "vcpu": 4,
         "memory_gb": 16, "storage_gb": 200, "resolved": True},
    ],
    "totals": {"vcpu": 4, "memory_gb": 16, "storage_gb": 200},
}


def test_sizing_panel_renders_resolved_numbers(monkeypatch):
    monkeypatch.setattr(
        "portal.main.httpx.post", lambda *a, **k: _FakeResp(FAKE_SIZING)
    )
    response = client.post(
        "/request/sizing",
        data={"component_technology": "postgres16", "component_size": "medium"},
    )
    assert response.status_code == 200
    body = response.text
    assert "PostgreSQL 16" in body
    assert "Environment total" in body
    # Resolved numbers appear.
    for value in ("4", "16", "200"):
        assert value in body


# --- Increment 1.5: live cost panel ------------------------------------------

FAKE_COST = {
    "currency": "AED",
    "deployment_target": "onprem",
    "known_target": True,
    "lines": [
        {"technology_name": "PostgreSQL 16", "size": "medium", "resolved": True,
         "resource_monthly": 672.0, "licence_monthly": 0.0, "one_time": 500.0,
         "monthly": 672.0},
    ],
    "totals": {"one_time": 500.0, "monthly": 672.0, "annual": 8064.0},
}


def test_cost_panel_renders_totals(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.post", lambda *a, **k: _FakeResp(FAKE_COST))
    response = client.post(
        "/request/cost",
        data={
            "deployment_target": "onprem",
            "component_technology": "postgres16",
            "component_size": "medium",
        },
    )
    assert response.status_code == 200
    body = response.text
    assert "Monthly:" in body and "Annual:" in body and "One-time:" in body
    assert "672.00" in body
    assert "8064.00" in body


def test_cost_panel_prompts_without_target(monkeypatch):
    no_target = {**FAKE_COST, "known_target": False,
                 "totals": {"one_time": 0, "monthly": 0, "annual": 0}}
    monkeypatch.setattr("portal.main.httpx.post", lambda *a, **k: _FakeResp(no_target))
    response = client.post("/request/cost", data={"deployment_target": ""})
    assert "Choose a deployment target" in response.text


# --- Increment 2.8: 'My requests' dashboard ----------------------------------

FAKE_REQUESTS = [
    {
        "reference": "REQ-2026-0007", "status": "provisioned",
        "requester": "mohammed.khader@emaratechg.ae",
        "environment_name": "egate-uat", "target_environment": None,
        "components": [{"technology_code": "postgres16", "size": "medium"}],
        "estimate": {"currency": "AED", "one_time": 500.0, "monthly": 672.0, "annual": 8064.0},
        "approval": {"jira_key": "SDIMD-99", "status": "approved",
                     "ticket_url": "https://jira.emaratech.ae/browse/SDIMD-99"},
    },
]


def test_my_requests_dashboard_renders(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", lambda *a, **k: _FakeResp(FAKE_REQUESTS))
    response = client.get("/requests")
    assert response.status_code == 200
    body = response.text
    assert "My requests" in body
    assert 'href="/request/REQ-2026-0007"' in body       # reference links to detail
    assert "s-provisioned" in body                        # status badge class
    assert "postgres16 (medium)" in body                  # components summary
    assert "672.00 AED" in body                           # monthly cost
    assert "SDIMD-99" in body                             # Jira ticket link


def test_my_requests_empty_state(monkeypatch):
    monkeypatch.setattr("portal.main.httpx.get", lambda *a, **k: _FakeResp([]))
    response = client.get("/requests")
    assert response.status_code == 200
    assert "haven't raised any requests yet" in response.text


def test_my_requests_reports_api_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("portal.main.httpx.get", boom)
    response = client.get("/requests")
    assert response.status_code == 200
    assert "Couldn't load requests" in response.text
    assert "connection refused" in response.text


# --- Increment 2.9: decommission by reference --------------------------------

FAKE_PROVISIONED = [
    {"reference": "REQ-2026-0031", "status": "provisioned",
     "environment_name": "egate-uat", "target_environment": None,
     "components": [{"technology_code": "postgres16", "size": "medium"}],
     "estimate": None, "approval": None},
]


def test_request_new_shows_decommission_picker(monkeypatch):
    def fake_get(url, *a, **k):
        if "/api/lookups" in url:
            return _FakeResp(FAKE_LOOKUPS)
        if "/api/requests" in url:  # the provisioned pick-list
            return _FakeResp(FAKE_PROVISIONED)
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    response = client.get("/request/new")
    assert response.status_code == 200
    body = response.text
    assert 'id="mode-decommission"' in body          # the decommission section exists
    assert 'name="source_reference"' in body          # source picker present
    assert "REQ-2026-0031" in body                     # a provisioned request is listed


FAKE_STATS = {
    "kpis": {"total": 5, "active": 2, "in_flight": 1, "failed": 1, "decommissioned": 1},
    "active_monthly_cost": {"amount": 426.0, "currency": "AED"},
    "by_status": [{"key": "provisioned", "count": 2}, {"key": "apply-failed", "count": 1}],
    "by_type": [{"key": "create", "count": 4}],
    "by_technology": [{"key": "postgres16", "count": 3}],
    "by_target": [{"key": "onprem", "count": 5}],
    "trend": [{"week": "2026-W30", "count": 5}],
}


def test_overview_renders_for_oversight(monkeypatch):
    def fake_get(url, *a, **k):
        if url.endswith("/api/me"):
            return _FakeResp({"email": "a@x.com", "roles": ["platform_admin"]})
        if url.endswith("/api/stats"):
            return _FakeResp(FAKE_STATS)
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    resp = client.get("/overview")
    assert resp.status_code == 200
    body = resp.text
    assert "Estate overview" in body
    assert "provisioned" in body and "postgres16" in body
    assert "426" in body               # active monthly cost tile


def test_overview_has_drilldown_links(monkeypatch):
    def fake_get(url, *a, **k):
        if url.endswith("/api/me"):
            return _FakeResp({"email": "a@x.com", "roles": ["auditor"]})
        if url.endswith("/api/stats"):
            return _FakeResp(FAKE_STATS)
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    body = client.get("/overview").text
    assert "/requests?scope=all" in body               # KPI drill-down
    assert "status=provisioned" in body                 # active tile / status bar
    assert "technology=postgres16" in body              # technology bar
    assert "week=2026-W30" in body                       # trend column


def test_requests_estate_scope_for_oversight(monkeypatch):
    calls = {}

    def fake_get(url, params=None, **k):
        if url.endswith("/api/me"):
            return _FakeResp({"email": "a@x.com", "roles": ["platform_admin"]})
        if url.endswith("/api/requests"):
            calls["params"] = params or {}
            return _FakeResp([{"reference": "REQ-1", "status": "provisioned",
                               "environment_name": "e", "target_environment": None,
                               "components": [], "estimate": None, "approval": None}])
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    resp = client.get("/requests?scope=all&status=provisioned")
    assert resp.status_code == 200
    assert "Estate requests" in resp.text and "Back to overview" in resp.text
    # estate scope drops the per-user filter and forwards the status filter
    assert "requester" not in calls["params"]
    assert calls["params"].get("status") == "provisioned"


def test_requests_estate_scope_denied_for_requester(monkeypatch):
    calls = {}

    def fake_get(url, params=None, **k):
        if url.endswith("/api/me"):
            return _FakeResp({"email": "r@x.com", "roles": ["requester"]})
        if url.endswith("/api/requests"):
            calls["params"] = params or {}
            return _FakeResp([])
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    resp = client.get("/requests?scope=all&status=provisioned")
    assert resp.status_code == 200
    # a non-oversight user can't get the estate view — falls back to their own
    assert "requester" in calls["params"]
    assert "My requests" in resp.text


def test_overview_forbidden_for_requester(monkeypatch):
    def fake_get(url, *a, **k):
        if url.endswith("/api/me"):
            return _FakeResp({"email": "r@x.com", "roles": ["requester"]})
        if url.endswith("/api/stats"):
            return _FakeResp({}, status=403)
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "doesn't have access" in resp.text


def test_workflow_steps_marks_done_and_current():
    from portal.main import _workflow_steps

    req = {"status": "planned", "approval": {"jira_key": "X"}}
    audit = [{"event": "approval.approved", "created_at": "2026-07-28T10:00:00"},
             {"event": "plan.previewed", "created_at": "2026-07-28T10:01:00"}]
    by = {s["label"]: s["state"] for s in _workflow_steps(req, audit)}
    assert by["Submitted"] == "done" and by["Approved"] == "done" and by["Planned"] == "done"
    assert by["Provisioning"] == "current"
    assert by["Provisioned"] == "pending"


def test_workflow_steps_marks_failure():
    from portal.main import _workflow_steps

    req = {"status": "apply-failed", "approval": {"jira_key": "X"}}
    audit = [{"event": "approval.approved", "created_at": "t"},
             {"event": "plan.previewed", "created_at": "t"},
             {"event": "provisioning.started", "created_at": "t"},
             {"event": "apply.failed", "created_at": "t"}]
    by = {s["label"]: s["state"] for s in _workflow_steps(req, audit)}
    assert by["Provisioning"] == "failed"


def test_workflow_steps_decommissioned_appends_terminal():
    from portal.main import _workflow_steps

    steps = _workflow_steps({"status": "decommissioned", "approval": {"jira_key": "X"}}, [])
    assert steps[-1]["label"] == "Decommissioned"
    assert all(s["state"] == "done" for s in steps)


def test_request_workflow_route_renders(monkeypatch):
    def fake_get(url, *a, **k):
        if url.endswith("/audit"):
            return _FakeResp({"entries": [
                {"event": "approval.approved", "created_at": "2026-07-28T10:00:00+00:00"},
                {"event": "plan.previewed", "created_at": "2026-07-28T10:01:00+00:00"},
                {"event": "provisioning.started", "created_at": "2026-07-28T10:02:00+00:00"},
                {"event": "provisioned", "created_at": "2026-07-28T10:03:00+00:00"},
            ]})
        return _FakeResp({"reference": "REQ-2026-0007", "status": "provisioned",
                          "approval": {"jira_key": "SDIMD-9"}})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    resp = client.get("/request/REQ-2026-0007/workflow")
    assert resp.status_code == 200
    assert "Provisioned" in resp.text
    assert "is-done" in resp.text


def test_decommission_technologies_fragment(monkeypatch):
    def fake_get(url, *a, **k):
        if "/api/requests/REQ-2026-0031" in url:
            return _FakeResp({
                "reference": "REQ-2026-0031",
                "components": [{"technology_code": "postgres16", "size": "medium"},
                               {"technology_code": "redis7", "size": "small"}],
            })
        return _FakeResp({})

    monkeypatch.setattr("portal.main.httpx.get", fake_get)
    response = client.get(
        "/request/decommission-technologies",
        params={"source_reference": "REQ-2026-0031", "selected": "postgres16"},
    )
    assert response.status_code == 200
    body = response.text
    # Each technology is a checkbox carrying its size, and the pre-selected one is ticked.
    assert 'value="postgres16:medium"' in body
    assert 'value="redis7:small"' in body
    assert "checked" in body
