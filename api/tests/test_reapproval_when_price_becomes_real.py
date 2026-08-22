"""A price that becomes knowable after approval needs approving again.

REQ-2026-0176 and REQ-2026-0178, both keycloak, both the same shape:

  1. keycloak has no certified blueprint, so nothing knows what it builds, so it
     cannot be priced. It is approved showing no cost.
  2. The agent certifies it MID-FLIGHT — drafts a profile, boots a real machine,
     proves it, certifies on the machine's own report. The work succeeds.
  3. Execution then refuses: "monthly 90.59 exceeds approved 0.00 by more than
     10%", and the request sits in-progress for ever, looking active.

THE GUARD IS RIGHT AND IS NOT WEAKENED HERE. The whole separation of authority
rests on the executing layer re-checking what was actually approved; building a
90.59/month resource against a 0.00 approval is exactly what it exists to stop.

What was wrong is what happened next: nothing. A cost that has become knowable is
not a failure to retry — it is a different request from the one that was
approved, and the only person who can approve this one is the person who
approved that one. So it goes back to Jira, which holds the approval, with the
figure it now has.

Two requests hit this before it was fixed. It is structural, not an edge case:
EVERY first request for a newly-certified component lands here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from db.models import AuditLog, Request, RequestComponent
from db.seed import seed
from db.session import Base

REFUSAL = ('{"detail":"Cost re-validation failed: monthly 90.59 exceeds approved '
           '0.00 by more than 10%."}')


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


def a_request(session, reference="REQ-REAPPROVE-1"):
    req = Request(reference=reference, requester="a@b.com", request_type="create",
                  status="in-progress", deployment_target="oci",
                  environment_name="test-env", environment_tier="Development")
    req.components = [RequestComponent(technology_code="keycloak", size="small")]
    session.add(req)
    session.commit()
    return req


# --- telling this refusal from every other one --------------------------------

def test_a_cost_refusal_is_recognised():
    assert main._needs_reapproval(REFUSAL) is True


@pytest.mark.parametrize("other", [
    '{"detail":"Plan prices at 1240.00 monthly, above the 250.00 proof cap."}',
    '{"detail":"Policy re-check failed at execution."}',
    '{"detail":"High-severity IaC findings block this apply."}',
    '{"detail":"Approval not confirmed in Jira."}',
    "", None,
])
def test_other_refusals_are_left_alone(other):
    """409 also carries the proof cost cap, a policy block and an IaC scan block.
    Those are different problems needing different actions, and turning them all
    into "ask for approval again" would send somebody to Jira to fix a Rego
    rule."""
    assert main._needs_reapproval(other) is False


# --- what happens instead of stalling -----------------------------------------

def test_the_request_goes_back_for_approval_at_the_real_figure(session):
    req = a_request(session)
    main._ask_for_reapproval(session, req, jira_key=None, monthly=90.59)
    session.commit()

    assert req.status == main.REAPPROVAL_NEEDED
    assert "90.59" in req.status_detail
    assert "Nothing has been built" in req.status_detail


def test_it_says_the_work_succeeded_rather_than_reading_as_a_failure(session):
    """The agent did everything right — drafted, built a real machine, proved it,
    certified it. A message that reads as a failure would send somebody hunting a
    fault that does not exist."""
    req = a_request(session, "REQ-REAPPROVE-2")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=90.59)

    detail = req.status_detail
    assert "certified automatically" in detail
    assert "price is real for the first time" in detail
    assert "Approve it again" in detail or "approve" in detail.lower()


def test_it_offers_the_way_out_as_well_as_the_way_on(session):
    """Approving again is one answer; not wanting it at 90.59 a month is another,
    and a request with no exit is how REQ-2026-0176 sat in-progress for ever."""
    req = a_request(session, "REQ-REAPPROVE-3")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=90.59)
    assert "cancel" in req.status_detail.lower()


def test_a_missing_figure_still_asks_rather_than_crashing(session):
    """Pricing can fail. An unaskable question is worse than an imprecise one."""
    req = a_request(session, "REQ-REAPPROVE-4")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=None)

    assert req.status == main.REAPPROVAL_NEEDED
    assert "a real figure" in req.status_detail


def test_it_is_recorded_in_the_audit_trail(session):
    req = a_request(session, "REQ-REAPPROVE-5")
    main._ask_for_reapproval(session, req, jira_key="SDIMD-1", monthly=90.59)
    session.commit()

    entry = session.scalar(select(AuditLog).where(
        AuditLog.reference == "REQ-REAPPROVE-5",
        AuditLog.event == "cost.reapproval_requested"))
    assert entry is not None
    assert entry.detail["monthly"] == 90.59
    assert entry.detail["was_approved_at"] == "unpriced"


# --- it must not be swept up and built ----------------------------------------

def test_the_new_status_is_not_one_the_sweep_provisions(session):
    """The sweep picks up 'submitted' and 'planned'. A request waiting for a
    human to agree a price must not quietly resume — that would be the portal
    building something nobody approved, which is the whole thing being guarded
    against."""
    assert main.REAPPROVAL_NEEDED not in ("submitted", "planned")


def test_it_can_be_cancelled_like_any_other_stalled_request(session):
    """The status was added after the cancel feature. A request that cannot be
    closed is how the first one ended up edited in the database by hand."""
    assert main.REAPPROVAL_NEEDED in main.CANCELLABLE, (
        "a request waiting for re-approval cannot be cancelled, so its only exit "
        "is an approval it may never get")
