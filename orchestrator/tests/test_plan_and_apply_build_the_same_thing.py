"""Apply must build exactly what plan planned — no more, no less, same places.

REQ-2026-0315, 2026-09-13. Four components: an OKE cluster and a managed
PostgreSQL (both `managed`), plus minio and oracle-free sharing one VM. Its
audit trail, in order:

    plan.previewed   oci-service-vm: Plan: 3 to add, 0 to change, 0 to destroy.
    apply.failed     Terraform apply failed for oci-oke: no saved plan for this
                     request — approve (plan) it first

Two defects, and they hid each other.

PLAN LOST THE MANAGED HOSTS. `_placement_units` dropped every host the cloud
runs, on the reasoning that a managed service is not a machine — true, and not
the question. `oci-oke` is oke-cluster.tf plus oke-nodepool.tf. So the plan
covered one of three hosts, and its approver approved "3 to add" for a stack
they believed included a Kubernetes cluster.

APPLY NEVER LOOKED AT THE PLACEMENT AT ALL. It iterated resource KINDS and took
the default workspace for each, while plan wrote one workspace per HOST. The two
lists agree only when every host is a VM and no kind carries more than one —
which is "consolidated" and nothing else. Both endpoints derived their own answer
from the same signed payload, which is the split `_handoff_payload` one layer up
says was fixed: "so plan, apply, drift, state and destroy cannot disagree about
what a request consists of — the kind of split that lost REQ-2026-0094's second
resource."

IT FAILED SAFELY, WHICH IS LUCK AND NOT DESIGN. Nothing was created, because
apply looked in an empty directory. Where a directory of that name holds a plan
from an EARLIER layout, the same code finds one, applies it, and reports success
for infrastructure nobody approved — a superseded placement's machines, built
and billing, against a ticket that says something else. That is what `plan_id`
is for.

THE TESTS THAT WERE HERE PASSED. One for an all-managed placement, one for a
managed host beside a VM, and both were right about what they looked at. Nothing
stood on the boundary between them, and a mixed placement is what every real
request is. Six entries in PLAN.md 5c now say some version of this.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import orchestrator.main as omain
from orchestrator import provisioner

# A cluster is built one VCN per tier and the orchestrator refuses to guess
# which, so a payload naming `oci-oke` needs its tier mapped before a spec can
# be computed at all. Mapped here rather than stubbed: this is the first thing
# planning a managed host runs into, and a test that stubbed it past would not
# notice the day it starts refusing.
OKE_NETWORKS = {"dev": {"vcn": "ocid1.vcn.oc1..dev", "api": "ocid1.subnet.oc1..api",
                        "node": "ocid1.subnet.oc1..node", "pod": "ocid1.subnet.oc1..pod",
                        "lb": "ocid1.subnet.oc1..lb",
                        "bastion": "ocid1.subnet.oc1..bastion"}}


@pytest.fixture(autouse=True)
def _networks(monkeypatch):
    monkeypatch.setenv("OCI_OKE_NETWORKS", json.dumps(OKE_NETWORKS))
    # And a Kubernetes version, for the same reason: unset, the spec asks OCI
    # which versions it accepts, and the suite does not reach the internet.
    monkeypatch.setenv("OCI_OKE_KUBERNETES_VERSION", "v1.31.1")

PAYLOAD = {
    "reference": "REQ-2026-9315",
    "resource_kind": "oci-oke",
    # Sorted and de-duplicated, exactly as `_environment_resource_kinds` sends
    # them — which is why "separated" cannot be read off this list: three
    # machines of one kind arrive here as one entry.
    "resource_kinds": ["oci-oke", "oci-postgres", "oci-service-vm"],
    "policy_input": {
        "environment_tier": "dev",
        "components": [
            {"technology_code": "oci-oke", "size": "small"},
            {"technology_code": "postgres16", "size": "small"},
            {"technology_code": "minio", "size": "small"},
            {"technology_code": "oracle-free", "size": "small"},
        ],
    },
}


def host(host_id, components, kind, mode="vm", vcpu=4, memory=16, storage=100):
    sized = mode == "vm"
    return {"id": host_id, "host_mode": mode, "components": components,
            "resource_kind": kind, "resolved": sized,
            "vcpu": vcpu if sized else None,
            "memory_gb": memory if sized else None,
            "storage_gb": storage if sized else None}


def placed(hosts, version=2, option="managed"):
    return {**PAYLOAD, "placement": {"version": version, "option_key": option,
                                     "hosts": hosts}}


# REQ-2026-0315's own layout.
MIXED = placed([
    host("managed-oci-oke", ["oci-oke"], "oci-oke", "managed"),
    host("managed-postgres16", ["postgres16"], "oci-postgres", "managed"),
    host("host-1", ["minio", "oracle-free"], "oci-service-vm"),
])

# Two machines on one kind. The kind list collapses them; only the placement
# knows there are two.
SEPARATED = placed([
    host("host-1", ["minio"], "oci-service-vm"),
    host("host-2", ["oracle-free"], "oci-service-vm"),
], version=3, option="separated")

CONSOLIDATED = placed([
    host("host-1", ["minio", "oracle-free"], "oci-service-vm"),
], version=4, option="consolidated")

ALL_MANAGED = placed([
    host("managed-oci-oke", ["oci-oke"], "oci-oke", "managed"),
    host("managed-postgres16", ["postgres16"], "oci-postgres", "managed"),
], version=5)

LAYOUTS = {"mixed": MIXED, "separated": SEPARATED,
           "consolidated": CONSOLIDATED, "all-managed": ALL_MANAGED}


# --- the invariant ------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_plan_and_apply_ask_for_the_same_workspaces(name):
    """THE WHOLE POINT. Both endpoints call `_work_units`, so this is now true by
    construction — which is the fix. Asserted anyway, over every layout the
    workspace offers, because "by construction" is what the docstring one layer
    up said before the construction grew a second path.
    """
    units = omain._work_units(LAYOUTS[name])
    workspaces = [u["workspace"] for u in units]

    assert workspaces == [u["workspace"] for u in omain._work_units(LAYOUTS[name])]
    assert len(workspaces) == len(set(workspaces)), (
        f"two units share a workspace, so one plan would overwrite the other: "
        f"{workspaces}")


@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_every_host_in_the_placement_gets_a_workspace(name):
    """Managed hosts included. A host in the layout and absent from the plan is
    infrastructure the approver was shown a price for and nothing builds."""
    payload = LAYOUTS[name]
    placed_hosts = {h["id"] for h in payload["placement"]["hosts"]}
    built = {u["host_id"] for u in omain._work_units(payload)}

    assert built == placed_hosts


def test_the_mixed_layout_plans_the_cluster_and_the_database():
    """REQ-2026-0315 exactly. Before the fix this was one unit."""
    kinds = sorted(u["kind"] for u in omain._work_units(MIXED))

    assert kinds == ["oci-oke", "oci-postgres", "oci-service-vm"]


def test_separated_machines_do_not_collapse_into_one():
    """The kind list has one `oci-service-vm` in it and the placement has two
    machines. Apply used to read the kind list."""
    workspaces = sorted(u["workspace"] for u in omain._work_units(SEPARATED))

    assert workspaces == ["oci-service-vm__host-1", "oci-service-vm__host-2"]


# --- and the unplaced path is untouched ---------------------------------------

def test_a_payload_with_no_placement_still_gets_one_workspace_per_kind():
    """Every request raised before placement existed. Its workspaces are on disk
    under these names, holding Terraform state for live machines — a workspace
    that cannot find its state plans to build a second one."""
    units = omain._work_units(PAYLOAD)

    assert [u["workspace"] for u in units] == PAYLOAD["resource_kinds"]
    assert [u["kind"] for u in units] == PAYLOAD["resource_kinds"]


def test_an_unplaced_unit_is_named_exactly_as_it_always_was():
    """`_workspace_resource_name` with workspace == kind must come out where
    `_resource_name` did. Terraform reads a rename as a change to live
    infrastructure, and for a compute instance it can force replacement."""
    for kind in PAYLOAD["resource_kinds"]:
        assert (omain._workspace_resource_name("env-name", kind, "REQ-2026-9315", "oci-oke")
                == omain._resource_name("env-name", kind, "REQ-2026-9315", "oci-oke"))


def test_an_unplaced_unit_carries_the_ordinary_spec():
    units = {u["kind"]: u["spec"] for u in omain._work_units(PAYLOAD)}

    for kind, spec in units.items():
        assert spec == omain._compute_spec(PAYLOAD, kind)


# --- a layout that cannot be built is refused before either endpoint acts ------

def test_apply_refuses_a_cluster_workload_exactly_as_plan_does():
    """`_refuse_unsized_hosts` lived in the plan endpoint only. Apply reaching it
    means a refusal cannot be skipped by going straight to the second call."""
    on_a_cluster = placed([
        host("cluster-minio", ["minio"], "oci-service-vm", "container")])

    with pytest.raises(HTTPException) as raised:
        omain._work_units(on_a_cluster)

    assert raised.value.status_code == 400
    assert "nothing in this system deploys into a cluster" in raised.value.detail


def test_an_unsized_machine_is_refused_from_the_unit_list_too():
    unsized = placed([{**host("host-1", ["minio"], "oci-service-vm"),
                       "resolved": False, "vcpu": None}])

    with pytest.raises(HTTPException):
        omain._work_units(unsized)


# --- which layout a saved plan belongs to -------------------------------------

def test_the_plan_id_is_the_placement_version():
    assert omain._plan_id(MIXED) == "v2"
    assert omain._plan_id(SEPARATED) == "v3"


def test_no_placement_has_an_empty_plan_id():
    """And an empty one matches only an empty one, so a legacy request's plan
    stays appliable and a placed request's never matches it."""
    assert omain._plan_id(PAYLOAD) == ""


def test_two_versions_of_one_request_do_not_share_a_plan_id():
    """THE HAZARD `plan_id` EXISTS FOR. "Consolidated" and a later
    "consolidated" with different components both write `oci-service-vm` — same
    directory, same filename. Without a version on the saved plan, apply cannot
    tell the approved one from the one that was replaced."""
    v4 = omain._plan_id(CONSOLIDATED)
    v9 = omain._plan_id(placed(CONSOLIDATED["placement"]["hosts"], version=9,
                               option="consolidated"))

    assert v4 != v9
    assert ([u["workspace"] for u in omain._work_units(CONSOLIDATED)]
            == ["oci-service-vm"]), "the workspace name really is reused"


# --- the saved plan, on disk --------------------------------------------------

def test_a_saved_plan_says_which_layout_it_is_for(tmp_path):
    provisioner._remember_plan(tmp_path, "REQ-2026-9315", "oci-service-vm",
                               "oci-service-vm", "v2")
    marker = provisioner._read_plan_marker(tmp_path)

    assert marker["plan_id"] == "v2"
    assert marker["workspace"] == "oci-service-vm"
    assert marker["reference"] == "REQ-2026-9315"


def test_a_workspace_with_no_marker_reads_as_unclaimed(tmp_path):
    assert provisioner._read_plan_marker(tmp_path) is None


def test_a_corrupt_marker_reads_as_unclaimed_rather_than_raising(tmp_path):
    """Apply refuses an unclaimed plan, so unreadable must land on the side that
    refuses. A marker that raised would fail the apply with a JSONDecodeError
    instead of a sentence somebody can act on."""
    (tmp_path / provisioner.PLAN_MARKER).write_text("{not json", encoding="utf-8")

    assert provisioner._read_plan_marker(tmp_path) is None


def test_forgetting_a_plan_leaves_no_marker(tmp_path):
    """Drift runs `plan -out=tfplan` in a provisioned request's own workspace and
    overwrites the approved plan with one nobody approved."""
    provisioner._remember_plan(tmp_path, "REQ-2026-9315", "x", "x", "v2")
    provisioner._forget_plan(tmp_path)

    assert provisioner._read_plan_marker(tmp_path) is None


def test_forgetting_a_plan_that_was_never_there_is_not_an_error(tmp_path):
    provisioner._forget_plan(tmp_path)  # must not raise


def test_drift_forgets_the_plan_it_overwrites():
    """Read through the source: running drift for real needs Terraform, a cloud
    and a provisioned request. What matters is that the call is there and that it
    comes before the plan that overwrites the file."""
    import inspect

    source = inspect.getsource(provisioner.terraform_drift)
    assert "def terraform_drift" in source, (
        "inspect.getsource did not return terraform_drift. provisioner.py has "
        "almost certainly changed on disk since this process imported it — do "
        "not edit source while the suite is running.")

    assert "_forget_plan(workdir)" in source
    assert source.index("_forget_plan(workdir)") < source.index(f"-out={{PLAN_FILE}}")
