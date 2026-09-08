"""A placement is a decision, so it is recorded rather than stored (P.8).

Two properties are defended here.

The first is that changing a placement never destroys the one it replaced. "What
was this approved as?" has to stay answerable after somebody changes their mind,
and a mutable field whose earlier values are gone cannot answer it.

The second is subtler and is why the estimate is kept beside the topology: rates
move. Recomputing a six-week-old request today produces a number nobody ever
approved. An audit trail that reconstructs a different answer than the one that
was signed off is not an audit trail.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.placement import ComponentFacts, HOST_MANAGED, HOST_VM, enumerate_options
from api.placement_store import (
    constraining_placement,
    current_placement,
    filter_options,
    host_modes_used,
    permitted_host_modes,
    placement_history,
    record_placement,
)
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        yield s


VM_TOPOLOGY = {
    "environment": "prod", "deployment_target": "oci",
    "hosts": [{"id": "host-1", "host_mode": "vm",
               "components": ["postgres16", "nodejs20"]}],
}
MANAGED_TOPOLOGY = {
    "environment": "dev", "deployment_target": "oci",
    "hosts": [{"id": "managed-postgres16", "host_mode": "managed",
               "components": ["postgres16"]}],
}
CONTAINER_TOPOLOGY = {
    "environment": "dev", "deployment_target": "oci",
    "hosts": [{"id": "cluster-1", "host_mode": "container",
               "components": ["nodejs20"]}],
}


# --- nothing is ever overwritten ---------------------------------------------

def test_the_first_placement_is_version_one(session):
    placement = record_placement(session, 1, "consolidated", VM_TOPOLOGY,
                                 actor="mohammed.khader")
    assert placement.version == 1
    assert placement.superseded_at is None
    assert placement.created_by == "mohammed.khader"


def test_changing_placement_twice_leaves_a_history(session):
    """The acceptance check."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    record_placement(session, 1, "separated", VM_TOPOLOGY)

    history = placement_history(session, 1)
    assert [p.version for p in history] == [1, 2]
    assert history[0].superseded_at is not None, "the first must be superseded"
    assert history[1].superseded_at is None, "the second is in force"


def test_the_superseded_topology_is_not_rewritten(session):
    """The whole reason for append-only. What was approved must stay readable."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY,
                     estimate={"totals": {"monthly": 700.47}})
    record_placement(session, 1, "separated", CONTAINER_TOPOLOGY,
                     estimate={"totals": {"monthly": 706.05}})

    first = placement_history(session, 1)[0]
    assert first.option_key == "consolidated"
    assert first.topology["hosts"][0]["host_mode"] == "vm"
    assert first.estimate["totals"]["monthly"] == 700.47


def test_the_estimate_is_stored_not_recomputed(session):
    """Rates move. The figure that reached Jira is part of the record."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY,
                     sizing={"machine_count": 1},
                     estimate={"totals": {"monthly": 700.47}, "currency": "AED"})

    placement = current_placement(session, 1)
    assert placement.estimate["totals"]["monthly"] == 700.47
    assert placement.sizing["machine_count"] == 1


def test_current_placement_is_the_unsuperseded_one_not_the_newest(session):
    """Reading the highest version instead would resurrect a withdrawn
    placement the moment one is superseded without a replacement."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    second = record_placement(session, 1, "separated", VM_TOPOLOGY)

    assert current_placement(session, 1).id == second.id


def test_requests_do_not_share_placements(session):
    record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    record_placement(session, 2, "separated", CONTAINER_TOPOLOGY)

    assert current_placement(session, 1).option_key == "consolidated"
    assert current_placement(session, 2).option_key == "separated"
    assert len(placement_history(session, 1)) == 1


def test_a_request_with_no_placement_has_none(session):
    assert current_placement(session, 99) is None
    assert placement_history(session, 99) == []


# --- a follow-up is held to what the environment is made of ------------------

def test_host_modes_are_read_from_the_recorded_topology():
    assert host_modes_used(VM_TOPOLOGY) == {"vm"}
    assert host_modes_used(CONTAINER_TOPOLOGY) == {"container"}
    assert host_modes_used(None) == frozenset()


def test_nothing_placed_yet_means_no_constraint():
    assert permitted_host_modes(None) is None


def test_a_vm_environment_permits_vms_and_managed_services(session):
    """A managed service brings its own hosting, so adding one asks nothing of
    the machines already there."""
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    assert permitted_host_modes(prior) == {"vm", "managed"}


def test_a_vm_environment_does_not_permit_a_cluster(session):
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    assert "container" not in permitted_host_modes(prior)


def test_a_container_environment_permits_containers(session):
    prior = record_placement(session, 1, "separated", CONTAINER_TOPOLOGY)
    assert permitted_host_modes(prior) == {"container", "managed"}


# --- the acceptance check: what a follow-up is offered ------------------------

SELECTION = [
    ComponentFacts("compute-vm", frozenset({HOST_VM}), is_host=True),
    ComponentFacts("postgres16",
                   frozenset({HOST_VM, "container", HOST_MANAGED})),
    ComponentFacts("nodejs20", frozenset({HOST_VM, "container"})),
]


def test_an_unplaced_request_is_offered_everything(session):
    options = enumerate_options(SELECTION)
    assert filter_options(options, None) == options


def test_options_consistent_with_a_vm_environment_stay_available(session):
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    filtered = filter_options(enumerate_options(SELECTION), prior)

    # Every option the resolver produces uses vm or managed hosts, so all
    # survive — the constraint bites on container placements, tested below.
    assert all(o.eligible for o in filtered)


def test_a_container_option_is_refused_against_a_vm_environment(session):
    """The acceptance check: you cannot deploy to a cluster in an environment
    that was built on machines."""
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)

    class FakeHost:
        host_mode = "container"
    cluster_option = type(enumerate_options(SELECTION)[0])(
        key="existing-cluster", title="Deploy onto an existing cluster",
        summary="", hosts=(FakeHost(),))

    filtered = filter_options([cluster_option], prior)
    refused = filtered[0]

    assert not refused.eligible
    assert "built on vm" in refused.reasons[0]
    assert "rebuild, not a resize" in refused.reasons[0]


def test_the_refused_option_is_still_returned(session):
    """Removing it would be indistinguishable from a broken portal."""
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)

    class FakeHost:
        host_mode = "container"
    option = type(enumerate_options(SELECTION)[0])(
        key="existing-cluster", title="Deploy onto an existing cluster",
        summary="", hosts=(FakeHost(),))

    assert len(filter_options([option], prior)) == 1


def test_an_existing_refusal_is_kept_alongside_the_new_one(session):
    """An option refused by policy AND by the prior placement must say both.
    Overwriting the first reason would hide half of why it cannot be chosen."""
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)

    class FakeHost:
        host_mode = "container"
    option = type(enumerate_options(SELECTION)[0])(
        key="existing-cluster", title="Deploy onto an existing cluster",
        summary="", hosts=(FakeHost(),), eligible=False,
        reasons=("postgres16 may not share a host in prod.",))

    refused = filter_options([option], prior)[0]
    assert len(refused.reasons) == 2
    assert "may not share a host" in refused.reasons[0]
    assert "rebuild, not a resize" in refused.reasons[1]


# --- the constraint is about a built environment, not a live draft (P.11) -----
#
# Putting a screen on this is what found it. The constraint was being read from
# THIS request's own last placement, so the first click in the wizard became the
# only click: choosing "managed" and then asking to compare it against
# "consolidated" was refused with *"this environment was built on managed"* about
# an environment that did not exist. Nothing had been built.

def test_a_draft_is_not_constrained_by_its_own_earlier_choice(session):
    """The regression. A requester exploring options is choosing, not changing
    how a running environment is hosted."""
    record_placement(session, 1, "managed", MANAGED_TOPOLOGY)
    assert constraining_placement(session, 1, "draft") is None


def test_a_request_awaiting_approval_is_still_revisable(session):
    """Submitted is not built. The approver has not looked yet, and nothing has
    been provisioned — changing the layout here is a change to a proposal."""
    record_placement(session, 1, "managed", MANAGED_TOPOLOGY)
    for status in ("submitted", "planned", "rejected", "cancelled"):
        assert constraining_placement(session, 1, status) is None, status


def test_a_provisioned_environment_does_constrain(session):
    """The case the constraint was written for. Machines exist; how they are
    hosted is now a fact rather than a preference."""
    placement = record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    assert constraining_placement(session, 1, "provisioned") == placement


def test_an_unrecognised_status_is_treated_as_built(session):
    """Fail safe. The exception list names what is NOT yet built, so a status
    nobody remembered to add keeps the constraint instead of silently dropping
    it — a governance rule must not be lost by omission."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    for status in ("apply-failed", "manual-fulfil", "auto-building",
                   "some-status-invented-next-year"):
        assert constraining_placement(session, 1, status) is not None, status


def test_a_half_built_environment_constrains(session):
    """A failed apply may have created resources before it stopped. Re-hosting
    on top of those is the rebuild this rule exists to prevent, so it is the one
    case where being generous would be wrong."""
    record_placement(session, 1, "consolidated", VM_TOPOLOGY)
    assert constraining_placement(session, 1, "apply-failed") is not None


def test_status_matching_ignores_case_and_padding(session):
    record_placement(session, 1, "managed", MANAGED_TOPOLOGY)
    assert constraining_placement(session, 1, " Draft ") is None


def test_a_request_with_no_placement_is_unconstrained_whatever_its_status(session):
    assert constraining_placement(session, 99, "provisioned") is None


# --- a refusal keeps the cluster list and the advice (P.11) -------------------

def test_a_refused_option_keeps_its_clusters_and_warnings(session):
    """The second defect. This function predates both fields, and rebuilding the
    option without them emptied the cluster list and the stateful-workload advice
    on exactly the options that most needed explaining — the requester was told
    they could not deploy onto a cluster, with the clusters gone from the page."""
    prior = record_placement(session, 1, "consolidated", VM_TOPOLOGY)

    class FakeHost:
        host_mode = "container"

    class FakeCluster:
        def as_dict(self):
            return {"id": "c-1", "name": "oke-egate-dev"}

    option = type(enumerate_options(SELECTION)[0])(
        key="existing-cluster", title="Deploy onto an existing cluster",
        summary="", hosts=(FakeHost(),),
        clusters=(FakeCluster(),),
        warnings=("postgres16 keeps data.",))

    refused = filter_options([option], prior)[0]
    assert not refused.eligible
    assert len(refused.clusters) == 1, "the cluster list survived the refusal"
    assert refused.warnings == ("postgres16 keeps data.",)
