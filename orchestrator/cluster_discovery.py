"""Which Kubernetes clusters exist in the tenancy, and how big they are.

FETCHED LIVE, NEVER STORED AS CATALOGUE ROWS (ARCHITECTURE.md P7). A cluster's
capacity is the most perishable fact in the placement phase — it changes as pods
are scheduled and as node pools scale — so a row in the catalogue would be a
number that was true once. The caller caches it briefly and re-reads; nothing
here writes to the database.

WHY THE ORCHESTRATOR AND NOT THE API. Listing clusters needs OCI credentials and
the API holds none. Same reasoning, same signed channel and the same read-only
guarantee as `cloud_catalogue`: every call below is a `list_*` or a `get_*`, so
this module cannot create, change or destroy anything.

WHAT `allocatable` MEANS HERE, AND WHAT IT DOES NOT.

The figures reported are the node pools' TOTAL capacity — node count multiplied
by the shape's OCPUs and memory. True allocatable capacity is smaller: the
kubelet reserves memory and CPU for the system, the eviction threshold holds
more back, and whatever is already scheduled has taken its share. Reading that
needs the Kubernetes API.

NOTHING IN THIS SYSTEM CAN REACH A CLUSTER'S KUBERNETES API. The OKE blueprint
builds clusters with a private endpoint, in a VCN whose subnets are all private,
and this process has no route into it. So allocatable is not merely unread but
unreadable from here, and reporting total capacity as though it were allocatable
would overstate what a cluster can take.

It is therefore reported as an UPPER BOUND and said to be one. A fit judged
against it is optimistic, which is the honest direction to be wrong in while the
option that would consume it is refused anyway — and the day the Kubernetes API
becomes reachable, this is the function that starts returning the real number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ClusterDiscoveryUnavailable(RuntimeError):
    """The clusters could not be listed. The caller reports that as itself —
    never as "you have no clusters", which is a different fact entirely."""


def mode() -> str:
    """`live` reads the tenancy; anything else serves the mock fixture.

    Its own switch rather than OCI_CATALOGUE_MODE: listing clusters is a
    different permission from listing shapes, and an operator turning one on
    should not silently turn the other on with it.
    """
    return os.getenv("OCI_CLUSTER_DISCOVERY_MODE", "mock").strip().lower()


def is_live() -> bool:
    return mode() == "live"


def _compartment() -> str:
    return (os.getenv("OCI_COMPARTMENT_OCID") or "").strip()


@dataclass(frozen=True)
class DiscoveredCluster:
    """One cluster, as the cloud describes it.

    Deliberately NOT `api.clusters.Cluster`: this module knows nothing about
    entitlement, quotas or requests, and the shape that crosses the boundary is
    the cloud's answer rather than the portal's judgement of it.
    """

    id: str
    name: str
    region: str
    lifecycle_state: str
    kubernetes_version: str
    # The tag the portal writes when it builds. Empty for a cluster somebody
    # created by hand, which is not a reason to hide it — it is a reason the
    # requester may not be entitled to it, and that is api.clusters' decision.
    project_code: str
    cost_centre_code: str
    # Upper bounds. See the module docstring: this is the node pools' total
    # capacity, and true allocatable capacity is smaller by an amount nothing
    # here can see.
    capacity_vcpu: int
    capacity_memory_gb: int
    node_count: int

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "region": self.region,
            "lifecycle_state": self.lifecycle_state,
            "kubernetes_version": self.kubernetes_version,
            "project_code": self.project_code,
            "cost_centre_code": self.cost_centre_code,
            "capacity_vcpu": self.capacity_vcpu,
            "capacity_memory_gb": self.capacity_memory_gb,
            "node_count": self.node_count,
            # Carried on every row so a consumer cannot forget it. A field named
            # `allocatable` that holds capacity is how an optimistic number
            # becomes an authoritative one two layers later.
            "capacity_is_an_upper_bound": True,
        }


# A cluster the portal itself built, so the fixture exercises the tag path, and
# one created by hand, so it exercises the untagged path that entitlement has to
# refuse rather than crash on.
_MOCK_CLUSTERS = [
    DiscoveredCluster(
        id="ocid1.cluster.oc1.me-dubai-1.mock-egate-dev",
        name="oke-egate-dev", region="me-dubai-1", lifecycle_state="ACTIVE",
        kubernetes_version="v1.31.1", project_code="EGATE",
        cost_centre_code="IMD-1001",
        capacity_vcpu=24, capacity_memory_gb=96, node_count=3),
    DiscoveredCluster(
        id="ocid1.cluster.oc1.me-dubai-1.mock-unlabelled",
        name="oke-built-by-hand", region="me-dubai-1", lifecycle_state="ACTIVE",
        kubernetes_version="v1.30.1", project_code="", cost_centre_code="",
        capacity_vcpu=8, capacity_memory_gb=32, node_count=1),
]


def _containerengine_client():
    try:
        import oci  # noqa: PLC0415 — imported lazily; mock mode needs no SDK
    except ImportError as exc:  # pragma: no cover - the image ships the SDK
        raise ClusterDiscoveryUnavailable(
            "the OCI SDK is not installed in the orchestrator") from exc
    try:
        config = oci.config.from_file()
    except Exception as exc:  # noqa: BLE001 — the SDK raises many types
        raise ClusterDiscoveryUnavailable(
            f"OCI credentials are not usable: {exc}") from exc
    return oci.container_engine.ContainerEngineClient(config), config


def _shape_capacity(shape_config, ocpus_fallback: float = 0.0) -> tuple[int, int]:
    """OCPUs and memory for one node, as vCPUs.

    One OCPU is two vCPUs on the x86 flex shapes this tenancy uses, which is the
    same conversion `_instance_sizing` and `_placement_spec` make in the other
    direction. Stated here rather than left implicit so the two cannot drift.
    """
    ocpus = float(getattr(shape_config, "ocpus", 0) or ocpus_fallback or 0)
    memory = float(getattr(shape_config, "memory_in_gbs", 0) or 0)
    return int(ocpus * 2), int(memory)


def _live_clusters() -> list[DiscoveredCluster]:
    compartment = _compartment()
    if not compartment:
        raise ClusterDiscoveryUnavailable(
            "OCI_COMPARTMENT_OCID is not set — cannot list clusters.")

    client, config = _containerengine_client()
    region = str(config.get("region") or "")
    try:
        clusters = client.list_clusters(compartment_id=compartment).data
    except Exception as exc:  # noqa: BLE001 — the SDK raises many types
        raise ClusterDiscoveryUnavailable(str(exc)) from exc

    found = []
    for cluster in clusters:
        state = str(getattr(cluster, "lifecycle_state", "") or "")
        # A cluster being deleted or failing still EXISTS and is still listed.
        # Filtering here would make "the portal cannot see it" and "it is not
        # healthy" look the same to whoever is reading the list.
        try:
            pools = client.list_node_pools(compartment_id=compartment,
                                           cluster_id=cluster.id).data
        except Exception:  # noqa: BLE001 — one unreadable pool is not a failure
            pools = []

        vcpu = memory = nodes = 0
        for pool in pools:
            size = int(getattr(pool, "node_config_details", None)
                       and getattr(pool.node_config_details, "size", 0) or 0)
            per_node_vcpu, per_node_memory = _shape_capacity(
                getattr(pool, "node_shape_config", None))
            vcpu += per_node_vcpu * size
            memory += per_node_memory * size
            nodes += size

        tags = dict(getattr(cluster, "freeform_tags", None) or {})
        found.append(DiscoveredCluster(
            id=str(cluster.id), name=str(getattr(cluster, "name", "") or ""),
            region=region, lifecycle_state=state,
            kubernetes_version=str(getattr(cluster, "kubernetes_version", "") or ""),
            project_code=str(tags.get("project_code", "") or ""),
            cost_centre_code=str(tags.get("cost_centre_code", "") or ""),
            capacity_vcpu=vcpu, capacity_memory_gb=memory, node_count=nodes))
    return found


def fetch() -> dict:
    """Every cluster in the compartment, with its capacity.

    Raises `ClusterDiscoveryUnavailable` rather than returning an empty list when
    the tenancy cannot be read. The two must never look the same: "you have no
    clusters" is a statement about the requester, and "we could not look" is a
    statement about the portal, and only one of them is their problem.
    """
    clusters = _live_clusters() if is_live() else list(_MOCK_CLUSTERS)
    return {
        "mode": mode(),
        "clusters": [c.as_dict() for c in clusters],
        "count": len(clusters),
        # Repeated at the top level so a consumer reading only the envelope
        # still cannot mistake capacity for allocatable capacity.
        "capacity_is_an_upper_bound": True,
        "capacity_note": (
            "Node pool totals. True allocatable capacity is smaller and needs "
            "the Kubernetes API, which is not reachable from the orchestrator."),
    }
