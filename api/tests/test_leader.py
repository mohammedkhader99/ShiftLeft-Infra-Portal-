"""Single-active leader election for the poller (F-OPS-01).

Guarantees that with N API replicas the poller runs in exactly one: only the
lease holder polls, a dead leader's lease expires and a standby takes over, and
/health/ready reflects database connectivity.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import leader
from db.models import LeaderLease
from db.seed import seed
from db.session import Base


@pytest.fixture()
def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override():
        yield _db
    main.app.dependency_overrides[main.get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# --- Lease semantics ---------------------------------------------------------

def test_first_acquire_creates_lease(session):
    assert leader.try_acquire(session, "poller", holder="a") is True
    row = session.scalar(select(LeaderLease).where(LeaderLease.name == "poller"))
    assert row.holder == "a"


def test_renew_keeps_holder_and_acquired_at(session):
    assert leader.try_acquire(session, "poller", holder="a")
    row = session.scalar(select(LeaderLease))
    first_acquired = row.acquired_at
    assert leader.try_acquire(session, "poller", holder="a")  # renew
    session.refresh(row)
    assert row.holder == "a" and row.acquired_at == first_acquired


def test_second_holder_denied_while_lease_valid(session):
    assert leader.try_acquire(session, "poller", holder="a") is True
    assert leader.try_acquire(session, "poller", holder="b") is False
    row = session.scalar(select(LeaderLease))
    assert row.holder == "a"  # unchanged


def test_takeover_after_expiry(session):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    # 'a' holds a lease that already expired an hour ago.
    assert leader.try_acquire(session, "poller", holder="a", now=past)
    # 'b' can take over now that a's lease has lapsed.
    assert leader.try_acquire(session, "poller", holder="b") is True
    row = session.scalar(select(LeaderLease))
    assert row.holder == "b"


def test_release_lets_standby_take_over(session):
    assert leader.try_acquire(session, "poller", holder="a")
    leader.release(session, "poller", holder="a")
    assert leader.try_acquire(session, "poller", holder="b") is True
    assert session.scalar(select(LeaderLease)).holder == "b"


def test_release_by_non_holder_is_noop(session):
    assert leader.try_acquire(session, "poller", holder="a")
    leader.release(session, "poller", holder="b")  # b doesn't hold it
    # a still holds a valid lease, so b is still denied.
    assert leader.try_acquire(session, "poller", holder="b") is False
    assert session.scalar(select(LeaderLease)).holder == "a"


# --- Readiness endpoint ------------------------------------------------------

def test_health_ready_ok(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["ready"] is True


def test_health_ready_503_when_db_down(client, monkeypatch):
    def boom():
        raise RuntimeError("db unreachable")
    monkeypatch.setattr(main, "SessionLocal", boom)
    assert client.get("/health/ready").status_code == 503


def test_health_liveness_ok(client):
    assert client.get("/health").json()["ok"] is True
