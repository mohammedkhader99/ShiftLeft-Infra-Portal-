"""The plan is built from the placement, not from the component list (P.13, F-ORC-11).

The unit of work used to be a RESOURCE KIND, and that was right until a placement
could put two machines on one kind. "Separated" builds three machines that are
all `oci-instance`; one workspace per kind collapses them into a single VM. The
portal would then build something other than what was approved and report
success — the failure this whole phase exists to end, arriving one layer further
down than the places it has already been caught.

The other half is the shape. Sizing from the largest component is correct when a
machine holds one component and wrong when it holds three, because three
co-resident components need the SUM. The placement already computed that sum and
the approver already signed it, so it travels down rather than being re-derived.

THE WORKSPACE NAMING TESTS ARE THE DANGEROUS ONES. A workspace that cannot find
its state believes its resource does not exist: it plans to create a second one
and can never destroy the first. Real, running infrastructure is on the other end
of every assertion in that section.
"""

import orchestrator.main as omain
from orchestrator import provisioner

BASE_PAYLOAD = {
    "reference": "REQ-2026-9300",
    "resource_kind": "oci-instance",
    "resource_kinds": ["oci-instance"],
    "policy_input": {
        "environment_tier": "dev",
        "components": [
            {"technology_code": "postgres16", "size": "medium"},
            {"technology_code": "nodejs20", "size": "small"},
        ],
    },
}


def host(host_id, components, vcpu=8, memory=24, storage=300, *,
         kind="oci-instance", mode="vm", resolved=True):
    return {"id": host_id, "host_mode": mode, "components": components,
            "resource_kind": kind, "resolved": resolved,
            "vcpu": vcpu, "memory_gb": memory, "storage_gb": storage}


def placed(hosts, version=1, option="consolidated"):
    return {**BASE_PAYLOAD,
            "placement": {"version": version, "option_key": option,
                          "hosts": hosts}}


CONSOLIDATED = placed([host("host-1", ["postgres16", "nodejs20"])])
SEPARATED = placed(
    [host("host-1", ["postgres16"], 5, 20, 240),
     host("host-2", ["nodejs20"], 3, 5, 60)],
    version=2, option="separated")


# --- the same components, different placements, different plans --------------

def test_consolidating_builds_one_machine():
    units = omain._placement_units(CONSOLIDATED)
    assert len(units) == 1
    assert units[0]["components"] == ["postgres16", "nodejs20"]


def test_separating_builds_one_machine_per_host():
    """The regression the whole increment is about. Both hosts are oci-instance,
    so a per-kind loop would have planned one VM and silently dropped the other."""
    units = omain._placement_units(SEPARATED)
    assert len(units) == 2
    assert [u["components"] for u in units] == [["postgres16"], ["nodejs20"]]


def test_the_two_placements_do_not_produce_the_same_plan():
    """P.13's acceptance, stated directly."""
    one = omain._placement_units(CONSOLIDATED)
    many = omain._placement_units(SEPARATED)

    assert len(one) != len(many)
    assert omain._placement_spec(CONSOLIDATED, one[0])["ocpus"] != \
        omain._placement_spec(SEPARATED, many[0])["ocpus"]


def test_each_machine_gets_its_own_state():
    """Two machines sharing a workspace share a state file, and the second apply
    would propose destroying the first machine to build itself."""
    units = omain._placement_units(SEPARATED)
    assert len({u["workspace"] for u in units}) == 2


# --- the shape comes from the placement --------------------------------------

def test_the_shape_is_the_placements_and_not_the_largest_component():
    """Sizing from the largest component is right for one component on a machine
    and wrong for three: co-resident components need the sum, which the approver
    has already been shown and has signed."""
    unit = omain._placement_units(CONSOLIDATED)[0]
    spec = omain._placement_spec(CONSOLIDATED, unit)

    assert spec["ocpus"] == 4, "8 vCPU is 4 OCPUs on x86 flex"
    assert spec["memory_gb"] == 24
    assert spec["boot_volume_gb"] == 300


def test_an_even_vcpu_count_converts_the_same_way_on_both_paths():
    """A placement and a pre-placement request of the same size must ask the
    cloud for the same machine, or the two paths disagree about what 'small' is.

    Every sizing anchor in the catalogue has an EVEN vCPU count, which is the only
    reason the older path's round() has been safe."""
    pre = omain._instance_sizing({"policy_input": {"components": [
        {"technology_code": "x", "vcpu": 8, "memory_gb": 24},
    ]}})
    unit = omain._placement_units(CONSOLIDATED)[0]
    assert omain._placement_spec(CONSOLIDATED, unit)["ocpus"] == pre["ocpus"]


def test_an_odd_vcpu_count_rounds_up_and_never_down():
    """THE ROUNDING TEST, and it is about headroom rather than arithmetic.

    P.6 adds headroom and deliberately rounds UP: "a machine that needs 4.8 vCPU
    gets 5, never 4 — rounding a requirement down is how headroom becomes a
    shortfall". A 4 vCPU requirement plus 20% is 5, and Python's round(5/2) is 2,
    which is 4 vCPUs — the headroom spent undoing itself, and a machine built
    smaller than the figure the approver signed.
    """
    for vcpu, expected_ocpus in ((3, 2), (5, 3), (7, 4), (9, 5), (1, 1)):
        payload = placed([host("host-1", ["postgres16"], vcpu, 8, 100)])
        unit = omain._placement_units(payload)[0]
        ocpus = omain._placement_spec(payload, unit)["ocpus"]

        assert ocpus == expected_ocpus, f"{vcpu} vCPU -> {ocpus} OCPUs"
        assert ocpus * 2 >= vcpu, (
            f"{vcpu} vCPU became {ocpus * 2} vCPU — smaller than was priced")


def test_a_machine_never_gets_fewer_vcpus_than_the_placement_resolved():
    """The property the case above is one example of."""
    for vcpu in range(1, 33):
        payload = placed([host("host-1", ["postgres16"], vcpu, 8, 100)])
        unit = omain._placement_units(payload)[0]
        built = omain._placement_spec(payload, unit)["ocpus"] * 2
        assert built >= vcpu, f"{vcpu} vCPU was built as {built}"


def test_the_rest_of_the_spec_is_still_derived_as_before(monkeypatch):
    """Only the shape is overridden. The image, the subnet and the first-boot
    configuration are unchanged, so a placement cannot quietly drop them."""
    unit = omain._placement_units(CONSOLIDATED)[0]
    spec = omain._placement_spec(CONSOLIDATED, unit)
    ordinary = omain._compute_spec(CONSOLIDATED, "oci-instance")

    for key in ordinary:
        if key in ("ocpus", "memory_gb", "boot_volume_gb"):
            continue
        assert spec[key] == ordinary[key], key


# --- a managed service is not a machine --------------------------------------

def test_a_managed_host_builds_no_machine():
    payload = placed([
        host("managed-postgres16", ["postgres16"], None, None, None,
             kind="oci-postgresql", mode="managed"),
        host("host-1", ["nodejs20"], 3, 5, 60),
    ], option="managed")
    units = omain._placement_units(payload)

    assert len(units) == 1
    assert units[0]["components"] == ["nodejs20"]


def test_an_all_managed_placement_produces_no_units():
    """None, not an empty list: the caller falls back to the kind list, which is
    what actually provisions the managed service."""
    payload = placed([host("managed-postgres16", ["postgres16"], None, None, None,
                           kind="oci-postgresql", mode="managed")],
                     option="managed")
    assert omain._placement_units(payload) is None


# --- nothing is built at a guessed shape or from a guessed module -------------

def test_an_unsized_machine_is_refused_not_defaulted():
    """It was excluded from the cost the approver signed. Building it anyway
    means provisioning something nobody approved, at a shape nobody chose."""
    import pytest
    from fastapi import HTTPException

    payload = placed([host("host-1", ["opensearch"], None, None, None,
                           resolved=False)])
    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(payload)

    assert raised.value.status_code == 400
    assert "no resolved size" in raised.value.detail
    assert "host-1" in raised.value.detail


def test_a_host_no_blueprint_builds_is_refused():
    """A request for Python 3.12 once received an empty bucket because a missing
    mapping fell through to a default. Nothing says what to build, so nothing is
    built."""
    import pytest
    from fastapi import HTTPException

    payload = placed([host("host-1", ["mystery"], kind=None)])
    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(payload)

    assert "no certified blueprint builds" in raised.value.detail


def test_the_refusal_names_the_placement_version():
    """So the person reading the error can find the record it came from."""
    import pytest
    from fastapi import HTTPException

    payload = placed([host("host-1", ["opensearch"], None, None, None,
                           resolved=False)], version=7)
    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(payload)
    assert "version 7" in raised.value.detail


def test_a_managed_host_is_never_treated_as_unsized():
    """It has no shape because it needs none — the cloud runs it."""
    payload = placed([host("managed-postgres16", ["postgres16"], None, None, None,
                           kind="oci-postgresql", mode="managed", resolved=True)])
    omain._refuse_unsized_hosts(payload)  # must not raise


# --- a payload with no placement behaves exactly as it always did -------------

def test_no_placement_means_no_units():
    assert omain._placement_units(BASE_PAYLOAD) is None


def test_a_malformed_placement_is_ignored_rather_than_crashing():
    """An older or hand-made payload must not be able to break provisioning by
    carrying the wrong shape under a key this code now reads."""
    for bad in ({"placement": None}, {"placement": "consolidated"},
                {"placement": {}}, {"placement": {"hosts": []}},
                {"placement": {"hosts": "host-1"}}):
        assert omain._placement_units({**BASE_PAYLOAD, **bad}) is None


def test_nothing_is_refused_when_there_is_no_placement():
    omain._refuse_unsized_hosts(BASE_PAYLOAD)  # must not raise


# --- workspace naming: where real infrastructure is at stake ------------------

def test_one_machine_per_kind_keeps_the_directory_it_has_always_had():
    """THE STATE-SAFETY TEST. Every request built before placement existed has
    its state in a directory named for the resource kind. Renaming it would make
    the workspace unable to find that state, so it would plan to create a second
    machine and could never destroy the first."""
    unit = omain._placement_units(CONSOLIDATED)[0]
    assert unit["workspace"] == "oci-instance"
    assert provisioner.WORKSPACE_SEPARATOR not in unit["workspace"]


def test_only_a_kind_with_several_machines_is_suffixed():
    units = omain._placement_units(SEPARATED)
    assert all(u["workspace"].startswith("oci-instance__") for u in units)
    assert {u["workspace"] for u in units} == {"oci-instance__host-1",
                                              "oci-instance__host-2"}


def test_a_mixed_stack_suffixes_only_the_kind_that_needs_it():
    payload = placed([
        host("host-1", ["postgres16"], 5, 20, 240),
        host("host-2", ["nodejs20"], 3, 5, 60),
        host("host-3", ["nginx"], 2, 4, 50, kind="oci-nginx"),
    ], option="separated")
    workspaces = {u["workspace"] for u in omain._placement_units(payload)}

    assert workspaces == {"oci-instance__host-1", "oci-instance__host-2",
                          "oci-nginx"}


def test_the_kind_is_recoverable_from_every_workspace_name():
    """Destroy and drift find workspaces by listing the directory, so this is the
    only route back from a name on disk to the manifest saying how to tear it
    down. Get it wrong and a cluster gets the ten-minute default timeout and
    fails mid-teardown with the resources still live and still billing."""
    for kind in ("oci-instance", "oci-oke", "aws-s3"):
        assert provisioner.kind_of_workspace(kind) == kind
        suffixed = provisioner.workspace_name(kind, "host-9", shared=True)
        assert provisioner.kind_of_workspace(suffixed) == kind


def test_workspace_paths_differ_per_host(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    a = provisioner.workspace_path("REQ-P", "oci-instance__host-1")
    b = provisioner.workspace_path("REQ-P", "oci-instance__host-2")

    assert a != b
    assert a.parent == b.parent == tmp_path / "REQ-P"


def test_a_legacy_flat_workspace_still_wins(tmp_path, monkeypatch):
    """The flat layout is the durable record that a resource predates suffixed
    names. Real state sitting in it beats every naming rule."""
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    flat = tmp_path / "REQ-OLD"
    flat.mkdir()
    (flat / "terraform.tfstate").write_text("{}", encoding="utf-8")

    assert provisioner.workspace_path("REQ-OLD", "oci-instance__host-1") == flat


# --- naming the machines apart ------------------------------------------------

def test_several_machines_on_one_kind_are_named_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    units = omain._placement_units(SEPARATED)
    names = {omain._workspace_resource_name("egate", u["workspace"], "REQ-P",
                                            "oci-instance")
             for u in units}
    assert len(names) == 2


def test_a_single_machine_keeps_the_name_the_old_rule_gives_it(tmp_path, monkeypatch):
    """Terraform reads a rename as a change to live infrastructure, and for a
    compute instance it can force replacement — destroying and rebuilding a
    working machine."""
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    unit = omain._placement_units(CONSOLIDATED)[0]

    assert omain._workspace_resource_name("egate", unit["workspace"], "REQ-P",
                                          "oci-instance") == \
        omain._resource_name("egate", "oci-instance", "REQ-P", "oci-instance")


def test_the_kind_alone_would_have_named_these_machines_wrong(tmp_path, monkeypatch):
    """THE ONE THAT WOULD HAVE BITTEN, stated as the difference it prevents.

    Destroy and drift used to derive the name from the resource KIND. Both of
    these machines are `oci-instance`, so that rule gives them ONE name between
    them — and it is not the name either was planned under. A machine torn down
    under a name it was not built with is the class of mismatch that leaves real
    infrastructure running.
    """
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    units = omain._placement_units(SEPARATED)
    kind_derived = {omain._resource_name("egate", u["kind"], "REQ-P", "oci-instance")
                    for u in units}
    workspace_derived = {omain._workspace_resource_name("egate", u["workspace"],
                                                        "REQ-P", "oci-instance")
                         for u in units}

    assert len(kind_derived) == 1, "the old rule collapses both machines to one name"
    assert len(workspace_derived) == 2
    assert kind_derived.isdisjoint(workspace_derived)


def test_the_name_survives_a_round_trip_through_the_directory_name(tmp_path, monkeypatch):
    """Destroy has only what `existing_workspaces` read off disk. Starting from
    that string alone must reproduce the planned name."""
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    for unit in omain._placement_units(SEPARATED):
        planned = omain._workspace_resource_name("egate", unit["workspace"],
                                                 "REQ-P", "oci-instance")
        # Simulate the directory being listed, rather than reusing the unit.
        from_disk = provisioner.workspace_path("REQ-P", unit["workspace"]).name
        assert omain._workspace_resource_name("egate", from_disk, "REQ-P",
                                              "oci-instance") == planned


# --- a container host is not a machine ----------------------------------------
#
# FOUND BY ASKING WHAT SCENARIO B WOULD ACTUALLY BUILD, and it was worse than the
# false promise it was traced for. A workload placed on a cluster resolves to its
# OWN blueprint's resource kind — `oci-service-vm` for Node.js — so the machine
# filter accepted a container host and `_placement_spec` sized it. "Provision a
# Kubernetes cluster and run Node.js on it" would have built one virtual machine
# with Node.js on it and NO CLUSTER, because the cluster provider is excluded from
# the workloads and so had no host of its own. Reported as success.
#
# P.13's unit mechanism introduced that. Before it, the kind list would at least
# have built the cluster alongside a spurious VM.

ON_A_CLUSTER = placed(
    [{"id": "new-cluster", "host_mode": "container",
      "components": ["nodejs20"], "resource_kind": "oci-service-vm",
      "resolved": True, "vcpu": 3, "memory_gb": 5, "storage_gb": 60}],
    option="new-cluster")


def test_a_container_host_produces_no_machine():
    assert omain._placement_units(ON_A_CLUSTER) is None


def test_a_container_host_stops_the_handoff():
    """Refused rather than skipped. Skipping would build the rest of the request
    and leave the workload silently absent, which is the same shape of failure in
    a quieter form."""
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(ON_A_CLUSTER)

    assert raised.value.status_code == 400
    assert "deploys into a cluster" in raised.value.detail
    assert "new-cluster" in raised.value.detail


def test_the_refusal_says_a_machine_would_have_been_built_instead():
    """The reason names the actual consequence, because "unsupported" would not
    tell the reader that the alternative was silently wrong rather than absent."""
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        omain._refuse_unsized_hosts(ON_A_CLUSTER)
    assert "machines instead of the cluster" in raised.value.detail


def test_a_cluster_beside_machines_still_refuses_the_whole_handoff():
    """Half a build is not a build. A mixed layout must not quietly provision its
    machine half and drop the cluster half."""
    import pytest
    from fastapi import HTTPException

    mixed = placed([
        {"id": "new-cluster", "host_mode": "container", "components": ["nodejs20"],
         "resource_kind": "oci-service-vm", "resolved": True,
         "vcpu": 3, "memory_gb": 5, "storage_gb": 60},
        host("host-1", ["postgres16"], 5, 20, 240),
    ], option="new-cluster")

    with pytest.raises(HTTPException):
        omain._refuse_unsized_hosts(mixed)
    assert omain._placement_units(mixed) is not None, (
        "the machine half is still recognised; the REFUSAL is what stops it")


def test_a_managed_host_is_still_not_confused_with_a_container_one():
    """Both produce no machine, for different reasons: the cloud runs a managed
    service, and nothing can reach a cluster. Only one of them is an error."""
    managed_only = placed([
        host("managed-postgres16", ["postgres16"], None, None, None,
             kind="oci-postgres", mode="managed")], option="managed")

    omain._refuse_unsized_hosts(managed_only)  # must not raise
    assert omain._placement_units(managed_only) is None
