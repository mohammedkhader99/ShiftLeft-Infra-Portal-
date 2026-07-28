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
