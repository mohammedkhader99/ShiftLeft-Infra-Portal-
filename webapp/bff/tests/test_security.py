"""BFF application-hardening checks (F-SEC-09): security headers, the Origin-based
CSRF check, and the strict-session secret guard."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import webapp.bff.main as bff
from webapp.bff import security

client = TestClient(bff.app)


def _fake_upstream(monkeypatch):
    class _Up:
        content = b"{}"
        status_code = 200
        headers = {"content-type": "application/json"}

    async def _f(method, url, params, content, headers):
        return _Up()
    monkeypatch.setattr(bff, "_proxy_upstream", _f)


def test_security_headers_present():
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline'" in r.headers["Content-Security-Policy"]


def test_csrf_blocks_foreign_origin_post(monkeypatch):
    _fake_upstream(monkeypatch)
    r = client.post("/api/requests/draft", headers={"Origin": "https://evil.example"}, json={})
    assert r.status_code == 403


def test_csrf_blocks_missing_origin_on_post(monkeypatch):
    _fake_upstream(monkeypatch)
    assert client.post("/api/requests/draft", json={}).status_code == 403


def test_csrf_allows_same_origin_post(monkeypatch):
    _fake_upstream(monkeypatch)
    r = client.post("/api/requests/draft", headers={"Origin": "http://testserver"}, json={})
    assert r.status_code != 403  # passes CSRF (upstream mocked)


def test_csrf_exempts_get(monkeypatch):
    _fake_upstream(monkeypatch)
    assert client.get("/api/me").status_code != 403


def test_origin_allowlist(monkeypatch):
    monkeypatch.setenv("PORTAL_ORIGIN", "https://portal.example")

    class _Req:
        headers = {"host": "internal-host"}
    assert security._origin_allowed(_Req(), "https://portal.example") is True
    assert security._origin_allowed(_Req(), "https://other.example") is False


# --- Shared rate limiting (F-OPS-01): delegates counting to the API ----------

def _shared_client(monkeypatch, counter, limit="2"):
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "shared")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", limit)
    app = FastAPI()
    security.install(app, shared_counter=counter)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return TestClient(app)


def test_shared_limit_429_over_limit(monkeypatch):
    counts = iter([1, 2, 3])

    async def counter(key):
        return next(counts)

    c = _shared_client(monkeypatch, counter, limit="2")
    assert [c.get("/ping").status_code for _ in range(3)] == [200, 200, 429]


def test_shared_limit_fails_open_when_api_errors(monkeypatch):
    async def counter(key):
        raise RuntimeError("api unreachable")

    c = _shared_client(monkeypatch, counter, limit="1")
    # The store hop failed — must not block the portal.
    assert c.get("/ping").status_code == 200
    assert c.get("/ping").status_code == 200


def test_shared_limit_exempts_healthz(monkeypatch):
    async def counter(key):
        return 999  # always over any limit

    c = _shared_client(monkeypatch, counter, limit="1")
    assert c.get("/healthz").status_code == 200


def test_memory_backend_used_when_not_shared(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)  # default memory

    async def counter(key):
        raise AssertionError("shared counter must not be called under the memory backend")

    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "5")
    app = FastAPI()
    security.install(app, shared_counter=counter)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    assert TestClient(app).get("/ping").status_code == 200  # counter never invoked


def test_secure_profile_requires_real_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECURE", "true")
    monkeypatch.delenv("SESSION_SECRET", raising=False)  # -> dev default
    with pytest.raises(RuntimeError):
        importlib.reload(bff)
    # restore a clean module for any later tests
    monkeypatch.setenv("SESSION_SECRET", "a-real-secret")
    importlib.reload(bff)
