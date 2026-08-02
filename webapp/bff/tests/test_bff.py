"""BFF checks (UX.1 / UX.1b): identity forwarding + the live sign-in guard."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import webapp.bff.main as bff
from webapp.bff import auth

client = TestClient(bff.app)


class _FakeReq:
    """A stand-in Request whose only surface the token helpers touch is .session."""

    def __init__(self, session: dict | None = None):
        self.session = session if session is not None else {}


@pytest.fixture(autouse=True)
def _clear_token_store():
    """The server-side token store is a module global — isolate it per test."""
    auth._TOKEN_STORE.clear()
    yield
    auth._TOKEN_STORE.clear()


# A far-future stored_at so the store's TTL prune never drops a seeded entry.
_NEVER_PRUNE = 9_999_999_999


def _seed(**fields):
    """A signed-in _FakeReq whose cookie sid points at a server-side token entry."""
    fields.setdefault("stored_at", _NEVER_PRUNE)
    req = _FakeReq({"sid": "sid-test"})
    auth._TOKEN_STORE["sid-test"] = dict(fields)
    return req


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


def test_proxy_forwards_content_type_for_json_post(monkeypatch):
    captured = {}

    class _Upstream:
        content = b'{"totals":{"monthly":0}}'
        status_code = 200
        headers = {"content-type": "application/json"}

    async def fake_upstream(method, url, params, content, headers):
        captured["headers"] = headers
        return _Upstream()

    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream)
    # A browser sends Origin on a mutating request; the BFF's CSRF check requires it.
    resp = client.post("/api/cost", headers={"Origin": "http://testserver"},
                       json={"deployment_target": "onprem", "components": []})
    assert resp.status_code == 200
    # Without this the API can't parse the JSON body upstream.
    assert captured["headers"].get("Content-Type") == "application/json"


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


# --- Token lifecycle helpers (silent renewal) --------------------------------

def test_store_tokens_keeps_big_tokens_out_of_the_cookie():
    # The regression that broke login: id + refresh tokens are too big for the
    # signed cookie. They must live server-side; only a small sid is in the cookie.
    req = _FakeReq()
    auth.store_tokens(req, {"id_token": "id1", "refresh_token": "r1", "expires_at": 2000000000})
    sid = req.session["sid"]
    assert "id_token" not in req.session and "refresh_token" not in req.session
    assert auth.get_id_token(req) == "id1"
    entry = auth._TOKEN_STORE[sid]
    assert entry["refresh_token"] == "r1"
    assert entry["token_expires_at"] == 2000000000


def test_store_tokens_computes_expiry_from_expires_in(monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 1000.0)
    req = _FakeReq()
    auth.store_tokens(req, {"id_token": "id1", "expires_in": 3600})
    assert auth._TOKEN_STORE[req.session["sid"]]["token_expires_at"] == 1000 + 3600


def test_store_tokens_keeps_old_refresh_when_response_omits_it():
    # Entra may not re-issue a refresh token on every refresh — keep the old one.
    req = _seed(refresh_token="old")
    auth.store_tokens(req, {"id_token": "id2"})
    assert auth._TOKEN_STORE[req.session["sid"]]["refresh_token"] == "old"
    assert auth.get_id_token(req) == "id2"


def test_token_expired_within_skew_and_untracked(monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 1000.0)
    assert auth.token_expired(_seed(token_expires_at=1030), skew=60) is True   # 30s left
    assert auth.token_expired(_seed(token_expires_at=5000), skew=60) is False
    assert auth.token_expired(_FakeReq(), skew=60) is False  # no session/entry → don't force


def test_end_session_clears_identity_and_tokens():
    req = _seed(id_token="i", refresh_token="r", token_expires_at=1)
    req.session["user"] = {"email": "x"}
    sid = req.session["sid"]
    auth.end_session(req)
    assert req.session == {}
    assert sid not in auth._TOKEN_STORE


def _fake_oauth(monkeypatch, fetch):
    class _Entra:
        async def fetch_access_token(self, **kwargs):
            return fetch(**kwargs)

    class _OAuth:
        entra = _Entra()

    monkeypatch.setattr(auth, "get_oauth", lambda: _OAuth())


def test_refresh_tokens_renews_from_refresh_token(monkeypatch):
    captured = {}

    def fetch(**kwargs):
        captured.update(kwargs)
        return {"id_token": "id-new", "refresh_token": "r2", "expires_at": 2000000000}

    _fake_oauth(monkeypatch, fetch)
    req = _seed(refresh_token="r1")
    assert asyncio.run(auth.refresh_tokens(req)) is True
    # It really used the refresh grant with the stored token.
    assert captured["grant_type"] == "refresh_token"
    assert captured["refresh_token"] == "r1"
    # And the store now carries the fresh tokens.
    assert auth.get_id_token(req) == "id-new"
    assert auth._TOKEN_STORE[req.session["sid"]]["refresh_token"] == "r2"


def test_refresh_tokens_false_without_a_refresh_token():
    assert asyncio.run(auth.refresh_tokens(_FakeReq())) is False


def test_refresh_tokens_false_when_entra_rejects(monkeypatch):
    def fetch(**kwargs):
        raise RuntimeError("invalid_grant")

    _fake_oauth(monkeypatch, fetch)
    assert asyncio.run(auth.refresh_tokens(_seed(refresh_token="stale"))) is False


# --- Proxy renewal behaviour -------------------------------------------------

def _fake_upstream_seq(monkeypatch, codes):
    """Return responses with the given status codes in order; record call count."""
    state = {"calls": 0}
    seq = list(codes)

    class _Up:
        def __init__(self, code):
            self.status_code = code
            self.content = b"{}"
            self.headers = {"content-type": "application/json"}

    async def fake(method, url, params, content, headers):
        state["calls"] += 1
        return _Up(seq.pop(0) if seq else codes[-1])

    monkeypatch.setattr(bff, "_proxy_upstream", fake)
    return state


def test_proxy_renews_expiring_token_before_forwarding(monkeypatch):
    monkeypatch.setattr(auth, "auth_mode", lambda: "live")
    monkeypatch.setattr(auth, "token_expired", lambda request, skew=60: True)
    refreshed = {"n": 0}

    async def fake_refresh(request):
        refreshed["n"] += 1
        return True

    monkeypatch.setattr(auth, "refresh_tokens", fake_refresh)
    up = _fake_upstream_seq(monkeypatch, [200])

    resp = TestClient(bff.app).get("/api/me")
    assert resp.status_code == 200
    assert refreshed["n"] == 1   # renewed proactively, before forwarding
    assert up["calls"] == 1      # forwarded once; no 401 retry needed


def test_proxy_refreshes_and_retries_on_upstream_401(monkeypatch):
    monkeypatch.setattr(auth, "auth_mode", lambda: "live")
    monkeypatch.setattr(auth, "token_expired", lambda request, skew=60: False)

    async def fake_refresh(request):
        return True

    monkeypatch.setattr(auth, "refresh_tokens", fake_refresh)
    up = _fake_upstream_seq(monkeypatch, [401, 200])

    resp = TestClient(bff.app).get("/api/me")
    assert resp.status_code == 200   # 401 → refreshed → retried → 200
    assert up["calls"] == 2


def test_proxy_ends_session_when_renewal_fails(monkeypatch):
    monkeypatch.setattr(auth, "auth_mode", lambda: "live")
    monkeypatch.setattr(auth, "token_expired", lambda request, skew=60: False)

    async def fake_refresh(request):
        return False   # refresh token gone/expired — can't renew

    monkeypatch.setattr(auth, "refresh_tokens", fake_refresh)
    _fake_upstream_seq(monkeypatch, [401])

    resp = TestClient(bff.app).get("/api/me")
    assert resp.status_code == 401
    assert "sign in again" in resp.json()["detail"].lower()


def test_proxy_leaves_mock_mode_untouched(monkeypatch):
    # Mock mode has no tokens; a 401 passes straight through, no renewal attempted.
    monkeypatch.setattr(auth, "auth_mode", lambda: "mock")

    def _boom(request):
        raise AssertionError("token_expired must not run in mock mode")

    monkeypatch.setattr(auth, "token_expired", _boom)
    _fake_upstream_seq(monkeypatch, [401])

    resp = TestClient(bff.app).get("/api/me")
    assert resp.status_code == 401  # returned as-is, no re-auth message injected
