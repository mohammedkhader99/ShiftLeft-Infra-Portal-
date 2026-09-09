"""Which clusters exist, and who may see them (cluster discovery).

DORMANT BY DESIGN, AND BUILT ANYWAY. Both cluster options are refused, because
nothing in this system deploys a workload into a cluster — so this list changes
no outcome today. It tells a requester what they WOULD be entitled to, beside the
reason they cannot use it, and it means the day a deployment path exists the
entitlement answer is already arriving rather than starting from nothing.

The property that matters most is the one this phase keeps returning to:
"we could not look" and "you have none" must never look the same. One is a
statement about the portal and the other about the requester, and only one of
them is theirs to act on. A cache that served a stale list in place of a failed
read would erase that distinction, so it does not.

The second is that capacity here is an UPPER BOUND. Node pool totals are all the
OCI API can give; true allocatable capacity needs the Kubernetes API, which
nothing here can reach. A field named `allocatable` holding total capacity is how
an optimistic number becomes an authoritative one two layers later, so every row
carries the caveat.
"""

from __future__ import annotations

import pytest

import api.main as main
from orchestrator import cluster_discovery


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module state, so a test that left it warm would decide the
    next one's answer."""
    main._CLUSTER_CACHE["at"] = None
    main._CLUSTER_CACHE["clusters"] = None
    yield
    main._CLUSTER_CACHE["at"] = None
    main._CLUSTER_CACHE["clusters"] = None


class _Req:
    """The parts of a Request that scope a lookup."""

    def __init__(self, project="EGATE", cost_centre="IMD-1001", subsidiary=None):
        self.project_code = project
        self.cost_centre_code = cost_centre
        self.subsidiary = subsidiary


def _answer(clusters, ok=True, **extra):
    return {"ok": ok, "clusters": clusters, "count": len(clusters), **extra}


# --- what the orchestrator reads ---------------------------------------------

def test_mock_mode_returns_clusters_without_credentials(monkeypatch):
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    answer = cluster_discovery.fetch()

    assert answer["count"] == len(answer["clusters"]) > 0
    assert answer["mode"] == "mock"


def test_every_row_says_its_capacity_is_an_upper_bound(monkeypatch):
    """Carried per row, not only in the envelope: a consumer that reads one
    cluster out of the list still cannot mistake capacity for allocatable."""
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    answer = cluster_discovery.fetch()

    assert answer["capacity_is_an_upper_bound"] is True
    assert all(c["capacity_is_an_upper_bound"] for c in answer["clusters"])
    assert "Kubernetes API" in answer["capacity_note"]


def test_the_fixture_covers_a_cluster_nobody_tagged(monkeypatch):
    """A cluster somebody created by hand has no project tag. That is not a
    reason to hide it — it is a reason the requester may not be entitled to it,
    and that is api.clusters' decision, not this module's."""
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    tags = {c["project_code"] for c in cluster_discovery.fetch()["clusters"]}

    assert "" in tags, "no untagged cluster to exercise the entitlement path"
    assert any(tags - {""}), "and no tagged one either"


def test_a_tenancy_that_cannot_be_read_raises_rather_than_reporting_none(monkeypatch):
    """The distinction, at its source. Returning [] here would travel all the way
    to a requester as "you are entitled to no cluster"."""
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "live")
    monkeypatch.delenv("OCI_COMPARTMENT_OCID", raising=False)

    with pytest.raises(cluster_discovery.ClusterDiscoveryUnavailable) as raised:
        cluster_discovery.fetch()
    assert "OCI_COMPARTMENT_OCID" in str(raised.value)


def test_discovery_has_its_own_switch(monkeypatch):
    """Not OCI_CATALOGUE_MODE. Listing clusters is a different permission from
    listing shapes, and an operator turning one on should not silently turn the
    other on with it."""
    monkeypatch.setenv("OCI_CATALOGUE_MODE", "live")
    monkeypatch.delenv("OCI_CLUSTER_DISCOVERY_MODE", raising=False)

    assert cluster_discovery.is_live() is False


# --- what the API does with it ------------------------------------------------

def test_a_cluster_in_scope_is_offered(monkeypatch):
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: _answer([
        {"id": "c-1", "name": "oke-egate-dev", "region": "me-dubai-1",
         "project_code": "EGATE", "cost_centre_code": "IMD-1001",
         "capacity_vcpu": 24, "capacity_memory_gb": 96}]))

    assessed = main._discover_clusters(None, _Req())
    assert [c.cluster.name for c in assessed] == ["oke-egate-dev"]
    assert assessed[0].eligible


def test_another_projects_cluster_is_absent_not_refused(monkeypatch):
    """Entitlement stays a visibility boundary. Listing it as refused would
    publish its name, region and size to anyone who opens the form."""
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: _answer([
        {"id": "c-1", "name": "oke-egate-dev", "region": "me-dubai-1",
         "project_code": "EGATE", "cost_centre_code": "IMD-1001",
         "capacity_vcpu": 24, "capacity_memory_gb": 96},
        {"id": "c-2", "name": "oke-visa-prod", "region": "me-dubai-1",
         "project_code": "VISA", "cost_centre_code": "IMD-2002",
         "capacity_vcpu": 64, "capacity_memory_gb": 256}]))

    names = {c.cluster.name for c in main._discover_clusters(None, _Req())}
    assert names == {"oke-egate-dev"}


def test_an_untagged_cluster_is_not_silently_given_away(monkeypatch):
    """`RequesterScope.covers` treats a scope of None as "shared infrastructure,
    visible to all", which is right for a cluster somebody CHOSE to leave
    unscoped. A cluster discovered in the tenancy with no portal tags chose
    nothing — it is simply untagged — so discovery maps a missing tag to "" and
    it matches no scope. Handing it to every requester would be granting access
    nobody decided to grant."""
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: _answer([
        {"id": "c-9", "name": "oke-built-by-hand", "region": "me-dubai-1",
         "project_code": "", "capacity_vcpu": 8, "capacity_memory_gb": 32}]))

    assert main._discover_clusters(None, _Req()) == []


# --- "we could not look" is not "you have none" -------------------------------

def test_an_unreachable_orchestrator_reports_none_not_empty(monkeypatch):
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: None)
    assert main._discover_clusters(None, _Req()) is None


def test_a_tenancy_that_could_not_be_read_reports_none_not_empty(monkeypatch):
    """The orchestrator answers 200 with ok:false so the caller can tell a
    reachable-but-blind orchestrator from an unreachable one. Both are None here;
    neither is an empty list."""
    monkeypatch.setattr(main, "_orchestrator_clusters",
                        lambda: _answer([], ok=False, reason="credentials"))
    assert main._discover_clusters(None, _Req()) is None


def test_a_tenancy_with_no_clusters_reports_empty_not_none(monkeypatch):
    """The other side of the same distinction, and the one that is genuinely the
    requester's business."""
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: _answer([]))
    assert main._discover_clusters(None, _Req()) == []


# --- the cache ----------------------------------------------------------------

def test_the_list_is_cached_rather_than_fetched_per_screen(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_orchestrator_clusters",
                        lambda: calls.append(1) or _answer([]))

    main._discover_clusters(None, _Req())
    main._discover_clusters(None, _Req())
    assert len(calls) == 1


def test_a_failed_read_is_not_papered_over_with_a_stale_list(monkeypatch):
    """THE CACHE MUST NOT ERASE THE DISTINCTION. Serving yesterday's clusters
    when today's read failed would tell a requester they have access the portal
    could not actually confirm, and they would have no way to tell."""
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: _answer([
        {"id": "c-1", "name": "oke-egate-dev", "region": "me-dubai-1",
         "project_code": "EGATE", "cost_centre_code": "IMD-1001",
         "capacity_vcpu": 24, "capacity_memory_gb": 96}]))
    assert main._discover_clusters(None, _Req())

    main._CLUSTER_CACHE["at"] = None  # the window has passed
    monkeypatch.setattr(main, "_orchestrator_clusters", lambda: None)

    assert main._discover_clusters(None, _Req()) is None


def test_the_cache_window_is_short_because_the_figure_goes_stale():
    """Capacity changes as pods are scheduled. An hour would be a number that was
    true once, which is the reason P7 keeps this out of the catalogue at all."""
    assert 0 < main._CLUSTER_CACHE_SECONDS <= 900


# --- and none of it makes the refused option choosable ------------------------

def test_finding_clusters_does_not_make_the_option_available(monkeypatch):
    """The whole point of building this while it is dormant. Discovery answers
    "which clusters could you use"; the option is refused for a different reason
    entirely, and finding clusters must not quietly override it."""
    from api.placement import (EXISTING_CLUSTER_OPTION, HOST_MANAGED, HOST_VM,
                               ComponentFacts, enumerate_options)
    from api.clusters import Cluster, Need, RequesterScope, assess_clusters

    oke = ComponentFacts(code="oci-oke", host_modes=frozenset({HOST_MANAGED}),
                         provides_cluster=True)
    app = ComponentFacts(code="nodejs20", host_modes=frozenset({HOST_VM}))
    found = assess_clusters(
        [Cluster(id="c-1", name="oke-egate-dev", region="me-dubai-1",
                 project_code="EGATE", allocatable_vcpu=64,
                 allocatable_memory_gb=256)],
        RequesterScope(project_codes=frozenset({"EGATE"})),
        Need(vcpu=2, memory_gb=4))

    options = {o.key: o for o in enumerate_options([oke, app], found)}
    existing = options[EXISTING_CLUSTER_OPTION]

    assert existing.eligible is False
    assert "cannot deploy workloads into" in existing.reasons[0]
    assert existing.clusters, "and the list still reaches the requester"
