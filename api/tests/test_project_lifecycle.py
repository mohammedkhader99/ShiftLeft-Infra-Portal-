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


# --- Managing projects through the API ---------------------------------------

@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient
    from api.main import app, get_session

    def override():
        yield session
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_creating_a_project_records_who_asked_for_it(client, session):
    """requested_by is stamped from the signed-in identity, not accepted from the
    body — so it records who actually did it rather than who claimed to."""
    r = client.post("/api/projects", json={
        "code": "pay", "name": "Payments", "owner_email": "owner@example.com",
        "description": "Card processing", "cost_centre_code": "IMD-2002"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "PAY"            # normalised
    assert body["owner_email"] == "owner@example.com"
    assert body["requested_by"], "the signed-in identity should have been recorded"
    assert body["active"] is True


def test_a_new_project_appears_in_the_form_and_a_disabled_one_does_not(client):
    client.post("/api/projects", json={"code": "NEWP", "name": "New"})
    codes = {p["code"] for p in client.get("/api/lookups").json()["projects"]}
    assert "NEWP" in codes

    client.post("/api/projects", json={"code": "NEWP", "name": "New", "active": False})
    codes = {p["code"] for p in client.get("/api/lookups").json()["projects"]}
    assert "NEWP" not in codes
    # ...but the console still lists it, or an administrator could never re-enable it.
    assert "NEWP" in {p["code"] for p in client.get("/api/projects").json()["projects"]}


def test_editing_does_not_rewrite_who_originally_asked(client, session):
    """requested_by is the origin of the project, not its last editor. Edits are
    in the audit trail."""
    first = client.post("/api/projects", json={"code": "ORIG", "name": "One"}).json()
    again = client.post("/api/projects", json={"code": "ORIG", "name": "Renamed"}).json()
    assert again["name"] == "Renamed"
    assert again["requested_by"] == first["requested_by"]


def test_the_console_shows_what_a_project_already_owns(client, session):
    """An administrator about to disable a project should see the blast radius
    before they do it."""
    row = next(p for p in client.get("/api/projects").json()["projects"]
               if p["code"] == "EGATE")
    assert row["environment_count"] >= 1


def test_deleting_a_referenced_project_is_refused_with_the_alternative(client):
    """Requests carry the project code permanently and environments hold a
    foreign key to it. Deleting would break that history, so it refuses and names
    the operation that was actually wanted."""
    r = client.delete("/api/projects/EGATE")
    assert r.status_code == 409
    assert "disable" in r.json()["detail"].lower()


def test_an_unreferenced_project_can_be_deleted(client):
    client.post("/api/projects", json={"code": "TEMP", "name": "Temporary"})
    assert client.delete("/api/projects/TEMP").status_code == 200
    assert "TEMP" not in {p["code"] for p in client.get("/api/projects").json()["projects"]}
    assert client.delete("/api/projects/TEMP").status_code == 404


def test_a_malformed_code_is_rejected(client):
    assert client.post("/api/projects", json={"code": "bad code!", "name": "x"}).status_code == 422


# --- Surfacing an expiry before the date passes -------------------------------

def test_the_sweep_announces_each_stage_once(session, monkeypatch):
    """Announcing every cycle would make the audit log unreadable and train people
    to ignore it."""
    from db.models import AuditLog
    monkeypatch.setenv("PROJECT_EXPIRY_WARN_DAYS", "30")
    _project(session, "SOON", active=True, expires_at=date.today() + timedelta(days=5))

    for _ in range(3):
        main._sweep_project_expiry(session)
        session.commit()

    events = [e for e in session.scalars(
        select(AuditLog).where(AuditLog.event == "project.expiring")).all()]
    assert len(events) == 1, "should announce once, not once per sweep"
    assert events[0].detail["project"] == "SOON"


def test_crossing_from_expiring_to_expired_is_announced_again(session):
    """Two different facts. The second one matters more."""
    from db.models import AuditLog
    p = _project(session, "GONE", active=True, expires_at=date.today() + timedelta(days=1))
    main._sweep_project_expiry(session)
    session.commit()

    p.expires_at = date.today() - timedelta(days=1)   # the date passes
    session.commit()
    main._sweep_project_expiry(session)
    session.commit()

    events = {e.event for e in session.scalars(select(AuditLog)).all()}
    assert "project.expiring" in events and "project.expired" in events


def test_renewing_a_project_lets_it_warn_again_later(session):
    """Otherwise a project renewed once would never warn again."""
    p = _project(session, "RENEW", active=True, expires_at=date.today() + timedelta(days=2))
    main._sweep_project_expiry(session)
    session.commit()
    assert p.expiry_notified == "expiring"

    p.expires_at = date.today() + timedelta(days=400)   # renewed
    session.commit()
    main._sweep_project_expiry(session)
    session.commit()
    session.refresh(p)
    assert p.expiry_notified is None


def test_a_project_with_no_expiry_is_never_swept(session):
    from db.models import AuditLog
    _project(session, "FOREVER", active=True, expires_at=None)
    main._sweep_project_expiry(session)
    session.commit()
    assert not session.scalars(
        select(AuditLog).where(AuditLog.event.like("project.expir%"))).all()


def test_the_sweep_never_touches_what_the_project_owns(session):
    """The whole point. A record reaching its date must not tear anything down."""
    from db.models import Request
    _project(session, "GONE", active=True, expires_at=date.today() - timedelta(days=1))
    session.add(Request(reference="REQ-OWNED", status="provisioned", requester="t@x.com",
                        request_type="create", deployment_target="oci", project_code="GONE"))
    session.commit()
    main._sweep_project_expiry(session)
    session.commit()
    req = session.scalar(select(Request).where(Request.reference == "REQ-OWNED"))
    assert req.status == "provisioned", "an expiring project must not change its environments"


def test_the_expiring_view_shows_what_each_project_still_owns(client, session):
    """'PAY expires in nine days' is a diary note. 'PAY expires in nine days and
    owns four environments' is a decision."""
    from db.models import Request
    _project(session, "SOON", active=True, expires_at=date.today() + timedelta(days=3))
    session.add(Request(reference="REQ-A", status="provisioned", requester="t@x.com",
                        request_type="create", deployment_target="oci",
                        project_code="SOON", environment_name="pay-uat",
                        environment_tier="uat"))
    session.commit()

    body = client.get("/api/projects/expiring").json()
    row = next(p for p in body["projects"] if p["code"] == "SOON")
    assert row["status"] == "expiring" and row["days_left"] == 3
    assert [e["reference"] for e in row["environments"]] == ["REQ-A"]


def test_projects_with_time_left_are_not_in_the_expiring_view(client, session):
    _project(session, "FINE", active=True, expires_at=date.today() + timedelta(days=300))
    codes = {p["code"] for p in client.get("/api/projects/expiring").json()["projects"]}
    assert "FINE" not in codes


def test_every_change_is_audited(client, session):
    from db.models import AuditLog
    client.post("/api/projects", json={"code": "AUD", "name": "Audited"})
    client.post("/api/projects", json={"code": "AUD", "name": "Audited", "active": False})
    events = [e.event for e in session.scalars(
        select(AuditLog).where(AuditLog.event.like("project.%"))).all()]
    assert "project.created" in events and "project.updated" in events
