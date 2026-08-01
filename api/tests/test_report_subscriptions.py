"""Scheduled report subscriptions (F-RPT-11).

Oversight users (finance/security/admin) subscribe to a report on a cadence; the
poller generates it, stores a viewable run, and optionally POSTs it HMAC-signed
to a webhook. Reports reuse the existing forecast/anomalies/estate functions.
httpx is mocked so the suite stays offline.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import reports
from common.signing import sign
from db.models import AuditLog, ReportRun, ReportSubscription
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


def _naive(dt):
    """SQLite drops tzinfo on the round-trip (Postgres timestamptz keeps it), so
    normalise before comparing datetimes in these tests."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _mock_post(monkeypatch, status=200, capture=None):
    class Resp:
        status_code = status

    def _post(url, content=None, headers=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "headers": headers, "content": content})
        return Resp()
    monkeypatch.setattr(main.httpx, "post", _post)


def _sub(session, *, report="estate", cadence="weekly", url=None, secret=None, due_delta=timedelta(minutes=-1)):
    s = ReportSubscription(report=report, cadence=cadence, target_url=url, secret=secret,
                           next_due=datetime.now(timezone.utc) + due_delta)
    session.add(s)
    session.commit()
    return s


# --- Report generation (reuses existing functions) ---------------------------

@pytest.mark.parametrize("kind", reports.REPORT_KINDS)
def test_generate_report_each_kind(session, kind):
    data = reports.generate_report(session, kind)
    assert data["report"] == kind


def test_generate_report_estate_shape(session):
    data = reports.generate_report(session, "estate")
    assert "committed_monthly" in data and "by_status" in data
    assert data["currency"] == "AED"


def test_generate_report_rejects_unknown(session):
    with pytest.raises(ValueError):
        reports.generate_report(session, "nope")


# --- Sweep: generate, store, deliver, advance --------------------------------

def test_sweep_generates_and_advances(session):
    sub = _sub(session, cadence="weekly")
    before = _naive(sub.next_due)
    main._sweep_reports(session)
    runs = session.scalars(select(ReportRun).where(ReportRun.subscription_id == sub.id)).all()
    assert len(runs) == 1 and runs[0].report == "estate"
    session.refresh(sub)
    assert sub.last_sent_at is not None
    assert _naive(sub.next_due) > before  # advanced by the cadence (~7 days)
    assert (_naive(sub.next_due) - datetime.now(timezone.utc).replace(tzinfo=None)) > timedelta(days=6)
    assert session.scalars(select(AuditLog).where(AuditLog.event == "report.sent")).all()


def test_sweep_skips_not_due(session):
    _sub(session, due_delta=timedelta(days=3))  # not due yet
    main._sweep_reports(session)
    assert session.scalars(select(ReportRun)).all() == []


def test_sweep_skips_inactive(session):
    sub = _sub(session)
    sub.active = False
    session.commit()
    main._sweep_reports(session)
    assert session.scalars(select(ReportRun)).all() == []


def test_sweep_delivers_signed_when_url_set(session, monkeypatch):
    sub = _sub(session, url="https://reports.example/hook", secret="reportsecret1")
    cap = []
    _mock_post(monkeypatch, 200, cap)
    main._sweep_reports(session)
    run = session.scalar(select(ReportRun).where(ReportRun.subscription_id == sub.id))
    assert run.delivered == "delivered"
    sent = cap[0]
    assert sent["headers"]["X-Signature"] == sign(sub.secret, sent["content"])
    assert json.loads(sent["content"])["report"] == "estate"


def test_sweep_marks_failed_delivery(session, monkeypatch):
    sub = _sub(session, url="https://reports.example/hook", secret="reportsecret1")
    _mock_post(monkeypatch, status=500)
    main._sweep_reports(session)
    run = session.scalar(select(ReportRun).where(ReportRun.subscription_id == sub.id))
    assert run.delivered == "failed"  # stored anyway — always viewable in the portal


def test_sweep_no_delivery_without_url(session, monkeypatch):
    _sub(session)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST without a URL"))
    monkeypatch.setattr(main.httpx, "post", boom)
    main._sweep_reports(session)
    run = session.scalar(select(ReportRun))
    assert run.delivered is None


# --- Endpoints: CRUD, run-now, runs, RBAC ------------------------------------

def test_crud_and_no_secret_leak(client, session):
    body = client.post("/api/report-subscriptions",
                       json={"report": "forecast", "cadence": "monthly",
                             "target_url": "https://r.example/x", "secret": "abc12345"}).json()
    assert body["id"] and body["report"] == "forecast" and body["cadence"] == "monthly"
    assert "secret" not in body
    listed = client.get("/api/report-subscriptions").json()["subscriptions"]
    assert listed[0]["report"] == "forecast" and all("secret" not in s for s in listed)
    assert client.delete(f"/api/report-subscriptions/{body['id']}").json()["deleted"] == body["id"]


def test_validation(client, session):
    assert client.post("/api/report-subscriptions", json={"report": "bogus"}).status_code == 422
    assert client.post("/api/report-subscriptions",
                       json={"report": "estate", "cadence": "hourly"}).status_code == 422
    assert client.post("/api/report-subscriptions",
                       json={"report": "estate", "target_url": "ftp://x"}).status_code == 422


def test_rbac_requester_forbidden(client, session, monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.post("/api/report-subscriptions", headers={"X-Requester": "req@x.com"},
                    json={"report": "estate"})
    assert r.status_code == 403


def test_run_now_does_not_change_schedule(client, session):
    sub = _sub(session, report="anomalies", due_delta=timedelta(days=5))
    before = _naive(sub.next_due)
    body = client.post(f"/api/report-subscriptions/{sub.id}/run").json()
    assert body["report"] == "anomalies" and body["summary"]["report"] == "anomalies"
    session.refresh(sub)
    assert _naive(sub.next_due) == before  # run-now is a test; schedule untouched
    runs = client.get(f"/api/report-subscriptions/{sub.id}/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["id"] == body["id"]


def test_run_now_404(client, session):
    assert client.post("/api/report-subscriptions/9999/run").status_code == 404
