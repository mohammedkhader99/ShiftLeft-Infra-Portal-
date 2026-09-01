"""Asking a resource itself whether it is healthy, when no machine can report.

WHY THIS EXISTS. A machine proves itself by writing a self-report at the end of
first boot. A Kubernetes cluster cannot: OKE's worker nodes boot an Oracle-managed
image this project never renders, so there is no cloud-init to carry a report and
there never will be. Nor can a bucket or a managed database — they have no
operating system at all.

Until now the certification gate let those through vacuously, on the grounds that
"boots no machine" means "no boot evidence applies". True, and not the same as
"nothing to prove": postgres16 and oci-objectstorage are certifiable today with
no evidence whatsoever behind them.

So the resource is asked directly. Terraform exiting zero says an API call was
accepted; this says the thing it created reached a working state and stayed there
— which is the same distinction the boot reports draw for machines, and the same
one that was wrong four times in a week.

READ-ONLY. It reads lifecycle states and node counts; it changes nothing.
"""

from __future__ import annotations

import os

# Resource kinds this can speak for, and what "working" means for each. A kind
# absent from here is NOT silently healthy — verify() says it cannot judge, and
# the portal treats that as unproven rather than as permission.
#: Kinds that boot a COMPUTE INSTANCE. Every one of these is a VM with a
#: display name, so one check speaks for all of them: a new blueprint that
#: builds machines is covered the day it ships, without a line here.
COMPUTE_KINDS = ("oci-service-vm", "oci-instance", "oci-apache", "oci-kafka")

CHECKABLE = ("oci-oke", "oci-bucket", "oci-postgres") + COMPUTE_KINDS


class StateUnavailable(RuntimeError):
    """The resource could not be asked — not the same as an unhealthy answer."""


def _compartment() -> str:
    return (os.getenv("OCI_COMPUTE_COMPARTMENT_OCID")
            or os.getenv("OCI_COMPARTMENT_OCID") or "")


def _list_all(call, **kwargs) -> list:
    """Every page of a list call, or a single page where the SDK is absent.

    Pagination is not optional here. Listing images unpaginated returned 100 of
    212 in this tenancy, and the missing 112 included every Oracle Linux entry —
    a filter that looked like it worked and silently offered nothing. A cluster
    list past its first page would fail the same way, reporting "no cluster yet"
    for one that exists.

    The fallback exists so this module can be tested without the OCI SDK, which
    lives only in the orchestrator image.
    """
    try:
        import oci
    except ImportError:
        return list(call(**kwargs).data)
    return list(oci.pagination.list_call_get_all_results(call, **kwargs).data)


def _oke(name: str, clients) -> dict:
    """A cluster is working when it is ACTIVE and its nodes actually arrived.

    ACTIVE alone is not enough. A cluster whose node pool reports zero running
    nodes answers its API and runs nothing — the cluster-shaped version of a VM
    that booted with no software on it.
    """
    container, _ = clients
    comp = _compartment()
    clusters = [c for c in _list_all(container.list_clusters, compartment_id=comp)
                if c.name == name
                and c.lifecycle_state not in ("DELETED", "DELETING")]
    if not clusters:
        return {"state": "waiting", "detail": f"no cluster named {name} yet"}
    cluster = clusters[0]
    if cluster.lifecycle_state != "ACTIVE":
        state = "waiting" if cluster.lifecycle_state == "CREATING" else "broken"
        return {"state": state,
                "detail": f"cluster is {cluster.lifecycle_state}, not ACTIVE"}

    pools = _list_all(container.list_node_pools, compartment_id=comp,
                      cluster_id=cluster.id)
    if not pools:
        return {"state": "broken",
                "detail": "cluster is ACTIVE but has no node pool, so it runs nothing"}
    problems = []
    for pool in pools:
        detail = container.get_node_pool(pool.id).data
        wanted = int(getattr(detail, "node_config_details", None).size
                     if getattr(detail, "node_config_details", None) else 0)
        nodes = [n for n in (detail.nodes or [])
                 if getattr(n, "lifecycle_state", "") == "ACTIVE"]
        if wanted and len(nodes) < wanted:
            problems.append(
                f"node pool {detail.name} has {len(nodes)} of {wanted} nodes active")
    if problems:
        # Nodes take minutes after the cluster reports ACTIVE, so this is a
        # 'waiting' rather than a failure: the caller decides when to stop.
        return {"state": "waiting", "detail": "; ".join(problems)}
    return {"state": "ok", "detail": f"cluster ACTIVE with {len(pools)} node pool(s)"}


def _bucket(name: str, clients) -> dict:
    _, object_storage = clients
    try:
        namespace = object_storage.get_namespace().data
        bucket = object_storage.get_bucket(namespace, name).data
    except Exception as exc:  # noqa: BLE001 - SDK raises many types
        if "not found" in str(exc).lower() or "404" in str(exc):
            return {"state": "waiting", "detail": f"no bucket named {name} yet"}
        raise StateUnavailable(f"could not read bucket {name}: {exc}") from exc
    return {"state": "ok", "detail": f"bucket exists in {bucket.namespace}"}


def _compute(name: str, clients) -> dict:
    """Are the machines of this environment running NOW.

    WHAT THIS ADDS. The boot report already proves the SOFTWARE — what was
    installed, its version, whether the service is active, which ports it
    opened — and it is written once, at the end of first boot. It is a
    photograph. A VM that booted perfectly and was stopped last week still
    carries a report saying everything is fine. REQ-2026-0250 asked the other
    question, and the answer was "no health check is defined for oci-service-vm".

    THE NAME THE LEDGER RECORDS IS NOT THE NAME OCI CARRIES, and the first
    version of this matched exactly and would have reported every machine in the
    tenancy as missing. The modules name instances

        format("%s-%02d", var.instance_name, count.index + 1)

    so the ledger's `test-req-2026-0250-service-vm` is `...-service-vm-01` in
    OCI. Found by running the check against the real cloud; the unit tests all
    passed, because their fakes carried the name this code expected.

    SEVERAL MACHINES IS NORMAL, not a fault: a Kafka quorum is three, named -01,
    -02 and -03 by the same rule. What is NOT normal is two instances sharing one
    display name — OCI permits it, and it would mean a re-apply built a second
    copy that the tenancy is quietly billing for.
    """
    import re

    compute = clients[2] if len(clients) > 2 else None
    if compute is None:
        raise StateUnavailable("no compute client, so no machine can be asked")

    # `name` or `name-NN`. Anchored at both ends so a longer environment name
    # that merely starts the same way cannot be mistaken for this one.
    belongs = re.compile(rf"^{re.escape(name)}(-\d{{2,}})?$")
    found = [i for i in _list_all(compute.list_instances,
                                  compartment_id=_compartment())
             if belongs.match(i.display_name or "")
             and i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    if not found:
        return {"state": "waiting", "detail": f"no instance named {name} yet"}

    names = [i.display_name for i in found]
    if len(set(names)) != len(names):
        return {"state": "broken",
                "detail": f"two instances share a display name under {name}; "
                          f"one of them is unaccounted for and still billing"}

    starting = [i for i in found
                if i.lifecycle_state in ("PROVISIONING", "STARTING")]
    stopped = [i for i in found
               if i.lifecycle_state not in ("RUNNING", "PROVISIONING", "STARTING")]
    if stopped:
        detail = "; ".join(f"{i.display_name} is {i.lifecycle_state}" for i in stopped)
        return {"state": "broken",
                "detail": f"{detail}; it will not become RUNNING on its own"}
    if starting:
        detail = "; ".join(f"{i.display_name} is {i.lifecycle_state}" for i in starting)
        return {"state": "waiting", "detail": f"{detail}, not yet RUNNING"}
    machine = "machine" if len(found) == 1 else "machines"
    return {"state": "ok",
            "detail": f"{len(found)} {machine} RUNNING ({', '.join(sorted(names))})"}


def _clients():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    cfg = _oci_config()
    return (oci.container_engine.ContainerEngineClient(cfg),
            oci.object_storage.ObjectStorageClient(cfg),
            oci.core.ComputeClient(cfg))


def check(resource_kind: str, name: str, clients=None) -> dict:
    """Whether this resource reached a working state.

    Returns {"state": ok | waiting | broken | unknown, "detail": str}. `unknown`
    means this kind cannot be judged here, and the caller must treat it as
    unproven — never as healthy. Conflating the two is what let a bucket be
    certified on no evidence at all.
    """
    kind = (resource_kind or "").strip()
    if kind not in CHECKABLE:
        return {"state": "unknown",
                "detail": f"no health check is defined for {kind or 'this resource'}"}
    if not _compartment():
        raise StateUnavailable("no compartment is configured, so nothing can be asked")
    clients = clients or _clients()
    if kind == "oci-oke":
        return _oke(name, clients)
    if kind == "oci-bucket":
        return _bucket(name, clients)
    if kind in COMPUTE_KINDS:
        return _compute(name, clients)
    # oci-postgres: the managed database reports its own lifecycle, and the
    # module already surfaces it through cloud_state. Left explicitly unhandled
    # rather than guessed at — an unknown answer blocks certification, which is
    # the safe direction.
    return {"state": "unknown",
            "detail": f"{kind} has no health check yet, so it cannot be proven"}
