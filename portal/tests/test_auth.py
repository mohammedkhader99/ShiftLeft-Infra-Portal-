"""Increment 2.3a checks: mock dev login, session, requester stamping.

Live Microsoft sign-in is not exercised here (no real Entra); the mock flow and
the requester header the portal sends are what we can verify locally.
"""

from fastapi.testclient import TestClient

from portal.main import app

client = TestClient(app)


def test_login_page_renders_in_mock():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_live_mode_guard_redirects_not_errors(monkeypatch):
    # Regression: the guard must be able to read the session in live mode
    # (session middleware ordering). Anonymous -> redirect to /login, not 500.
    monkeypatch.setattr("portal.main.auth.is_live", lambda: True)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith("/login")


def test_mock_login_sets_session_and_home_shows_user():
    c = TestClient(app)
    c.post("/login", data={"email": "alice@example.com", "name": "Alice"},
           follow_redirects=False)
    home = c.get("/")
    assert "Alice" in home.text


def test_logout_clears_user():
    c = TestClient(app)
    c.post("/login", data={"email": "alice@example.com", "name": "Alice"})
    c.get("/logout")
    home = c.get("/")
    # Back to the default mock user, not Alice.
    assert "Alice" not in home.text


def test_portal_sends_requester_header_from_session(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"reference": "REQ-2026-0001", "status": "draft"}

    def fake_post(url, *args, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _Resp()

    def fake_get(*args, **kwargs):
        class G:
            def raise_for_status(self):
                return None

            def json(self):
                return {"projects": [], "cost_centres": [], "technologies": [], "environments": []}
        return G()

    monkeypatch.setattr("portal.main.httpx.post", fake_post)
    monkeypatch.setattr("portal.main.httpx.get", fake_get)

    c = TestClient(app)
    c.post("/login", data={"email": "bob@example.com", "name": "Bob"})
    c.post("/request/save", data={"request_type": "create"})
    assert captured["headers"].get("X-Requester") == "bob@example.com"
