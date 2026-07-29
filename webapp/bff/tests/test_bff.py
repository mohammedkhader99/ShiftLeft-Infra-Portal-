"""BFF checks (UX.1 / UX.1b): identity forwarding + the live sign-in guard."""

from fastapi.testclient import TestClient

import webapp.bff.main as bff
from webapp.bff import auth

client = TestClient(bff.app)


def test_healthz():
    assert client.get("/healthz").json() == {"ok": True}


def test_proxy_forwards_identity_and_path(monkeypatch):
    captured = {}

    class _Upstream:
        content = b'{"email":"mohammed.khader@emaratechg.ae","roles":["requester"]}'
        status_code = 200
        headers = {"content-type": "application/json"}

    async def fake_upstream(method, url, params, content, headers):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        return _Upstream()

    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream)

    resp = client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["roles"] == ["requester"]
    # BFF added the identity server-side and targeted the right upstream path.
    assert captured["headers"]["X-Requester"] == auth.DEFAULT_DEV_USER["email"]
    assert captured["url"].endswith("/api/me")


def test_live_mode_redirects_anonymous_page_load_to_login(monkeypatch):
    monkeypatch.setattr(auth, "auth_mode", lambda: "live")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith("/login")


def test_mock_login_starts_a_session(monkeypatch):
    monkeypatch.setattr(auth, "auth_mode", lambda: "mock")
    c = TestClient(bff.app)
    resp = c.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert c.cookies.get("session")  # a session cookie was set
