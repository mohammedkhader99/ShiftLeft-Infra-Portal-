"""A reason the requester is shown must not stop mid-word (H.9).

REQ-2026-0312 was held up showing:

    "...so building this would provision machines instead of th"

which reads as a corrupted record rather than a sentence somebody stopped. What
it removed was every word telling the requester what to do instead.

TWO FAULTS, AND THE SECOND IS THE ONE THAT MATTERED. `_short_reason` cut at
exactly 300 characters wherever that fell — against a 500-character column, so
it was also throwing away two fifths of the room the record has. That limit was
right for the wall of terraform output it was written for and wrong for the
orchestrator's refusals, which are sentences composed for a person.

And those sentences put the remedy at the end, which is the half a trim eats. So
the orchestrator's refusal now leads with it.
"""

from __future__ import annotations

import pytest

import api.main as main
from db.models import Request

LIMIT = Request.__table__.columns["status_detail"].type.length


def test_the_limit_is_the_column_not_a_number_somebody_chose():
    """300 against a 500-character column was not a decision, it was a default
    nobody revisited. H.4 is the standing lesson: read the width, do not
    remember it."""
    assert main._REASON_LIMIT == LIMIT == 500


def test_a_short_reason_is_left_exactly_alone():
    assert main._short_reason("Quota exceeded in me-dubai-1.") == \
        "Quota exceeded in me-dubai-1."


def test_a_long_reason_stops_at_a_sentence_when_one_is_near_the_end():
    """A full stop reads as deliberate. A word boundary reads as truncation.
    Both beat stopping at "th"."""
    body = "First sentence. " + "word " * 120 + "tail."
    out = main._short_reason(body)

    assert len(out) <= LIMIT
    assert out.endswith((".", "\u2026")), out[-40:]


def test_it_never_stops_in_the_middle_of_a_word():
    """The defect, stated as a property. Any input, any length."""
    for n in range(480, 560, 7):
        out = main._short_reason("x " + "abcdefghij " * 60 + "y" * n)
        assert len(out) <= LIMIT
        assert out.endswith((".", "\u2026")), out[-30:]
        if out.endswith("\u2026"):
            # The character before the ellipsis ends a word, not a fragment of
            # one: the source had a space there.
            assert not out[:-1].endswith(("-", ",", ";", ":")), out[-30:]


def test_a_cut_reason_says_it_was_cut():
    out = main._short_reason("no full stops here " * 60)

    assert out.endswith("\u2026"), out[-40:]


def test_terraforms_own_headline_still_wins():
    """The reason this function exists at all: a wall of terraform output has
    one line worth showing."""
    blob = "Initializing...\n\nError: quota exceeded for VM.Standard.E4.Flex\n\nmore noise"
    assert main._short_reason(blob) == "Error: quota exceeded for VM.Standard.E4.Flex"


def test_a_json_wrapped_detail_is_unwrapped_first():
    assert main._short_reason('{"detail": "Approval not confirmed in Jira."}') == \
        "Approval not confirmed in Jira."


def test_nothing_at_all_still_says_something():
    assert main._short_reason("") == "Provisioning failed."


# --- and the message this was found on now fits ------------------------------

def _the_real_refusal() -> str:
    """The orchestrator's actual sentence, built the way it builds it.

    NOT A COPY TYPED HERE. A literal would pass while the real message drifted
    past the column — which is the whole failure being tested. This calls the
    refusal and reads what it produced.
    """
    import pytest
    from fastapi import HTTPException

    import orchestrator.main as omain

    payload = {"placement": {"version": 1, "hosts": [
        {"id": f"cluster-{c}", "host_mode": "container", "components": [c],
         "resolved": True, "resource_kind": "oci-instance"}
        for c in ("postgres16", "minio", "oracle-free")]}}
    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(payload)
    return raised.value.detail


def test_the_cluster_refusal_survives_whole():
    """THE ACCEPTANCE CHECK. The orchestrator's refusal is written for a person
    and ends with what to do; if the trim eats that, the requester is told they
    are stuck and not how to get unstuck."""
    refusal = _the_real_refusal()
    out = main._short_reason(refusal)

    assert out == refusal, f"lost {len(refusal) - len(out)} characters"
    assert "Choose a layout that builds machines" in out, "the remedy survived"
    assert "machines instead of the cluster" in out, (
        "and so did the consequence — a reader told only 'unsupported' does not "
        "learn that building it anyway would have been silently WRONG rather "
        "than absent")


def test_the_remedy_comes_before_the_detail_in_that_refusal():
    """Belt and braces: even if the message grows past the column later, what
    gets cut is the placement detail rather than the way out."""
    import inspect

    import orchestrator.main as omain

    source = inspect.getsource(omain._refuse_unsized_hosts)
    remedy = source.index("Choose a")
    detail = source.index("Placement version")

    assert remedy < detail, "the remedy must precede the detail, or a trim eats it"
