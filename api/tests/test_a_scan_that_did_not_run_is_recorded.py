"""A scan the orchestrator could not complete must leave a trace in the portal.

The other half of H.12. The orchestrator now says which workspaces its IaC scan
failed on; `_record_scan` is what turns that into something a person sees, and it
returned early on "no findings" — so a stack whose scan crashed wrote no audit
entry, set no status, and showed nothing at all. Identical, on every screen the
portal has, to a stack that was scanned and came back clean.

That is the same sentence as H.11, which is the defect where the scan had not run
since 2026-08-06 and there was no symptom. A fix that stops at the orchestrator
leaves the information travelling as far as the API and dying there.

IT STILL DOES NOT BLOCK. Enforcement is the orchestrator's call and is off; this
records and surfaces. The point is that "not checked" and "checked, clean" stop
being the same thing on the screen.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from db.models import AuditLog, Base, Request

CLEAN = {"findings": [], "counts": {"high": 0, "medium": 0, "low": 0},
         "high": 0, "ok": True, "errors": [], "unreviewed_types": []}
CRASHED = {**CLEAN, "ok": False,
           "errors": ["oci-oke: terraform show failed"]}
HIGH = {"findings": [{"severity": "high", "message": "public bucket"}],
        "counts": {"high": 1, "medium": 0, "low": 0}, "high": 1, "ok": False,
        "errors": [], "unreviewed_types": []}


@pytest.fixture()
def request_row():
    """One request, and the session it lives in.

    SQLITE DOES NOT ENFORCE VARCHAR LENGTHS, which is why the status-line test
    below reads the column's declared width rather than trusting the insert to
    fail. Three separate incidents in this project began with a string that fitted
    in a test and not in Postgres.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    req = Request(reference="REQ-S-1", status="planned", requester="r@x.com",
                  environment_name="e1")
    session.add(req)
    session.commit()
    yield session, req
    session.close()


def events(session, reference="REQ-S-1"):
    return session.scalars(
        select(AuditLog).where(AuditLog.reference == reference,
                               AuditLog.event == "scan.findings")).all()


# --- the failure is recorded ---------------------------------------------------

def test_a_scan_that_could_not_run_is_audited(request_row):
    """THE DEFECT. No findings and no audit entry, so nothing distinguished this
    from a clean scan anywhere in the portal."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CRASHED)

    assert len(events(session)) == 1


def test_the_audit_says_which_workspace_could_not_be_scanned(request_row):
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CRASHED)

    assert events(session)[0].detail["errors"] == ["oci-oke: terraform show failed"]


def test_the_requester_is_told_the_plan_was_not_checked(request_row):
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CRASHED)

    assert "did not complete" in req.status_detail
    assert "has not been checked" in req.status_detail


def test_it_never_reports_a_failed_scan_as_a_count_of_findings(request_row):
    """The number a scan that did not run "found" is not a fact about the plan.
    Saying "0 findings" here is the sentence this whole pair of defects is."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CRASHED)

    assert "0 high-severity" not in req.status_detail


def test_the_status_fits_the_column_it_is_stored_in(request_row):
    """H.4's lesson. `status_detail` is varchar(500), SQLite does not enforce it,
    and a sentence written here that fits in the test and not in Postgres is how
    three separate incidents started."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CRASHED)

    width = Request.__table__.c.status_detail.type.length
    assert len(req.status_detail) <= width, (len(req.status_detail), width)


# --- a clean scan is still silent ----------------------------------------------

def test_a_clean_scan_writes_nothing(request_row):
    """Unchanged. An audit entry per plan saying "nothing found" is noise, and
    noise is what a real finding would then be lost in."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", CLEAN)

    assert events(session) == []
    assert not req.status_detail


def test_no_scan_at_all_writes_nothing(request_row):
    """An orchestrator too old to send one. Its absence is a contract fact, not
    a failure this portal should invent an alarm about."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", None)

    assert events(session) == []


# --- findings still behave as they did -----------------------------------------

def test_high_findings_are_still_audited_and_surfaced(request_row):
    session, req = request_row
    main._record_scan(session, req, "INFRA-1", HIGH)

    assert len(events(session)) == 1
    assert events(session)[0].detail["findings"]
    assert "1 high-severity finding(s)" in req.status_detail


def test_findings_win_the_status_line_over_a_failed_scan(request_row):
    """Both at once: one workspace scanned and found something high, another
    could not be scanned. The finding is the more actionable of the two and the
    errors are in the audit entry either way."""
    session, req = request_row
    main._record_scan(session, req, "INFRA-1",
                      {**HIGH, "errors": ["oci-oke: terraform show failed"]})

    assert "high-severity" in req.status_detail
    assert events(session)[0].detail["errors"] == ["oci-oke: terraform show failed"]
