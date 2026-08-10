"""Projects have an owner, a lifetime and a switch that works (F-CAT / F-GOV).

A project was a bare lookup — code, name, and an `active` flag that nothing read.
An administrator could disable a project and it would still appear in the request
form and still be accepted on submit, because the flag was never consulted
anywhere. These tests pin that it now is.

The distinction that matters, and it is deliberate:

  * DISABLED refuses always. Someone decided to switch it off.
  * EXPIRED warns by default and blocks only under PROJECT_EXPIRY_ENFORCED,
    matching the budget and quota gates. Expiry arrives by the calendar, and a
    date silently starting to refuse work would be a surprise.

Neither touches what a project already owns. A record reaching its date must
never tear down running infrastructure.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.validation import validate_submission
from db.models import Project
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


def _project(session, code, **kw):
    row = Project(code=code, name=kw.pop("name", f"Project {code}"), **kw)
    session.add(row)
    session.commit()
    return row


def _create_fields(project_code):
    return {"request_type": "create", "project_code": project_code,
            "environment_name": "env-one", "environment_tier": "dev",
            "deployment_target": "oci", "cost_centre_code": "IMD-2002",
            "components": [{"technology_code": "apache", "size": "small"}]}


# --- The switch now works -----------------------------------------------------

def test_a_disabled_project_is_hidden_from_the_request_form(session):
    """It was returned regardless, so disabling a project changed nothing the
    requester could see."""
    _project(session, "LIVE", active=True)
    _project(session, "OFF", active=False)
    rows = session.scalars(
        select(Project).where(Project.active.is_(True))).all()
    codes = {p.code for p in rows}
    assert "LIVE" in codes and "OFF" not in codes


def test_a_disabled_project_is_refused_on_submit(session):
    """Hiding it from the dropdown is not enough — the form is a convenience,
    not the boundary. A crafted or stale submission must still be refused."""
    _project(session, "OFF", active=False)
    errors = validate_submission(_create_fields("OFF"), session)
    assert "project_code" in errors
    assert "disabled" in errors["project_code"].lower()


def test_an_active_project_still_passes(session):
    _project(session, "LIVE", active=True)
    errors = validate_submission(_create_fields("LIVE"), session)
    assert "project_code" not in errors


def test_an_unknown_project_is_still_refused(session):
    errors = validate_submission(_create_fields("NOPE"), session)
    assert "Unknown project" in errors["project_code"]


# --- Expiry ------------------------------------------------------------------

def test_no_expiry_date_gates_nothing(session):
    """Most projects should have none, and an absent date must never block."""
    _project(session, "FOREVER", active=True, expires_at=None)
    assert main._project_expiry_status(session, "FOREVER") is None


def test_a_project_with_time_left_is_ok(session):
    _project(session, "OK", active=True, expires_at=date.today() + timedelta(days=200))
    assert main._project_expiry_status(session, "OK")["status"] == "ok"


def test_a_project_nearing_its_date_warns(session, monkeypatch):
    monkeypatch.setenv("PROJECT_EXPIRY_WARN_DAYS", "30")
    _project(session, "SOON", active=True, expires_at=date.today() + timedelta(days=10))
    st = main._project_expiry_status(session, "SOON")
    assert st["status"] == "expiring" and st["days_left"] == 10


def test_an_expired_project_is_reported_with_its_owner(session):
    _project(session, "GONE", active=True, expires_at=date.today() - timedelta(days=5),
             owner_email="owner@example.com")
    st = main._project_expiry_status(session, "GONE")
    assert st["status"] == "expired" and st["days_left"] == -5
    msg = main._project_expiry_message(st)
    assert "5 days ago" in msg and "owner@example.com" in msg


def test_expiry_blocks_only_when_enforcement_is_on(monkeypatch):
    """Same shape as the budget and quota gates: warn until an operator decides."""
    monkeypatch.delenv("PROJECT_EXPIRY_ENFORCED", raising=False)
    assert main._project_expiry_enforce() is False
    monkeypatch.setenv("PROJECT_EXPIRY_ENFORCED", "true")
    assert main._project_expiry_enforce() is True


def test_an_expired_project_does_not_by_itself_fail_validation(session):
    """Expiry is a guardrail, not a field error. It is decided at submit against
    the enforcement setting, so an expired project can still be warned through."""
    _project(session, "GONE", active=True, expires_at=date.today() - timedelta(days=1))
    errors = validate_submission(_create_fields("GONE"), session)
    assert "project_code" not in errors


# --- The new fields -----------------------------------------------------------

def test_a_project_records_who_asked_and_who_owns_it(session):
    p = _project(session, "PAY", active=True, description="Payments platform",
                 owner_email="owner@example.com", requested_by="req@example.com",
                 requested_by_name="A Requester", cost_centre_code="IMD-2002",
                 expires_at=date.today() + timedelta(days=365))
    session.refresh(p)
    assert p.owner_email == "owner@example.com"
    assert p.requested_by_name == "A Requester"
    assert p.cost_centre_code == "IMD-2002"
    assert p.created_at is not None, "created_at should default, not be left null"


def test_the_settings_are_registered_for_the_admin_console():
    """The standing rule: a new setting must appear in the console."""
    from api import settings
    assert "PROJECT_EXPIRY_ENFORCED" in settings.ALLOWLIST
    assert "PROJECT_EXPIRY_WARN_DAYS" in settings.ALLOWLIST
