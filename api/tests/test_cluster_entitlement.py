"""What a requester may deploy onto, and what they may even see (P.5).

Two failures are guarded here and they pull in opposite directions.

The first is silent filtering: a cluster that has capacity but breaches a quota
must come back listed and refused, with the ceiling stated, so the requester can
ask for less or ask for more headroom.

The second is disclosure: a cluster belonging to another project must not come
back at all. Applying "always show the reason" to entitlement would publish the
name, region and size of infrastructure the caller has no business knowing
exists. The tests below assert that difference deliberately, because it looks
like an inconsistency until you see what each rule is protecting.
"""

from __future__ import annotations

import pytest

from api.clusters import (
    Cluster,
    ClusterRefused,
    Need,
    Quota,
    RequesterScope,
    assess_clusters,
    authorise_cluster,
    stateful_warnings,
    visible_clusters,
)

# Two teams' clusters, plus one piece of shared infrastructure.
EGATE = Cluster(id="ocid1.cluster.oc1..egate", name="oke-egate-prod",
                region="me-dubai-1", project_code="EGATE",
                cost_centre_code="IMD-1001", subsidiary_code="ETG",
                allocatable_vcpu=32, allocatable_memory_gb=128)
VISA = Cluster(id="ocid1.cluster.oc1..visa", name="oke-visa-prod",
               region="me-dubai-1", project_code="VISA",
               cost_centre_code="IMD-1002", subsidiary_code="ETG",
               allocatable_vcpu=64, allocatable_memory_gb=256)
SHARED = Cluster(id="ocid1.cluster.oc1..shared", name="oke-shared-dev",
                 region="me-dubai-1", allocatable_vcpu=8,
                 allocatable_memory_gb=32)
ALL = [EGATE, VISA, SHARED]

EGATE_ENGINEER = RequesterScope(
    project_codes=frozenset({"EGATE"}),
    cost_centre_codes=frozenset({"IMD-1001"}),
    subsidiary_codes=frozenset({"ETG"}))
VISA_ENGINEER = RequesterScope(
    project_codes=frozenset({"VISA"}),
    cost_centre_codes=frozenset({"IMD-1002"}),
    subsidiary_codes=frozenset({"ETG"}))

SMALL = Need(vcpu=4, memory_gb=16)


# --- the acceptance check: different people see different lists --------------

def test_two_requesters_see_different_clusters():
    egate = {c.name for c in visible_clusters(ALL, EGATE_ENGINEER)}
    visa = {c.name for c in visible_clusters(ALL, VISA_ENGINEER)}

    assert egate == {"oke-egate-prod", "oke-shared-dev"}
    assert visa == {"oke-visa-prod", "oke-shared-dev"}


def test_another_teams_cluster_is_not_listed_even_as_ineligible():
    """The disclosure guard. Listing it greyed would still publish its name,
    its region and how big it is."""
    options = assess_clusters(ALL, EGATE_ENGINEER, SMALL)
    names = {o.cluster.name for o in options}

    assert "oke-visa-prod" not in names
    assert not any("visa" in r.lower() for o in options for r in o.reasons)


def test_unscoped_infrastructure_is_visible_to_everyone():
    """A cluster declaring no project, cost centre or subsidiary is shared."""
    for scope in (EGATE_ENGINEER, VISA_ENGINEER):
        assert SHARED in visible_clusters(ALL, scope)


def test_a_scope_the_caller_lacks_hides_the_cluster():
    outsider = RequesterScope(project_codes=frozenset({"OTHER"}))
    assert visible_clusters(ALL, outsider) == [SHARED]


# --- eligibility, which is shown with its reason -----------------------------

def test_a_cluster_without_the_capacity_says_what_it_has():
    options = assess_clusters([SHARED], EGATE_ENGINEER, Need(vcpu=16, memory_gb=64))
    refused = options[0]

    assert not refused.eligible
    assert any("16 vCPU" in r and "8 allocatable" in r for r in refused.reasons)
    assert any("64 GB" in r and "32 GB allocatable" in r for r in refused.reasons)


def test_a_quota_breach_names_the_ceiling():
    """The acceptance check: greyed, with the number that stopped it."""
    quota = Quota(label="IMD-1001 vCPU ceiling", limit=100, used=98)
    options = assess_clusters([EGATE], EGATE_ENGINEER, SMALL, [quota])
    refused = options[0]

    assert not refused.eligible
    reason = refused.reasons[0]
    assert "IMD-1001 vCPU ceiling" in reason
    assert "98 of 100" in reason
    assert "2 left" in reason
    assert "needs 4" in reason


def test_a_cluster_that_fits_within_quota_is_eligible():
    quota = Quota(label="IMD-1001 vCPU ceiling", limit=100, used=10)
    options = assess_clusters([EGATE], EGATE_ENGINEER, SMALL, [quota])

    assert options[0].eligible
    assert options[0].reasons == ()


def test_every_refusal_carries_at_least_one_reason():
    options = assess_clusters(ALL, EGATE_ENGINEER, Need(vcpu=999, memory_gb=999))
    for option in options:
        assert not option.eligible
        assert option.reasons, f"{option.cluster.name} refused with no reason"


# --- never trust a cluster id from the browser -------------------------------

def test_an_id_outside_the_callers_scope_is_refused():
    with pytest.raises(ClusterRefused):
        authorise_cluster(VISA.id, ALL, EGATE_ENGINEER, SMALL)


def test_that_refusal_does_not_reveal_the_cluster_exists():
    """An id the caller may not use and an id that does not exist must be
    indistinguishable, or the endpoint becomes a way to enumerate clusters.

    "Indistinguishable" means the same sentence, not the same bytes: each echoes
    back the id the caller themselves sent, which tells them nothing they did
    not already know. What must never differ is anything derived from whether
    the cluster is real — its name, its region, or the shape of the answer.
    """
    with pytest.raises(ClusterRefused) as forbidden:
        authorise_cluster(VISA.id, ALL, EGATE_ENGINEER, SMALL)
    with pytest.raises(ClusterRefused) as absent:
        authorise_cluster("ocid1.cluster.oc1..invented", ALL, EGATE_ENGINEER, SMALL)

    # Same template once each caller's own id is removed.
    assert (str(forbidden.value).replace(VISA.id, "ID")
            == str(absent.value).replace("ocid1.cluster.oc1..invented", "ID"))

    # And nothing the caller did not supply.
    for leaked in (VISA.name, VISA.region, str(VISA.allocatable_vcpu),
                   VISA.project_code, VISA.cost_centre_code):
        assert leaked not in str(forbidden.value)


def test_authorisation_refuses_rather_than_returns_a_flag():
    """A returned refusal can be ignored by a caller that forgets to check it;
    an exception cannot. This function is called immediately before persisting."""
    with pytest.raises(ClusterRefused):
        authorise_cluster(SHARED.id, ALL, EGATE_ENGINEER,
                          Need(vcpu=999, memory_gb=999))


def test_an_authorised_cluster_comes_back_eligible():
    option = authorise_cluster(EGATE.id, ALL, EGATE_ENGINEER, SMALL)
    assert option.eligible and option.cluster.name == "oke-egate-prod"


def test_a_quota_breach_is_refused_at_authorisation_too():
    """The list is a convenience; this is the control. A stale browser holding
    an id that was eligible a minute ago must not slip past."""
    quota = Quota(label="IMD-1001 vCPU ceiling", limit=100, used=99)
    with pytest.raises(ClusterRefused, match="ceiling"):
        authorise_cluster(EGATE.id, ALL, EGATE_ENGINEER, SMALL, [quota])


# --- stateful workloads on Kubernetes ----------------------------------------

def test_a_database_on_kubernetes_warns_and_names_the_alternative():
    warnings = stateful_warnings(["postgres16", "nodejs20"],
                                 managed_available=["postgres16"])

    assert len(warnings) == 1
    assert "postgres16" in warnings[0]
    assert "backups become your team's responsibility" in warnings[0]
    assert "Managed where available" in warnings[0]


def test_the_warning_still_appears_when_no_managed_option_exists():
    warnings = stateful_warnings(["mongodb"], managed_available=[])
    assert len(warnings) == 1
    assert "Managed where available" not in warnings[0]


def test_a_stateless_workload_is_not_warned_about():
    assert stateful_warnings(["nodejs20"], managed_available=["postgres16"]) == []


def test_the_warning_does_not_block():
    """Running a database on Kubernetes is a legitimate choice. The portal's job
    is to make sure it is made knowingly, not to forbid it."""
    options = assess_clusters([EGATE], EGATE_ENGINEER, SMALL)
    assert options[0].eligible
    assert stateful_warnings(["postgres16"], ["postgres16"])
