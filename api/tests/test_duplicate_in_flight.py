"""Refusing a second request to do the same thing to the same resource.

Found in use: a decommission submitted twice produced TWO accepted requests and
TWO Jira tickets for one teardown. Nothing downstream caught it, because each
request was individually valid — the source was provisioned for both, and the
components belonged to it in both. The clash exists only BETWEEN requests, so
validation is the only layer that can see it.

The tests split evenly between "must block" and "must NOT block", because a
duplicate guard that is too eager is its own bug: it would stop a requester
decommissioning one component while a colleague decommissions another, or stop
them retrying after a failure.

AND THEN IT MISSED THE WORST CASE (REQ-2026-0264, 2026-09-02). The status list
held every state BEFORE execution and none of the states DURING it, so the guard
covered "someone else has asked" and not "someone else is doing it right now" --
which is when a second teardown does the most damage, because the resources are
disappearing underneath both. `decommissioning` is set at destroy.handoff, and
was not in the list.

The status list is no longer copied into these tests. The copy said four while
the code meant five, which is exactly how a duplicated list falls behind.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.validation import (EXECUTING_STATUSES, IN_FLIGHT_STATUSES,
                            validate_submission)
from db.models import Approval, Request, RequestComponent
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    # The provisioned environment everything below acts on.
    source = Request(
        reference="REQ-2026-0120", requester="a@b.com", request_type="create",
        status="provisioned", deployment_target="oci", environment_name="test4",
    )
    source.components = [
        RequestComponent(technology_code="nginx", size="small"),
        RequestComponent(technology_code="redis7", size="small"),
    ]
    session.add(source)
    session.commit()
    yield session
    session.close()


def _existing(db, reference, request_type, techs, status="submitted", jira=None):
    """A request already in flight against REQ-2026-0120."""
    r = Request(reference=reference, requester="a@b.com", request_type=request_type,
                status=status, source_reference="REQ-2026-0120",
                deployment_target="oci")
    r.components = [RequestComponent(technology_code=t, size="small") for t in techs]
    db.add(r)
    db.flush()
    if jira:
        db.add(Approval(request_id=r.id, jira_key=jira, status="Pending"))
    db.commit()
    return r


def _submitting(request_type, techs, reference="REQ-2026-0999"):
    return {
        "request_type": request_type, "reference": reference,
        "source_reference": "REQ-2026-0120", "deployment_target": "oci",
        "components": [{"technology_code": t, "size": "small"} for t in techs],
    }


# --- Must block ---------------------------------------------------------------

def test_a_second_decommission_of_the_same_component_is_refused(db):
    """THE bug. Two accepted requests, two Jira tickets, one resource."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"])
    errors = validate_submission(_submitting("decommission", ["nginx"]), db)
    assert "source_reference" in errors
    assert "REQ-2026-0121" in errors["source_reference"]
    assert "already requesting decommission" in errors["source_reference"]


def test_the_message_names_the_jira_ticket_to_go_and_look_at(db):
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"], jira="SDIMD-75123")
    message = validate_submission(_submitting("decommission", ["nginx"]), db)["source_reference"]
    assert "SDIMD-75123" in message
    assert "nginx" in message, "and which technology clashes"


def test_partial_overlap_still_clashes(db):
    """Asking to tear down nginx+redis while nginx is already going."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"])
    errors = validate_submission(_submitting("decommission", ["nginx", "redis7"]), db)
    assert "source_reference" in errors


def test_a_second_refresh_of_the_same_environment_is_refused(db):
    """Refresh acts on the whole environment, so components do not narrow it."""
    _existing(db, "REQ-2026-0121", "refresh", [])
    errors = validate_submission(
        {**_submitting("refresh", []), "refresh_from_reference": "REQ-2026-0001"}, db)
    assert "source_reference" in errors


@pytest.mark.parametrize("status", IN_FLIGHT_STATUSES)
def test_every_in_flight_status_blocks(db, status):
    """Every status the list names means the other request can still act.

    Parametrised over the constant itself. It used to name the four statuses in
    a literal beside the four in the code, and when the code needed a fifth the
    test went on passing while agreeing with the wrong list."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"], status=status)
    assert "source_reference" in validate_submission(
        _submitting("decommission", ["nginx"]), db)


# --- while it is actually running (REQ-2026-0264) -------------------------------

def test_a_teardown_already_running_blocks_a_second_one(db):
    """THE DEFECT. REQ-2026-0263 entered `decommissioning` at 09:36:11, one
    minute after REQ-2026-0264 was drafted against the same environment and the
    same two components. Had 0264 been submitted after that moment the guard
    would have accepted it: two approved teardowns, two Jira tickets, one
    environment, resources vanishing under both."""
    _existing(db, "REQ-2026-0263", "decommission", ["nginx"],
              status="decommissioning")

    errors = validate_submission(_submitting("decommission", ["nginx"]), db)

    assert "source_reference" in errors, (
        "a second teardown is accepted while the first is actively running")
    assert "REQ-2026-0263" in errors["source_reference"]


def test_a_running_teardown_is_not_called_awaiting_approval(db):
    """It is past approval. Saying otherwise is the portal stating something
    untrue about its own state, and it points the reader at the wrong action."""
    _existing(db, "REQ-2026-0263", "decommission", ["nginx"],
              status="decommissioning")

    message = validate_submission(
        _submitting("decommission", ["nginx"]), db)["source_reference"]

    assert "awaiting approval" not in message, (
        "a teardown that is already running is described as awaiting approval")
    assert "cannot be cancelled" in message, (
        "it tells the reader to cancel something that can no longer be stopped")


def test_one_still_awaiting_approval_keeps_the_advice_that_applies(db):
    """The other half of the same split: this one CAN be cancelled, and saying
    so is the useful part of the message."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"], status="submitted")

    message = validate_submission(
        _submitting("decommission", ["nginx"]), db)["source_reference"]

    assert "awaiting approval" in message
    assert "cancel it" in message


def test_every_executing_status_is_also_an_in_flight_one():
    """The message branches on EXECUTING_STATUSES, but the query only finds
    requests whose status is in IN_FLIGHT_STATUSES. A status in the first and
    not the second would read as handled and never fire once."""
    missing = sorted(set(EXECUTING_STATUSES) - set(IN_FLIGHT_STATUSES))

    assert not missing, (
        f"{missing} would be described by the message but never matched by the "
        f"query that produces it")


# --- Must NOT block -----------------------------------------------------------

def test_decommissioning_a_different_component_is_allowed(db):
    """Someone tearing down redis must not be stopped by a request tearing down
    nginx. A guard that is too eager is its own bug."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"])
    assert validate_submission(_submitting("decommission", ["redis7"]), db) == {}


def test_a_failed_request_does_not_block_a_retry_forever(db):
    """The commonest reason to raise the same request twice is that the first
    one failed. Blocking on it would make recovery impossible.

    `teardown-failed` IS THE STATUS A FAILED DECOMMISSION ACTUALLY GETS. This
    read `decommission-failed` — nineteen characters, a status this system has
    never set, and one `varchar(16)` cannot hold. It was the last live use of
    the value that caused the 2026-09-09 incident, and it survived here because
    SQLite does not enforce VARCHAR: the test built a request in a state
    production cannot produce and proved nothing about the recovery path it
    names. Found on 2026-09-11 when the model began enforcing the width itself.
    """
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"],
              status="teardown-failed")
    assert validate_submission(_submitting("decommission", ["nginx"]), db) == {}


def test_a_completed_request_does_not_block(db):
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"], status="decommissioned")
    assert validate_submission(_submitting("decommission", ["nginx"]), db) == {}


def test_a_draft_does_not_block(db):
    """A draft is not a commitment, and blocking on one would have the requester
    blocked by their own unfinished work."""
    _existing(db, "REQ-2026-0121", "decommission", ["nginx"], status="draft")
    assert validate_submission(_submitting("decommission", ["nginx"]), db) == {}


def test_a_request_never_blocks_itself(db):
    """It is already a draft row in the database when it is validated. A rule
    that refuses a request on account of itself is worse than no rule."""
    mine = _existing(db, "REQ-2026-0121", "decommission", ["nginx"])
    assert validate_submission(
        _submitting("decommission", ["nginx"], reference=mine.reference), db) == {}


def test_a_different_request_type_does_not_block(db):
    """A refresh in flight must not stop a decommission — different work."""
    _existing(db, "REQ-2026-0121", "refresh", ["nginx"])
    assert validate_submission(_submitting("decommission", ["nginx"]), db) == {}


def test_a_different_source_does_not_block(db):
    other = Request(reference="REQ-2026-0200", requester="a@b.com",
                    request_type="decommission", status="submitted",
                    source_reference="REQ-2026-0999", deployment_target="oci")
    other.components = [RequestComponent(technology_code="nginx", size="small")]
    db.add(other)
    db.commit()
    assert validate_submission(_submitting("decommission", ["nginx"]), db) == {}


def test_creating_a_new_environment_is_never_blocked_by_this(db):
    """`create` does not act on an existing resource, so it is out of scope —
    guarded here because widening SOURCE_ACTING_TYPES by accident would block
    ordinary requests."""
    from api.validation import SOURCE_ACTING_TYPES
    assert "create" not in SOURCE_ACTING_TYPES
    assert "clone" not in SOURCE_ACTING_TYPES, "a clone READS its source"
    assert "dr" not in SOURCE_ACTING_TYPES
