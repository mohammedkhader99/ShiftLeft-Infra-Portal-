"""Scheduled auto-shutdown checks (F-FIN-06).

Business-hours schedule, off-hours fraction, the quantified saving (non-prod
compute x off-hours share), and the opt-in pause/resume sweep. Prod is never
shut down; the sweep is a no-op when disabled.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import shutdown
from api.main import app, get_session
from db.models import AuditLog, Request, RequestComponent
from db.seed import seed
from db.session import Base

# A known Monday 12:00 UTC to anchor the schedule tests.
_MON = (lambda d: d - timedelta(days=d.weekday()))(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc))


@pytest.fixture(autouse=True)
def _schedule(monkeypatch):
    monkeypatch.setenv("SHUTDOWN_DAYS", "mon-fri")
    monkeypatch.setenv("SHUTDOWN_START", "08:00")
    monkeypatch.setenv("SHUTDOWN_END", "20:00")
    monkeypatch.setenv("SHUTDOWN_TZ", "UTC")
    main._shutdown_last_off = None
    yield
    main._shutdown_last_off = None


@pytest.fixture()
def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)
    yield session
    session.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override_get_session():
        yield _db

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _prov(session, reference, tier, size="large", target="onprem"):
    req = Request(reference=reference, status="provisioned", requester="u@x.com",
                  request_type="create", environment_name=reference.lower(),
                  environment_tier=tier, deployment_target=target)
    req.components = [RequestComponent(technology_code="postgres16", size=size)]
    session.add(req)
    session.commit()
    return req


# --- Schedule ----------------------------------------------------------------

def test_off_hours_by_time_and_day():
    assert shutdown.is_off_hours(_MON.replace(hour=10)) is False   # weekday, in hours
    assert shutdown.is_off_hours(_MON.replace(hour=22)) is True    # weekday, after hours
    assert shutdown.is_off_hours(_MON + timedelta(days=5)) is True  # Saturday


def test_off_hours_fraction():
    # mon-fri 08:00-20:00 => 5*12 = 60 business hours of 168 => 108/168 off.
    assert abs(shutdown.off_hours_fraction() - 108 / 168) < 1e-9


# --- Saving quantification ---------------------------------------------------

def test_saving_is_compute_times_off_fraction(session):
    req = _prov(session, "REQ-NP", "uat", "large")   # onprem large postgres16 compute = 1128
    _prov(session, "REQ-PROD", "prod", "xlarge")     # prod excluded
    sv = shutdown.shutdown_savings(session)
    assert {e["reference"] for e in sv["environments"]} == {"REQ-NP"}
    env = sv["environments"][0]
    assert env["monthly_saving"] == round(env["compute_monthly"] * shutdown.off_hours_fraction(), 2)
    assert sv["total_saving"] == env["monthly_saving"]


def test_status_flags(session, monkeypatch):
    _prov(session, "REQ-NP", "uat")
    st = shutdown.status(session)
    assert st["enabled"] is False and st["total_saving"] > 0 and st["environment_count"] == 1
    assert "paused" in st["environments"][0]


# --- Opt-in sweep ------------------------------------------------------------

def test_sweep_is_a_noop_when_disabled(session):
    _prov(session, "REQ-NP", "uat")
    main._sweep_shutdowns(session)  # SHUTDOWN_ENABLED unset
    assert session.scalars(select(AuditLog).where(AuditLog.event.like("shutdown.%"))).all() == []


def test_sweep_records_pause_then_resume_once_per_boundary(session, monkeypatch):
    _prov(session, "REQ-NP", "uat")
    monkeypatch.setenv("SHUTDOWN_ENABLED", "true")

    monkeypatch.setattr(shutdown, "is_off_hours", lambda now=None: True)
    main._sweep_shutdowns(session)
    main._sweep_shutdowns(session)  # same state — no second event
    paused = session.scalars(select(AuditLog).where(AuditLog.event == "shutdown.paused")).all()
    assert len(paused) == 1 and paused[0].detail["environments"] == 1

    monkeypatch.setattr(shutdown, "is_off_hours", lambda now=None: False)
    main._sweep_shutdowns(session)
    assert len(session.scalars(select(AuditLog).where(AuditLog.event == "shutdown.resumed")).all()) == 1


# --- Endpoint ----------------------------------------------------------------

def test_endpoint_and_rbac(client, session, monkeypatch):
    _prov(session, "REQ-NP", "uat")
    body = client.get("/api/shutdown").json()
    assert body["schedule"]["days"] == "mon-fri" and body["environment_count"] == 1
    assert 0 < body["off_hours_fraction"] < 1
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    assert client.get("/api/shutdown", headers={"X-Requester": "req@x.com"}).status_code == 403
