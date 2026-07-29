"""BFF checks (UX.1): the proxy forwards the user's identity to the API."""

from fastapi.testclient import TestClient

import webapp.bff.main as bff

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
    # The BFF added the identity server-side and targeted the right upstream path.
    assert captured["headers"]["X-Requester"] == bff.DEV_USER
    assert captured["url"].endswith("/api/me")
