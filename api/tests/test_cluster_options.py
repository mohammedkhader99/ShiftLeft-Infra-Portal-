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

SUPERSEDED AGAIN 2026-09-11, AND THE TESTS BELOW ARE NOT WRONG -- THEY ARE ONE
OF TWO STATES.

Asked whether to offer the Kubernetes route, grey it out, or build the portal as
though the network route already existed, the platform owner chose the third
with the consequence stated: a request that chooses Kubernetes now passes
approval and then FAILS AT PROVISIONING, because the orchestrator still refuses
a container host and there is still no route to the private Kubernetes API.

`api.placement.cluster_deployment_offered` is that switch, and it defaults to
ON. Everything in this file up to "the switch, in its other position" describes
it OFF -- every sentence above is still exactly true there -- and the fixture
below pins it off so those assertions keep meaning what they meant. The new
section at the end covers ON, which is what a deployment gets today.

The orchestrator's guard is NOT part of the switch and does not move. Removing
it does not make the cluster path work; it makes a container host resolve to
`oci-service-vm` and build a lone machine with no cluster while reporting
success, which is the failure described three paragraphs up.
"""

from __future__ import annotations

import pytest

from api.clusters import Cluster, Need, Quota, RequesterScope, assess_clusters
import api.placement as placement_mod
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


@pytest.fixture(autouse=True)
def _deployment_refused(monkeypatch):
    """The switch OFF, which is what every test above the final section asserts.

    Pinned rather than assumed: the product default is ON since 2026-09-11, so a
    file that relied on the default would silently change what it was testing.
    """
    monkeypatch.setenv("CLUSTER_DEPLOYMENT_ENABLED", "false")


def allow(_topology):
    return {"allow": True, "violations": []}


def options_for(components, clusters=()):
    return {o.key: o for o in enumerate_options(components, clusters)}


# --- the selection decides which scenario applies ----------------------------

def test_asking_for_a_cluster_offers_the_cluster_options_first():
    """Still OFFERED — refused, but on the screen with the reason, and FIRST:
    the requester asked for a cluster, so why they cannot put workloads on it
    belongs at the top. The layouts that actually build follow underneath."""
    keys = list(options_for(KUBERNETES_SELECTION,
                            assess_clusters([MINE], SCOPE, SMALL)))

    assert keys[:2] == [EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION]
    assert MANAGED_OPTION in keys


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
    have no deployment path, not the cluster.

    It arrives as the MANAGED option rather than as "provision a new cluster",
    because with nothing to deploy onto it a cluster is exactly what the
    catalogue calls it — a managed service — and placing it that way is what
    guarantees the requester always has a layout that builds."""
    managed = options_for([OKE], ())[MANAGED_OPTION]

    assert managed.eligible is True
    assert managed.hosts[0].components == ("oci-oke",)
    assert managed.hosts[0].host_mode == HOST_MANAGED, (
        "a managed control plane, which is what the catalogue already calls it")


def test_a_kubernetes_request_always_has_something_it_can_choose():
    """THE COMPLAINT THIS CAME FROM. Every cluster option is refused, so making
    them the whole reply left a Kubernetes request with nothing to choose — a
    screen of "not available" that taught the requester nothing and built
    nothing."""
    for selection in (KUBERNETES_SELECTION, [OKE], [OKE, NODEJS]):
        options = enumerate_options(selection, ())
        assert any(o.eligible for o in options), (
            f"nothing buildable for {[c.code for c in selection]}")


def test_the_cluster_refusal_is_still_on_the_page_beside_it():
    """Usable is not the same as silent. A requester who asked for Kubernetes
    still has to learn why their workload is not going on it."""
    options = options_for(KUBERNETES_SELECTION, ())

    assert EXISTING_CLUSTER_OPTION in options
    assert "cannot deploy workloads into" in options[EXISTING_CLUSTER_OPTION].reasons[0]
    assert options[MANAGED_OPTION].eligible, "and the working layout is there too"


def test_a_selection_that_could_never_use_a_cluster_is_not_told_about_one():
    """SQL Server is `vm` in this catalogue, so "OKE and SQL Server" is not a
    thwarted Kubernetes deployment — it is a cluster and a database. Explaining
    a deployment nobody was attempting is noise."""
    mssql = ComponentFacts(code="mssql", host_modes=frozenset({HOST_VM}))
    keys = options_for([OKE, mssql], ())

    assert EXISTING_CLUSTER_OPTION not in keys
    assert NEW_CLUSTER_OPTION not in keys
    assert keys[MANAGED_OPTION].eligible


def test_a_cluster_request_is_asked_whether_to_consolidate_after_all():
    """REVERSED DELIBERATELY. "One machine or three" was not a question about a
    Kubernetes request while the workloads were going on Kubernetes. They are
    not: nothing deploys into a cluster, so they are going on machines, and how
    many machines is exactly the question."""
    keys = options_for(KUBERNETES_SELECTION, assess_clusters([MINE], SCOPE, SMALL))

    assert CONSOLIDATED_OPTION in keys
    assert SEPARATED_OPTION in keys


def test_a_selection_without_a_cluster_is_unchanged():
    """P.4's behaviour must survive: this increment adds a scenario, it does not
    rewrite the existing one."""
    keys = list(options_for([VM, POSTGRES, NODEJS]))
    assert keys == [MANAGED_OPTION, CONSOLIDATED_OPTION, SEPARATED_OPTION]


def test_the_cluster_is_never_placed_inside_a_cluster():
    """The invariant, restated for what it always meant. OKE appearing on a
    MANAGED host is correct — that is the cloud running it, and it is how the
    buildable layouts provision the cluster at all. Placing it on a container
    host would be a cluster inside a cluster; placing it on a machine is the
    refusal `_cloud_only` exists for."""
    for option in enumerate_options(KUBERNETES_SELECTION,
                                    assess_clusters([MINE], SCOPE, SMALL)):
        for host in option.hosts:
            if "oci-oke" in host.components:
                assert host.host_mode != HOST_CONTAINER, option.key
                if host.host_mode == HOST_VM:
                    assert not option.eligible, (
                        f"{option.key} offers to install a managed cluster on a "
                        f"machine")


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


# --- the switch, in its other position ---------------------------------------
#
# What a deployment actually gets today. Same selections, same clusters, same
# assertions asked the other way round.


@pytest.fixture()
def offered(monkeypatch):
    monkeypatch.setenv("CLUSTER_DEPLOYMENT_ENABLED", "true")


def test_the_switch_is_on_by_default(monkeypatch):
    """THE DECISION, asserted rather than described. Asked on 2026-09-11 to
    build the portal as though the network route existed, and this is what that
    means in code."""
    monkeypatch.delenv("CLUSTER_DEPLOYMENT_ENABLED", raising=False)
    assert placement_mod.cluster_deployment_offered() is True


def test_with_it_on_a_cluster_layout_can_be_chosen(offered):
    options = options_for(KUBERNETES_SELECTION, assess_clusters([MINE], SCOPE, SMALL))

    for key in (EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION):
        assert options[key].eligible is True, f"{key}: {options[key].reasons}"
        assert not options[key].reasons, key


def test_a_new_cluster_carries_the_cluster_and_the_workloads_separately(offered):
    """Two hosts, not one. The cluster is a managed service the cloud runs; the
    workloads are containers that land on it. One combined container host would
    price the control plane as a workload and lose the cluster from the picture
    the requester is shown."""
    new = options_for(KUBERNETES_SELECTION, ())[NEW_CLUSTER_OPTION]
    by_mode = {h.host_mode: h for h in new.hosts}

    assert by_mode[HOST_MANAGED].components == ("oci-oke",)
    assert set(by_mode[HOST_CONTAINER].components) == {"postgres16", "nodejs20"}


def test_the_cluster_is_still_never_placed_inside_a_cluster(offered):
    """The invariant survives the switch: a cluster on a container host would be
    a cluster inside a cluster."""
    for option in enumerate_options(KUBERNETES_SELECTION,
                                    assess_clusters([MINE], SCOPE, SMALL)):
        for host in option.hosts:
            if "oci-oke" in host.components:
                assert host.host_mode != HOST_CONTAINER, option.key


def test_entitlement_decides_once_deployment_no_longer_does(offered):
    """The dormant machinery wakes up. While nothing could deploy anywhere, which
    cluster a requester may use decided nothing; now it is the only question
    left, and offering one they are not entitled to would be found at the
    cluster rather than at the form."""
    quota = Quota(label="EGATE vCPU ceiling", limit=100, used=98)
    refused = options_for(KUBERNETES_SELECTION,
                          assess_clusters([MINE], SCOPE, SMALL, [quota]))[EXISTING_CLUSTER_OPTION]

    assert refused.eligible is False
    assert any("98 of 100" in r for r in refused.reasons), refused.reasons
    assert refused.clusters, "and the cluster is still listed, not hidden"


def test_one_usable_cluster_is_enough(offered):
    full = Cluster(id="c-3", name="oke-egate-full", region="me-dubai-1",
                   project_code="EGATE", allocatable_vcpu=1,
                   allocatable_memory_gb=1)
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE, full], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is True
    assert len(existing.clusters) == 2, "both still listed"
    assert sum(1 for c in existing.clusters if c.eligible) == 1


def test_the_stateful_warning_still_is_not_a_refusal(offered):
    """Now that the option CAN be chosen, the warning matters more rather than
    less: it is the whole point of seeing the choice made knowingly."""
    existing = options_for(KUBERNETES_SELECTION,
                           assess_clusters([MINE], SCOPE, SMALL))[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is True
    assert any("postgres16 keeps data" in w for w in existing.warnings)
    assert not existing.reasons


def test_a_kubernetes_request_still_has_something_it_can_choose(offered):
    for selection in (KUBERNETES_SELECTION, [OKE], [OKE, NODEJS]):
        options = enumerate_options(selection, ())
        assert any(o.eligible for o in options), [c.code for c in selection]


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
