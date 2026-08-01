"""Shared (DB-backed) rate-limit store for HA (F-OPS-01 / F-SEC-09).

The counter lives in the database so the per-minute limit is global across API
replicas. Covers the counter (increment / window reset / per-identity isolation /
prune) and the middleware (429 over the limit, /health exempt, fail-open).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ratelimit
from db.models import RateLimitCounter
from db.session import Base


@pytest.fixture()
def factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# --- Counter -----------------------------------------------------------------

def test_hit_increments_within_window(factory):
    with factory() as s:
        assert ratelimit.hit(s, "alice", now=1000.0) == 1
        assert ratelimit.hit(s, "alice", now=1000.0) == 2
        assert ratelimit.hit(s, "alice", now=1010.0) == 3  # same window (epoch 1000//60 == 1010//60)


def test_window_resets_next_minute(factory):
    with factory() as s:
        assert ratelimit.hit(s, "alice", now=1000.0) == 1
        assert ratelimit.hit(s, "alice", now=1000.0) == 2
        assert ratelimit.hit(s, "alice", now=1080.0) == 1  # next window -> fresh count


def test_identities_are_independent(factory):
    with factory() as s:
        assert ratelimit.hit(s, "alice", now=1000.0) == 1
        assert ratelimit.hit(s, "bob", now=1000.0) == 1
        assert ratelimit.hit(s, "alice", now=1000.0) == 2


def test_prune_removes_old_windows(factory):
    with factory() as s:
        ratelimit.hit(s, "alice", now=0.0)      # window 0
        ratelimit.hit(s, "alice", now=600.0)    # window 10
        ratelimit.hit(s, "alice", now=1200.0)   # window 20 (current)
        removed = ratelimit.prune(s, keep_windows=2, now=1200.0)  # keep windows 18,19,20
        assert removed == 2
        rows = s.scalars(select(RateLimitCounter)).all()
        assert [r.window_epoch for r in rows] == [20]


def test_shared_enabled_flag(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    assert ratelimit.shared_enabled() is False
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "shared")
    assert ratelimit.shared_enabled() is True


# --- Middleware --------------------------------------------------------------

def _app(factory, exempt=("/health",)):
    app = FastAPI()
    ratelimit.install_shared_rate_limit(app, exempt_prefixes=exempt, session_factory=factory)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def test_middleware_429_over_limit(factory, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    client = TestClient(_app(factory))
    codes = [client.get("/ping", headers={"X-Requester": "alice"}).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    # A different identity is unaffected by alice's usage.
    assert client.get("/ping", headers={"X-Requester": "bob"}).status_code == 200


def test_middleware_health_exempt(factory, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    client = TestClient(_app(factory))
    for _ in range(5):
        assert client.get("/health").status_code == 200  # never limited


def test_middleware_disabled_when_zero(factory, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    client = TestClient(_app(factory))
    for _ in range(10):
        assert client.get("/ping", headers={"X-Requester": "alice"}).status_code == 200


def test_middleware_fails_open_on_store_error(monkeypatch):
    class Boom:
        def __call__(self):
            raise RuntimeError("store down")
    app = FastAPI()
    ratelimit.install_shared_rate_limit(app, session_factory=Boom())

    @app.get("/ping")
    def ping():
        return {"ok": True}

    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    client = TestClient(app)
    # Store errors must not block requests — fail open.
    assert client.get("/ping", headers={"X-Requester": "alice"}).status_code == 200
    assert client.get("/ping", headers={"X-Requester": "alice"}).status_code == 200
