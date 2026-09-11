"""The pipeline a requester can watch (U.3).

Asked for directly: "Agent -> Generate Blueprint -> Generate/Select Terraform ->
Validate -> Terraform Plan -> Security/Policy Check -> Provision -> Verify ->
Ready." Every one of those already happened; what was missing was anywhere to
see it, so a request sitting at `in-progress` told its owner nothing about
whether the agent was building a recipe, whether Terraform had planned, or
whether it was stuck.

THE PROPERTY THAT MATTERS MOST HERE IS THAT IT DOES NOT INVENT HISTORY. A
progress bar is believed. One that fills itself in because a status looks
advanced would tell somebody their infrastructure was verified when nothing
verified it -- and this portal has already shipped a request marked
`provisioned` that had built an empty bucket. So a stage is `done` because the
append-only trail says so and names when; where the trail is silent it says
`done` with no time rather than a time nothing supports.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import progress
from api.audit import append_audit
from api.main import app, get_session
from db.models import AuditLog, Request
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
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def a_request(session, reference="REQ-PROGRESS-1", status="submitted", detail=None):
    req = Request(reference=reference, requester="a@b.com", request_type="create",
                  status=status, status_detail=detail, deployment_target="oci",
                  environment_name="prog", environment_tier="dev")
    session.add(req)
    session.commit()
    return req


def trail(session, reference, *events) -> dict[str, str]:
    """Write the trail through the REAL append path, and report when each landed.

    Not raw inserts: the audit log is hash-chained, and rows that bypass
    `append_audit` have no chain at all — the insert is refused, which is the
    tamper-evidence working. `append_audit` stamps its own time, so these tests
    assert on what was recorded and in what order rather than on clock values
    nobody controls.
    """
    first: dict[str, str] = {}
    for event in events:
        row = append_audit(session, event, reference=reference, actor="poller")
        session.flush()
        first.setdefault(event, row.created_at.isoformat())
    session.commit()
    return first


def moment(iso: str | None) -> str | None:
    """An ISO timestamp without its offset.

    SQLite has no timezone type, so a tz-aware datetime written through
    `append_audit` comes back naive and the strings differ by "+00:00" while
    being the same instant to the microsecond. PostgreSQL keeps the offset. The
    tests are about WHICH moment is shown, not about which database is under
    them, so both sides are compared on the part both databases agree on.
    """
    return iso.split("+")[0] if iso else iso


def stages(client, reference):
    body = client.get(f"/api/requests/{reference}/progress").json()
    return {s["key"]: s for s in body["stages"]}


# --- the shape of the answer --------------------------------------------------

def test_every_stage_the_requester_was_promised_is_there(session, client):
    a_request(session)
    keys = list(stages(client, "REQ-PROGRESS-1"))

    assert keys == ["submitted", "approved", "blueprint", "terraform", "plan",
                    "policy", "provision", "verify", "ready"]


def test_a_new_request_has_not_done_anything_yet(session, client):
    a_request(session, status="draft")
    got = stages(client, "REQ-PROGRESS-1")

    assert all(s["state"] == "pending" for s in got.values()), got


# --- it does not invent history ----------------------------------------------

def test_a_stage_is_done_because_the_trail_says_so_and_names_when(session, client):
    a_request(session, status="in-progress")
    at = trail(session, "REQ-PROGRESS-1", "approval.approved", "orchestrator.handoff")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["approved"]["state"] == "done"
    assert moment(got["approved"]["at"]) == moment(at["approval.approved"])
    assert got["terraform"]["state"] == "done"
    assert moment(got["terraform"]["at"]) == moment(at["orchestrator.handoff"])
    assert got["approved"]["at"] <= got["terraform"]["at"], "in the order it happened"


def test_a_stage_nothing_recorded_is_done_without_a_time_rather_than_a_made_up_one(
        session, client):
    """THE POINT OF THE WHOLE FILE. The request is demonstrably past this step,
    so saying `pending` would be wrong — but nothing recorded when, and a
    plausible timestamp is the one thing a progress bar must never produce."""
    a_request(session, status="provisioned")
    trail(session, "REQ-PROGRESS-1", "provisioned")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["plan"]["state"] == "done", "it is demonstrably past the plan"
    assert got["plan"]["at"] is None, "and nothing says when, so nothing is shown"


def test_the_time_shown_is_the_first_one_not_the_retry(session, client):
    """A retried apply writes failed then completed. Showing the later time for
    the failure would make a retry look like the original attempt."""
    a_request(session, status="in-progress")
    at = trail(session, "REQ-PROGRESS-1",
               "apply.failed", "provision.retry", "apply.failed")
    rows = session.scalars(
        __import__("sqlalchemy").select(AuditLog)
        .where(AuditLog.event == "apply.failed").order_by(AuditLog.id)).all()
    got = stages(client, "REQ-PROGRESS-1")

    assert len(rows) == 2, "two attempts were recorded"
    assert got["provision"]["state"] == "failed"
    assert moment(got["provision"]["at"]) == moment(at["apply.failed"])
    assert moment(got["provision"]["at"]) == moment(rows[0].created_at.isoformat())
    assert moment(got["provision"]["at"]) != moment(rows[1].created_at.isoformat()), (
        "the first attempt, not the retry")


# --- failure, refusal and the things that never happened ---------------------

def test_a_rejected_request_fails_at_approval(session, client):
    a_request(session, status="rejected")
    trail(session, "REQ-PROGRESS-1", "approval.rejected")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["approved"]["state"] == "failed"


def test_a_stopped_request_is_not_shown_as_still_working(session, client):
    """A cancelled request sits on no step. "In progress" about something that
    has stopped is how somebody ends up waiting for a build that is not coming."""
    a_request(session, status="cancelled")
    trail(session, "REQ-PROGRESS-1", "approval.approved")
    got = stages(client, "REQ-PROGRESS-1")

    assert not any(s["state"] == "current" for s in got.values()), got


def test_manual_fulfilment_skips_terraform_rather_than_failing_it(session, client):
    """It did not go wrong; it was never going to happen. Those are different
    answers and a requester acts differently on each."""
    a_request(session, status="manual-fulfil")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "fulfilment.manual")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["terraform"]["state"] == "skipped"
    assert got["provision"]["state"] == "pending"


def test_the_agent_building_a_recipe_is_the_stage_it_is_sitting_on(session, client):
    a_request(session, status="auto-building")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "autobuild.started")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["blueprint"]["state"] == "current"
    assert got["approved"]["state"] == "done"


def test_the_reason_it_stopped_is_attached_to_where_it_stopped(session, client):
    """The sentence on the request is written for a person to read. It belongs
    beside the step that is stuck, which is where somebody looking at a stalled
    pipeline looks first."""
    a_request(session, status="apply-failed",
              detail="Terraform apply failed: quota exceeded in me-dubai-1.")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "apply.failed")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["provision"]["state"] == "failed"
    assert "quota exceeded" in got["provision"]["detail"]


# --- and it changes nothing ---------------------------------------------------

def test_asking_where_a_request_is_moves_it_nowhere(session, client):
    """Read-only by construction. A progress view that could advance a request
    would be the portal deciding its own outcome."""
    a_request(session, status="submitted")
    before = session.get(Request, 1).status
    for _ in range(3):
        assert client.get("/api/requests/REQ-PROGRESS-1/progress").status_code == 200
    session.expire_all()

    assert session.get(Request, 1).status == before
    assert session.query(AuditLog).count() == 0, "and it writes no trail of its own"


def test_a_request_that_does_not_exist_says_so(client):
    assert client.get("/api/requests/REQ-NOPE/progress").status_code == 404


# --- the stage table itself ---------------------------------------------------

def test_every_stage_names_an_event_something_actually_writes():
    """A stage keyed on an event nobody emits would sit `pending` for ever and
    look like a hung pipeline. Checked against api/main.py's own append_audit
    calls rather than a list kept here."""
    import re
    from pathlib import Path

    source = Path(main.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'append_audit\(\s*session,\s*"([a-z_.]+)"', source))
    emitted |= {"request.submitted", "provisioned", "policy.blocked"}

    referenced = {e for stage in progress.STAGES
                  for e in stage.done_on + stage.failed_on + stage.skipped_on}
    unknown = sorted(referenced - emitted)

    assert not unknown, f"stages keyed on events nothing writes: {unknown}"


# --- the picture has to be coherent as a whole -------------------------------
#
# Found by running the endpoint against a real request rather than a fixture.
# REQ-2026-0305 came back with "Blueprint — not yet" directly above "Terraform
# selected — done". Each stage was individually truthful; together they
# described something impossible.

def test_a_step_is_not_pending_when_a_later_one_has_happened(session, client):
    a_request(session, status="cost-changed")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "orchestrator.handoff")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["terraform"]["state"] == "done", "the trail says it reached the handoff"
    assert got["blueprint"]["state"] == "done", (
        "and a step before that one cannot still be 'not yet'")
    assert got["blueprint"]["at"] is None, "still no invented time"


def test_nothing_after_the_last_thing_that_happened_is_filled_in(session, client):
    """The rule reaches backwards only. Steps genuinely still to come stay
    pending, or the whole view would claim the request had finished."""
    a_request(session, status="in-progress")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "provisioning.started")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["verify"]["state"] == "pending"
    assert got["ready"]["state"] == "pending"


def test_a_skipped_step_stays_skipped(session, client):
    """A later step proves nothing about one that was never going to run."""
    a_request(session, status="manual-fulfil")
    trail(session, "REQ-PROGRESS-1", "approval.approved", "fulfilment.manual")
    got = stages(client, "REQ-PROGRESS-1")

    assert got["terraform"]["state"] == "skipped"


def test_the_states_read_in_order_for_any_request(session, client):
    """The invariant behind all three: done and failed steps come first, then
    whatever is current, then what has not happened. No gaps."""
    for n, (status, events) in enumerate([
        ("submitted", ("approval.checked",)),
        ("auto-building", ("approval.approved", "autobuild.started")),
        ("planned", ("approval.approved", "orchestrator.handoff", "plan.previewed")),
        ("apply-failed", ("approval.approved", "orchestrator.handoff", "apply.failed")),
        ("provisioned", ("approval.approved", "provisioned")),
    ]):
        ref = f"REQ-ORDER-{n}"
        a_request(session, reference=ref, status=status)
        trail(session, ref, *events)
        states = [s["state"] for s in
                  client.get(f"/api/requests/{ref}/progress").json()["stages"]]

        settled = [i for i, s in enumerate(states) if s in ("done", "failed", "skipped")]
        pending = [i for i, s in enumerate(states) if s == "pending"]
        if settled and pending:
            assert max(settled) < min(pending), f"{status}: {states}"
