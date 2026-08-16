"""A request must never be given a resource nobody asked for.

WHY THIS FILE EXISTS. REQ-2026-0144 asked for Python 3.12 and received an empty
object storage bucket, and the request was marked `provisioned`. Nothing was
certified for python312, so the resource kind fell through a legacy derivation
that ends at "oci-bucket", the orchestrator built one, and the portal reported
success with a note that python312 "still needs manual fulfilment".

The same is true today of OKE, Kafka, MongoDB and every other uncertified
technology: ask for a Kubernetes cluster, get a bucket.

MANUAL FULFILMENT IS NOT WHAT THIS REFUSES. The portal still validates, prices,
approves and audits a request the infrastructure team will build by hand — that
is a documented feature and requesters rely on it. What stops is inventing a
cloud resource nobody asked for and calling the request finished.
"""

from __future__ import annotations

import inspect

from api import main as api_main


def _source() -> str:
    return inspect.getsource(api_main._advance_request)


def test_a_request_with_nothing_certified_never_reaches_the_orchestrator():
    """THE rule. The guard has to sit BEFORE the handoff — after it, a bucket
    already exists and the damage is a cleanup job."""
    source = _source()
    guard = source.index("_unautomated_components(session, req)")
    handoff = source.index("_handoff_payload(req)")
    assert guard < handoff, (
        "the no-substitute guard runs after the orchestrator handoff, so the "
        "bucket is built before anything decides it should not be")


def test_it_refuses_only_when_nothing_at_all_is_automated():
    """A stack of Apache plus MongoDB must still build the Apache. Refusing the
    whole request because one component is manual would be a worse failure than
    the one being fixed."""
    source = _source()
    assert "len(unmet) == len(set(requested))" in source, (
        "the guard does not compare against the FULL component list, so a mixed "
        "request may be refused for having one manual component")


def test_the_status_says_awaiting_people_not_failure():
    """`apply-failed` would send someone hunting a Terraform error that does not
    exist; `provisioned` would be a lie. It is approved, audited, and waiting on
    the infrastructure team."""
    source = _source()
    assert 'req.status = "manual-fulfil"' in source


def test_the_status_fits_the_column():
    """varchar(16), and SQLite would accept anything longer while Postgres would
    not — the trap that hid `decommission-failed` for weeks."""
    from db import models
    limit = models.Request.__table__.columns["status"].type.length
    assert len("manual-fulfil") <= limit


def test_it_says_plainly_that_nothing_was_built():
    """The sentence a requester reads decides whether they go looking for a
    resource that does not exist."""
    source = _source()
    assert "No cloud resource was created" in source
    assert "certified blueprint" in source


def test_the_requester_is_told_which_components_and_why():
    source = _source()
    assert "listed" in source and "add_comment" in source


def test_it_is_counted_as_in_flight_not_as_failed():
    """It is neither. Someone is going to build it by hand, so it belongs with
    the work still moving — counting it as failed would have an operator chasing
    a fault that is really a queue."""
    stats = inspect.getsource(api_main.stats)
    assert '"manual-fulfil"' in stats
    in_flight = stats.index("in_flight")
    failed = stats.index('"failed"')
    assert stats.index('"manual-fulfil"') < failed or in_flight < failed
