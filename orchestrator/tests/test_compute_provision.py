"""Orchestrator compute-provisioning checks: resource-kind routing, flex-shape
sizing, the compute-config gate, and the tfvars the module receives. No real
Terraform or cloud is touched.
"""

import pytest

import orchestrator.main as omain
from orchestrator import provisioner


# --- Payload helpers ---------------------------------------------------------

def test_resource_kind_defaults_to_bucket():
    assert omain._resource_kind({}) == "oci-bucket"
    assert omain._resource_kind({"resource_kind": "oci-instance"}) == "oci-instance"


def test_instance_sizing_from_largest_component():
    payload = {"policy_input": {"components": [
        {"technology_code": "compute-vm", "size": "small"},
        {"technology_code": "rhel9", "size": "large"},  # 8 vCPU / 64 GB -> 4 OCPUs
    ]}}
    assert omain._instance_sizing(payload) == {"ocpus": 4, "memory_gb": 64}


def test_instance_sizing_has_a_safe_minimum():
    assert omain._instance_sizing({"policy_input": {"components": []}}) == {"ocpus": 1, "memory_gb": 8}


# --- Compute-config gate -----------------------------------------------------

def _clear_compute_env(monkeypatch):
    for k in provisioner._COMPUTE_VARS:
        monkeypatch.delenv(k, raising=False)


def test_require_compute_raises_without_config(monkeypatch):
    _clear_compute_env(monkeypatch)
    with pytest.raises(provisioner.ProvisionError, match="not configured"):
        provisioner._require_compute()


def test_require_compute_passes_when_configured(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_SUBNET_OCID", "ocid1.subnet..s")
    monkeypatch.setenv("OCI_COMPUTE_IMAGE_OCID", "ocid1.image..i")
    monkeypatch.setenv("OCI_COMPUTE_SSH_AUTHORIZED_KEY", "ssh-rsa AAAA")
    provisioner._require_compute()  # no raise


# --- tfvars the module receives ----------------------------------------------

def test_oci_vars_compute_shape(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_SUBNET_OCID", "ocid1.subnet..s")
    monkeypatch.setenv("OCI_COMPUTE_IMAGE_OCID", "ocid1.image..i")
    monkeypatch.setenv("OCI_COMPUTE_SSH_AUTHORIZED_KEY", "ssh-rsa AAAA")
    v = provisioner._oci_vars("web-req-1", {"reference": "REQ-1"},
                              "oci-instance", {"ocpus": 4, "memory_gb": 64})
    assert v["resource_kind"] == "oci-instance"
    assert v["instance_name"] == "web-req-1" and v["bucket_name"] == ""
    assert v["instance_ocpus"] == 4 and v["instance_memory_gb"] == 64
    assert v["subnet_ocid"] == "ocid1.subnet..s"
    assert v["image_ocid"] == "ocid1.image..i"
    assert v["ssh_authorized_key"] == "ssh-rsa AAAA"


def test_oci_vars_bucket_default():
    v = provisioner._oci_vars("my-bucket", {"reference": "REQ-1"})
    assert v["resource_kind"] == "oci-bucket"
    assert v["bucket_name"] == "my-bucket" and v["instance_name"] == ""
