"""OKE: a customer-supplied Terraform module registered as a blueprint (F-CAT-10).

The module is the platform owner's, adapted only where the portal's fixed
variable contract differs from what it declared. These tests pin the adaptation
and the two portal-side changes it forced:

  * a Terraform command timeout per blueprint, because 10 minutes is fine for a
    VM and far too short for a Kubernetes cluster;
  * a node count derived from the request's size, because a node pool sized only
    by CPU and memory builds the same cluster whatever was priced.

The failure this guards against is not a broken plan. It is a plan that succeeds
and builds the wrong thing — the wrong size, an exposed bastion, or an apply
killed half way through that leaves resources nothing is tracking.
"""

import pytest

import orchestrator.main as omain
from orchestrator import blueprint_registry, provisioner

KIND = "oci-oke"


def _manifest():
    return blueprint_registry.for_resource_kind(KIND)


# --- The blueprint is wired up ------------------------------------------------

def test_the_blueprint_ships_and_dispatches_to_its_own_module():
    bp = _manifest()
    assert bp and bp["ref"] == "oci/oke"
    module = provisioner._module_dir("oci", KIND)
    assert module.name == "oke"
    # The customer's resource files came across, not just a skeleton.
    for f in ("oke-cluster.tf", "oke-nodepool.tf", "network.tf", "subnets.tf",
              "network-security-groups.tf", "bastion.tf", "variables.tf", "locals.tf"):
        assert (module / f).is_file(), f"{f} is missing from the copied module"


# --- An apply must not be killed part way through -----------------------------

def test_a_cluster_gets_longer_than_the_default_ten_minutes():
    """OCI takes 10-20 minutes to build a cluster. A timeout firing mid-apply
    leaves real, billing resources that Terraform never recorded and a later
    destroy cannot find."""
    assert provisioner._timeout_for(KIND) >= 1800


def test_every_other_blueprint_keeps_the_default():
    """The timeout is opt-in per recipe; nothing else changes behaviour."""
    for kind in ("oci-apache", "oci-bucket", "oci-instance"):
        assert provisioner._timeout_for(kind) == provisioner.DEFAULT_COMMAND_TIMEOUT
    assert provisioner._timeout_for("") == provisioner.DEFAULT_COMMAND_TIMEOUT
    assert provisioner._timeout_for("no-such-kind") == provisioner.DEFAULT_COMMAND_TIMEOUT


# --- It refuses to build something exposed ------------------------------------

def test_it_will_not_run_until_the_bastion_range_is_set(monkeypatch):
    """The bastion holds a public IP and is the only way in to a private
    Kubernetes API endpoint. No default is safe, so the blueprint declares the
    setting required and the portal refuses rather than exposing it."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.delenv("OCI_OKE_BASTION_CIDR", raising=False)

    with pytest.raises(provisioner.ProvisionError) as exc:
        provisioner._require_cloud("oci", KIND, creating=True)
    assert "OCI_OKE_BASTION_CIDR" in str(exc.value)

    monkeypatch.setenv("OCI_OKE_BASTION_CIDR", "203.0.113.0/24")
    provisioner._require_cloud("oci", KIND, creating=True)  # no longer refused


def test_tearing_down_is_never_blocked_by_that_gate(monkeypatch):
    """Otherwise unsetting the range would trap a running cluster."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.delenv("OCI_OKE_BASTION_CIDR", raising=False)
    provisioner._require_cloud("oci", KIND, creating=False)


# --- The request's size reaches the node pool ---------------------------------

def _payload(size):
    return {"policy_input": {"components": [{"technology_code": "oci-oke", "size": size}]}}


def test_node_count_follows_the_requested_size():
    assert omain._node_count(_payload("small")) == 2
    assert omain._node_count(_payload("medium")) == 3
    assert omain._node_count(_payload("large")) == 5
    assert omain._node_count(_payload("xlarge")) == 7


def test_an_unrecognised_size_does_not_build_a_cluster_by_accident():
    assert omain._node_count({"policy_input": {"components": []}}) == 1
    assert omain._node_count(_payload("enormous")) == 1


def test_the_largest_component_wins_like_the_shape_does():
    payload = {"policy_input": {"components": [
        {"technology_code": "oci-oke", "size": "small"},
        {"technology_code": "oci-oke", "size": "large"}]}}
    assert omain._node_count(payload) == 5


def test_the_node_count_and_shape_reach_terraform(monkeypatch):
    """Sized purely by the module's defaults, a small request would have built
    the same cluster as an xlarge one, and the price the approver approved would
    have meant nothing."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.setenv("OCI_OKE_BASTION_CIDR", "203.0.113.0/24")
    v = provisioner._cloud_vars("oci", "kube-uat", {"env": "uat"}, KIND,
                                {"ocpus": 4, "memory_gb": 64, "node_count": 5})
    assert v["node_count"] == 5
    assert v["instance_ocpus"] == 4 and v["instance_memory_gb"] == 64
    assert v["instance_name"] == "kube-uat"
    assert v["oke_bastion_allowed_cidr"] == "203.0.113.0/24"


def test_a_single_machine_blueprint_is_unaffected_by_node_count(monkeypatch):
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    v = provisioner._cloud_vars("oci", "web", {"env": "uat"}, "oci-apache", {"ocpus": 1})
    assert v["node_count"] == 1  # present, and ignored by a module that never declares it
