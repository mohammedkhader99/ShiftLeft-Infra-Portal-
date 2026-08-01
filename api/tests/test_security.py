"""Application-hardening checks for the API (F-SEC-09): security headers + the
per-client rate limit. (The rate limit is pinned OFF in conftest for the rest of
the suite; this test enables it with a low value.)"""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_security_headers_present():
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_rate_limit_trips_per_client(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    codes = [client.get("/api/me", headers={"X-Requester": "rl-a@x.com"}).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    # a different identity has its own window
    assert client.get("/api/me", headers={"X-Requester": "rl-b@x.com"}).status_code == 200


def test_health_is_exempt_from_rate_limit(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    assert [client.get("/health").status_code for _ in range(4)] == [200, 200, 200, 200]


def test_rate_limit_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    assert all(client.get("/api/me", headers={"X-Requester": "rl-c@x.com"}).status_code == 200
               for _ in range(10))
