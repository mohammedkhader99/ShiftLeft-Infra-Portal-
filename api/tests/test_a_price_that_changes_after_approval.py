"""A price that becomes knowable after approval stops the request.

THE REMEDY IS A NEW REQUEST, decided by the platform owner on 2026-08-31.
This file used to hold "approve it again"; re-approving in place needs a
resume path the portal does not have, and building one would mean putting
the status into the sweep, which would re-post a Jira comment every thirty
seconds. Cancel-and-resubmit uses paths that already work.

The property under test is unchanged and is the important one: a cost
nobody agreed to is never built.

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

def test_the_request_is_stopped_and_marked_with_the_real_figure(session):
    req = a_request(session)
    main._ask_for_reapproval(session, req, jira_key=None, monthly=90.59)
    session.commit()

    assert req.status == main.COST_CHANGED
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
    # THE REMEDY, and it changed on 2026-08-31: cancel and raise a new one.
    # Telling a requester to approve again would name the one action this
    # portal cannot carry out — there is no resume path, so a second
    # approval leaves the request exactly where it is.
    assert "Cancel this request and raise a new one" in detail
    assert "approve it again" not in detail.lower(), (
        "the message still offers re-approval, which does nothing")


def test_it_offers_the_way_out_as_well_as_the_way_on(session):
    """Cancelling is now the ONLY exit, which makes it matter more rather than
    less: a request with no exit is how REQ-2026-0176 sat in-progress for ever,
    and was closed in the end by editing the database by hand."""
    req = a_request(session, "REQ-REAPPROVE-3")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=90.59)
    assert "cancel" in req.status_detail.lower()


def test_a_missing_figure_still_asks_rather_than_crashing(session):
    """Pricing can fail. An unaskable question is worse than an imprecise one."""
    req = a_request(session, "REQ-REAPPROVE-4")
    main._ask_for_reapproval(session, req, jira_key=None, monthly=None)

    assert req.status == main.COST_CHANGED
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
    """The sweep picks up 'submitted' and 'planned'. A request stopped on a
    price nobody agreed to must not quietly resume — that would be the portal
    building something nobody approved, which is the whole thing being guarded
    against.

    It is also why the remedy is a new request rather than a second approval:
    putting this status into the sweep is exactly what must not happen."""
    assert main.COST_CHANGED not in ("submitted", "planned")


def test_it_can_be_cancelled_like_any_other_stalled_request(session):
    """The status was added after the cancel feature. A request that cannot be
    closed is how the first one ended up edited in the database by hand."""
    assert main.COST_CHANGED in main.CANCELLABLE, (
        "cancelling is the ONLY exit this request has, and it is not cancellable")


# --- the button must exist wherever the API would accept the click ---------------

def test_the_api_tells_the_browser_whether_cancel_would_be_accepted(session):
    """A MIRROR IS TWO SOURCES OF TRUTH, and this one drifted.

    The requests page kept its own copy of the status list, under a comment
    saying "Mirrors the API's CANCELLABLE set. A button offered where the API
    would refuse is a worse experience than no button at all." The risk was
    understood; the copy was still never updated when this status was added.

    So REQ-2026-0247 told its owner, in the portal, to cancel the request — and
    showed no way to cancel it. The only remedy the platform offers, unreachable.

    The API answers now, from the set the endpoint actually consults.
    """
    from api.main import RequestOut

    out = RequestOut(reference="REQ-X", status=main.COST_CHANGED, requester="a@b.c")
    assert out.cancellable is True

    assert RequestOut(reference="REQ-X", status="provisioned",
                      requester="a@b.c").cancellable is False, (
        "a provisioned request must not offer cancel — it has real resources, "
        "and closing the record while they run is how an orphan is made")


def test_the_browser_does_not_keep_its_own_copy_of_the_list():
    """The fix is not "add the missing status"; it is that there is one list."""
    from pathlib import Path

    page = Path("webapp/frontend/src/pages/MyRequests.tsx").read_text(encoding="utf-8")

    assert "r.cancellable" in page, (
        "the page no longer renders the API's answer")
    assert "const CANCELLABLE" not in page, (
        "the page has grown its own copy of the cancellable statuses again; it "
        "will drift, because the last one did")
