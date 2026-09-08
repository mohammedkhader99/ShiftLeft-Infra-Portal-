"""Scenario B: asking for a cluster (P.5a).

This increment exists because the plan had a hole. P.4 enumerated only Scenario
A, so nothing ever produced "deploy onto an existing cluster" — and P.5 built the
entitlement machinery, tested it thoroughly, and left it reachable from nothing
but its own tests. Working code that no caller reaches is indistinguishable from
code that does not work, and the tests passing made it look finished.

Two behaviours matter most here.

The first is that a requester entitled to NO cluster is told so. The empty list
is the case where "just hide the option" is most tempting and most wrong: the
option is documented, they will look for it, and its absence teaches them
nothing. It comes back refused, with the reason, and with the alternative named.

The second is that entitlement stays a visibility boundary even inside an
option. A cluster belonging to another project is absent, not greyed — greying
it would publish its name and size to anyone who opens the form.
"""

from __future__ import annotations

import pytest

from api.clusters import Cluster, Need, Quota, RequesterScope, assess_clusters
from api.placement import (
    CONSOLIDATED_OPTION,
    EXISTING_CLUSTER_OPTION,
    HOST_CONTAINER,
    HOST_MANAGED,
    HOST_VM,
    MANAGED_OPTION,
    NEW_CLUSTER_OPTION,
    SEPARATED_OPTION,
    ComponentFacts,
    enumerate_options,
    resolve_options,
)

OKE = ComponentFacts(code="oci-oke", host_modes=frozenset({HOST_MANAGED}),
                     provides_cluster=True)
POSTGRES = ComponentFacts(
    code="postgres16",
    host_modes=frozenset({HOST_VM, HOST_CONTAINER, HOST_MANAGED}))
NODEJS = ComponentFacts(code="nodejs20",
                        host_modes=frozenset({HOST_VM, HOST_CONTAINER}))
VM = ComponentFacts(code="compute-vm", host_modes=frozenset({HOST_VM}),
                    is_host=True)

KUBERNETES_SELECTION = [OKE, POSTGRES, NODEJS]

MINE = Cluster(id="c-1", name="oke-egate-dev", region="me-dubai-1",
               project_code="EGATE", allocatable_vcpu=32,
               allocatable_memory_gb=128)
THEIRS = Cluster(id="c-2", name="oke-visa-prod", region="me-dubai-1",
                 project_code="VISA", allocatable_vcpu=64,
                 allocatable_memory_gb=256)
SCOPE = RequesterScope(project_codes=frozenset({"EGATE"}))
SMALL = Need(vcpu=4, memory_gb=16)


def allow(_topology):
    return {"allow": True, "violations": []}


def options_for(components, clusters=()):
    return {o.key: o for o in enumerate_options(components, clusters)}


# --- the selection decides which scenario applies ----------------------------

def test_asking_for_a_cluster_offers_the_cluster_options():
    keys = list(options_for(KUBERNETES_SELECTION,
                            assess_clusters([MINE], SCOPE, SMALL)))
    assert keys == [EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION, MANAGED_OPTION]


def test_a_cluster_request_is_not_asked_whether_to_consolidate():
    """'One machine or three' is not a question about a Kubernetes request."""
    keys = options_for(KUBERNETES_SELECTION, assess_clusters([MINE], SCOPE, SMALL))
    assert CONSOLIDATED_OPTION not in keys
    assert SEPARATED_OPTION not in keys


def test_a_selection_without_a_cluster_is_unchanged():
    """P.4's behaviour must survive: this increment adds a scenario, it does not
    rewrite the existing one."""
    keys = list(options_for([VM, POSTGRES, NODEJS]))
    assert keys == [MANAGED_OPTION, CONSOLIDATED_OPTION, SEPARATED_OPTION]


def test_the_cluster_itself_is_not_placed_on_itself():
    """OKE is the thing workloads land on, like a VM. Treating it as a workload
    would place a cluster inside a cluster and size it twice."""
    for option in enumerate_options(KUBERNETES_SELECTION,
                                    assess_clusters([MINE], SCOPE, SMALL)).__iter__():
        placed = [c for h in option.hosts for c in h.components]
        assert "oci-oke" not in placed


# --- the empty case, which is where hiding is most tempting ------------------

def test_a_requester_with_no_entitled_cluster_is_told_so():
    """Not hidden. The option is documented; its silent absence teaches nothing
    and produces the support ticket this phase exists to prevent."""
    existing = options_for(KUBERNETES_SELECTION, ())[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert "not entitled to any existing cluster" in existing.reasons[0]
    assert "Provisioning a new one is offered below" in existing.reasons[0]


def test_provisioning_a_new_cluster_is_still_offered_when_there_are_none():
    """It is the answer in that case, not a fallback."""
    assert options_for(KUBERNETES_SELECTION, ())[NEW_CLUSTER_OPTION].eligible


def test_another_projects_cluster_is_absent_not_greyed():
    """Entitlement stays a visibility boundary inside the option too. Greying it
    would publish its name, region and size."""
    assessed = assess_clusters([MINE, THEIRS], SCOPE, SMALL)
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    names = {c.cluster.name for c in existing.clusters}
    assert names == {"oke-egate-dev"}
    assert "oke-visa-prod" not in str(existing.as_dict())


def test_a_cluster_that_cannot_fit_is_listed_with_its_number():
    """Capacity and quota are eligibility, not visibility: shown, refused, and
    with the figure that stopped it."""
    quota = Quota(label="EGATE vCPU ceiling", limit=100, used=98)
    assessed = assess_clusters([MINE], SCOPE, SMALL, [quota])
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert any("98 of 100" in r for r in existing.reasons)
    assert existing.clusters, "the cluster is still listed, not hidden"


def test_one_usable_cluster_among_several_makes_the_option_available():
    full = Cluster(id="c-3", name="oke-egate-full", region="me-dubai-1",
                   project_code="EGATE", allocatable_vcpu=1,
                   allocatable_memory_gb=1)
    assessed = assess_clusters([MINE, full], SCOPE, SMALL)
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    assert existing.eligible
    assert len(existing.clusters) == 2, "both listed"
    assert sum(1 for c in existing.clusters if c.eligible) == 1


# --- stateful workloads warn, and name the way out ---------------------------

def test_a_database_on_a_cluster_warns_and_points_at_the_managed_option():
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]

    assert any("postgres16 keeps data" in w for w in existing.warnings)
    assert any("Managed where available" in w for w in existing.warnings)


def test_the_warning_does_not_refuse_the_option():
    """Running a database on Kubernetes is a legitimate choice."""
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]
    assert existing.eligible and existing.warnings


def test_a_stateless_workload_is_not_warned_about():
    existing = options_for([OKE, NODEJS],
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]
    assert existing.warnings == ()


def test_the_managed_alternative_is_actually_offered_alongside():
    """The warning names it, so it had better be there."""
    keys = options_for(KUBERNETES_SELECTION, assess_clusters([MINE], SCOPE, SMALL))
    assert MANAGED_OPTION in keys


# --- through the resolver, with policy ---------------------------------------

def test_an_option_refused_at_build_time_keeps_its_own_reason():
    """A layout nobody can have is not sent to the policy. Asking whether it is
    permitted would overwrite 'you have no cluster' with something less useful."""
    def refuse_with_a_different_reason(_topology):
        return {"allow": False, "violations": ["some unrelated policy complaint"]}

    resolved = {o.key: o for o in resolve_options(
        KUBERNETES_SELECTION, "dev", "oci", refuse_with_a_different_reason,
        clusters=())}
    existing = resolved[EXISTING_CLUSTER_OPTION]

    assert "not entitled to any existing cluster" in existing.reasons[0]
    assert not any("unrelated policy complaint" in r for r in existing.reasons)


def test_clusters_and_warnings_survive_a_policy_refusal():
    """A refused option must not lose the list and the advice on the way back."""
    def refuse(_topology):
        return {"allow": False, "violations": ["nope"]}

    resolved = {o.key: o for o in resolve_options(
        KUBERNETES_SELECTION, "prod", "oci", refuse,
        clusters=assess_clusters([MINE], SCOPE, SMALL))}
    existing = resolved[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert existing.clusters, "the cluster list survived"
    assert existing.warnings, "the stateful warning survived"


def test_the_topology_sent_to_the_policy_uses_container_hosts():
    resolved = resolve_options(KUBERNETES_SELECTION, "dev", "oci", allow,
                               clusters=assess_clusters([MINE], SCOPE, SMALL))
    for option in resolved:
        if option.key in (EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION):
            assert all(h.host_mode == HOST_CONTAINER for h in option.hosts)


# --- "we could not look" is not "you have none" ------------------------------

def test_discovery_being_unavailable_is_not_reported_as_having_no_access():
    """Telling a requester they are entitled to nothing, when the truth is that
    nobody looked, is a lie the portal would be believed about."""
    unavailable = options_for(KUBERNETES_SELECTION, None)[EXISTING_CLUSTER_OPTION]
    entitled_to_none = options_for(KUBERNETES_SELECTION, ())[EXISTING_CLUSTER_OPTION]

    assert unavailable.reasons != entitled_to_none.reasons
    assert "gap in the portal, not a statement about your access" in unavailable.reasons[0]
    assert "not entitled" in entitled_to_none.reasons[0]


def test_both_still_offer_a_new_cluster():
    for clusters in (None, ()):
        assert options_for(KUBERNETES_SELECTION, clusters)[NEW_CLUSTER_OPTION].eligible
