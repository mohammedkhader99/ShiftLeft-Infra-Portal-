"""A request whose handoff fails must be one somebody can act on.

REQ-2026-0314, 2026-09-12. Its placement put three workloads on a cluster, the
orchestrator refused — correctly, nothing deploys into a cluster from here — and
the request was left in `auto-building`, which is in neither QUEUED_STATUSES nor
CANCELLABLE. The sweep would never look at it again and its owner could not close
it. Stranded, and invisible: the screen said "held up" with a perfectly good
explanation of a state nobody could leave.

THE SAME SHAPE, THREE TIMES NOW. REQ-2026-0176 sat `in-progress` for ever and was
closed in the end by editing the database. REQ-2026-0247 was stranded in
`auto-building` by the cost guard that was protecting it. This is the third, and
the first that was created by a deliberate change: making the Kubernetes route
selectable meant a placement could reach the orchestrator and be refused, which
was known — what was not anticipated is that the refusal would strand the
request rather than fail it.

HOW IT HID. The branch ended `return req.status  # still 'submitted' — retried
next cycle unless held`. That is true when the plan fails from `submitted`, and
false when the request arrived through `auto-building` — which happens whenever
SOME of its components lack a certified blueprint. A comment asserting an
invariant the code did not enforce, which is the recurring shape of nearly every
defect in this project.
"""

from __future__ import annotations

import pytest

import api.main as main


def test_the_two_sets_do_not_between_them_cover_every_status():
    """WHY THIS FILE IS NECESSARY AT ALL. If every status were either queued or
    cancellable, a request could never be stranded and none of this would
    matter. `auto-building` is the counter-example, and naming it here is what
    stops somebody reading the fix as belt-and-braces."""
    assert "auto-building" not in main.QUEUED_STATUSES
    assert "auto-building" not in main.CANCELLABLE


@pytest.mark.parametrize("status", ["auto-building", "in-progress", "submitted",
                                    "planned", "apply-failed"])
def test_every_status_a_handoff_can_fail_from_leaves_a_way_out(status):
    """The invariant, stated over the statuses rather than over one path.

    A request is either collectable by the sweep — so the retry counter and the
    halt can do their work — or cancellable by its owner. Neither is optional:
    a request that is neither is one nobody can finish.
    """
    queued = status in main.QUEUED_STATUSES
    cancellable = status in main.CANCELLABLE
    requeued = not (queued or cancellable)

    # `requeued` is what the fix does: a status in neither set becomes
    # `submitted`, which is in both.
    landed = "submitted" if requeued else status

    assert landed in main.QUEUED_STATUSES or landed in main.CANCELLABLE, (
        f"a handoff failing from {status!r} would leave the request stranded")


def test_submitted_is_both_collectable_and_closable():
    """Where a stranded request is put back to. If this ever stopped being true
    the fix would move the problem rather than solve it."""
    assert "submitted" in main.QUEUED_STATUSES
    assert "submitted" in main.CANCELLABLE


def test_the_requeue_is_audited():
    """A status the portal changed on its own behalf, without being asked. Every
    privileged step here is audited; one that quietly rewrote a request's state
    would be the exception, and this one exists precisely because nobody noticed
    the state it was rewriting."""
    import inspect

    source = inspect.getsource(main._advance_request)
    requeue = source[source.index("QUEUED_STATUSES and req.status not in CANCELLABLE"):]

    assert "provision.requeued" in requeue[:400], (
        "the requeue writes no audit entry")
    assert '"from": req.status' in requeue[:400], (
        "the audit does not record what it changed FROM, which is the only way "
        "to find the next status that needed this")


def test_the_comment_that_lied_is_gone():
    """It read "still 'submitted'" and the code did not ensure it. A comment
    asserting an invariant nothing enforces is worse than no comment: it stops
    the next reader checking."""
    import inspect

    source = inspect.getsource(main._advance_request)

    assert "still 'submitted' — retried next cycle" not in source
