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
