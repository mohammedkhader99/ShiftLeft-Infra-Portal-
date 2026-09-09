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

SUPERSEDED 2026-09-09, AND THIS IS THE IMPORTANT PART OF THIS FILE NOW.

Nothing in this system deploys a workload into a cluster. The OKE blueprint
builds a cluster whose Kubernetes API endpoint is private, in a VCN where every
subnet is private and the bastion cannot take a public IP; the orchestrator runs
in a container with no ssh, no kubectl and no route into that VCN. Terraform's
kubernetes provider must reach the API server at plan time, so no module here can
deploy into one. For a cluster the same request builds there is a second,
independent blocker: a provider cannot be configured against infrastructure the
same apply is creating.

So both cluster options are now REFUSED, and the tests below assert that instead.
What was found when this was traced is worse than a false promise: `_placement_units`
resolved a container host to the workload's OWN blueprint — `oci-service-vm` for
Node.js — so "provision a Kubernetes cluster and run Node.js on it" would have
built one virtual machine and no cluster, and reported success.

The entitlement, capacity and quota logic is NOT wrong and has NOT been removed.
It is dormant, and `test_the_entitlement_machinery_is_intact_and_dormant` exists
so it is not mistaken for dead code.
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
    """Still OFFERED — refused, but on the screen with the reason. An option that
    vanishes is indistinguishable from a portal that is broken."""
    keys = list(options_for(KUBERNETES_SELECTION,
                            assess_clusters([MINE], SCOPE, SMALL)))
    assert keys == [EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION, MANAGED_OPTION]


def test_neither_cluster_option_can_be_chosen():
    """The fact that decides both: nothing deploys into a cluster from here."""
    options = options_for(KUBERNETES_SELECTION,
                          assess_clusters([MINE], SCOPE, SMALL))
    for key in (EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION):
        assert options[key].eligible is False, key
        assert "cannot deploy workloads into" in options[key].reasons[0], key


def test_the_refusal_says_what_to_do_instead():
    """Provisioning the cluster on its own DOES work, and the requester deploying
    the workloads themselves is the honest route today."""
    reason = options_for(KUBERNETES_SELECTION, ())[NEW_CLUSTER_OPTION].reasons[0]

    assert "Ask for the cluster on its own" in reason
    assert "postgres16" in reason and "nodejs20" in reason, "it names the workloads"


def test_a_cluster_on_its_own_is_still_available():
    """THE PART THAT STILL WORKS, and the reason the options are not simply
    removed. oci-oke is certified and builds a cluster; it is the workloads that
    have no deployment path, not the cluster."""
    just_the_cluster = options_for([OKE], ())[NEW_CLUSTER_OPTION]

    assert just_the_cluster.eligible is True
    assert "Nothing is deployed into it" in just_the_cluster.summary
    assert just_the_cluster.hosts[0].components == ("oci-oke",)
    assert just_the_cluster.hosts[0].host_mode == HOST_MANAGED, (
        "a managed control plane, which is what the catalogue already calls it")


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

def test_entitlement_is_no_longer_the_deciding_reason():
    """It was, and the wording is kept in `api.clusters` for when it is again.
    Answering "which clusters may you use" while nothing can deploy to any of
    them would put two refusals on the screen and bury the one that decides it."""
    existing = options_for(KUBERNETES_SELECTION, ())[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert len(existing.reasons) == 1
    assert "not entitled" not in existing.reasons[0]
    assert "cannot deploy workloads into" in existing.reasons[0]


def test_another_projects_cluster_is_absent_not_greyed():
    """Entitlement stays a visibility boundary inside the option too. Greying it
    would publish its name, region and size."""
    assessed = assess_clusters([MINE, THEIRS], SCOPE, SMALL)
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    names = {c.cluster.name for c in existing.clusters}
    assert names == {"oke-egate-dev"}
    assert "oke-visa-prod" not in str(existing.as_dict())


def test_whatever_was_found_still_reaches_the_requester():
    """The list travels even though the option cannot be chosen, so a requester
    sees both what they would be entitled to and why it cannot be used. When a
    deployment path exists, the list is already arriving."""
    quota = Quota(label="EGATE vCPU ceiling", limit=100, used=98)
    assessed = assess_clusters([MINE], SCOPE, SMALL, [quota])
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    assert existing.clusters, "the cluster is still listed, not hidden"
    assert existing.clusters[0].cluster.name == "oke-egate-dev"


def test_a_usable_cluster_no_longer_makes_the_option_available():
    """It did, and that was right while deploying was possible. Capacity is not
    the binding constraint any more."""
    full = Cluster(id="c-3", name="oke-egate-full", region="me-dubai-1",
                   project_code="EGATE", allocatable_vcpu=1,
                   allocatable_memory_gb=1)
    assessed = assess_clusters([MINE, full], SCOPE, SMALL)
    existing = options_for(KUBERNETES_SELECTION, assessed)[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert len(existing.clusters) == 2, "both still listed"
    assert sum(1 for c in existing.clusters if c.eligible) == 1, (
        "and the assessment itself is unchanged")


def test_the_entitlement_machinery_is_intact_and_dormant():
    """NOT DEAD CODE. `api.clusters` still answers entitlement, capacity and
    quota correctly; it simply has no caller that can act on the answer while
    nothing deploys into a cluster. P.5a exists because this logic once had no
    caller and its tests passed anyway — this test is here so the next person to
    find it callerless deletes nothing."""
    assessed = assess_clusters([MINE, THEIRS], SCOPE, SMALL)

    assert [c.cluster.name for c in assessed] == ["oke-egate-dev"], (
        "another project's cluster is still absent, not greyed")
    quota = Quota(label="EGATE vCPU ceiling", limit=100, used=98)
    refused = assess_clusters([MINE], SCOPE, SMALL, [quota])
    assert refused and not refused[0].eligible
    assert any("98 of 100" in r for r in refused[0].reasons), (
        "and it still names the figure that stopped it")


# --- stateful workloads warn, and name the way out ---------------------------

def test_a_database_on_a_cluster_warns_and_points_at_the_managed_option():
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]

    assert any("postgres16 keeps data" in w for w in existing.warnings)
    assert any("Managed where available" in w for w in existing.warnings)


def test_the_warning_is_still_advisory_and_is_not_the_refusal():
    """Running a database on Kubernetes remains a legitimate choice, and the
    warning must not be mistaken for the reason the option cannot be taken —
    those are different sentences with different remedies."""
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]

    assert existing.warnings, "the advice survives"
    assert not any("keeps data" in r for r in existing.reasons), (
        "the stateful warning is not a refusal reason")
    assert "cannot deploy workloads into" in existing.reasons[0]


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

    assert "cannot deploy workloads into" in existing.reasons[0]
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

def test_neither_none_nor_empty_is_reported_as_having_no_access():
    """The principle that produced the None/[] distinction still holds — telling a
    requester they are entitled to nothing when nobody looked is a lie the portal
    would be believed about — but neither answer decides this option now, so
    NEITHER claims anything about access."""
    for clusters in (None, ()):
        reasons = options_for(KUBERNETES_SELECTION, clusters)[EXISTING_CLUSTER_OPTION].reasons
        assert len(reasons) == 1
        assert "not entitled" not in reasons[0]
        assert "cannot deploy workloads into" in reasons[0]


def test_neither_none_nor_empty_crashes_the_option():
    """`None` meant "nobody looked" and `()` meant "you have none"; both must
    still produce an option rather than an exception."""
    for clusters in (None, ()):
        option = options_for(KUBERNETES_SELECTION, clusters)[EXISTING_CLUSTER_OPTION]
        assert option.clusters == ()
