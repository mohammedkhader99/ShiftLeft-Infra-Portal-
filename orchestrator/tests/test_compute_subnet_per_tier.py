"""Ordinary machines are built in their own tier's network too.

Every resource kind except OKE took a single OCI_COMPUTE_SUBNET_OCID whatever
tier the request named. So a PRODUCTION nginx landed in the DEVELOPMENT subnet —
the exact failure the OKE map exists to prevent, left wide open for the twelve
technologies people actually raise, and far more likely to be hit than the
cluster case.

Staged deliberately. Until a tier declares a compute subnet the map is NOT in
force and the single setting still serves everything, so this changes nothing
until it is configured. The moment any tier declares one, silence from another
tier means somebody added tiers and forgot this one — not that the feature is
unused — and it is refused.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import orchestrator.main as omain
from orchestrator import oke_networks as nets
from orchestrator import provisioner

DEV = "ocid1.subnet.dev-vm-app"
SINGLE = "ocid1.subnet.the-one-setting"


def _payload(tier):
    return {"reference": "REQ-2026-0199", "resource_kind": "oci-service-vm",
            "resource_kinds": ["oci-service-vm"],
            "policy_input": {"environment_tier": tier, "environment_name": "env",
                             "components": [{"technology_code": "nginx",
                                             "size": "small"}]}}


@pytest.fixture(autouse=True)
def _single_setting(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_SUBNET_OCID", SINGLE)


def _in_force(monkeypatch):
    monkeypatch.setenv("OCI_OKE_NETWORKS",
                       json.dumps({"Development": {"compute": DEV}}))


# --- Not in force: nothing changes -------------------------------------------

def test_with_no_compute_mapping_the_single_setting_still_serves(monkeypatch):
    """The migration state. A change to the subnet every machine lands in must
    not alter behaviour the moment it ships."""
    monkeypatch.setenv("OCI_OKE_NETWORKS", json.dumps({"Development": {"vcn": "x"}}))
    spec = omain._compute_spec(_payload("Production"), "oci-service-vm")
    assert not spec.get("compute_subnet")
    assert provisioner._oci_vars("r", {}, "oci-service-vm", spec)["subnet_ocid"] == SINGLE


def test_with_no_map_at_all_the_single_setting_still_serves(monkeypatch):
    monkeypatch.delenv("OCI_OKE_NETWORKS", raising=False)
    spec = omain._compute_spec(_payload("Test"), "oci-service-vm")
    assert provisioner._oci_vars("r", {}, "oci-service-vm", spec)["subnet_ocid"] == SINGLE


# --- In force: the tier decides ----------------------------------------------

def test_a_mapped_tier_gets_its_own_subnet(monkeypatch):
    _in_force(monkeypatch)
    spec = omain._compute_spec(_payload("Development"), "oci-service-vm")
    assert provisioner._oci_vars("r", {}, "oci-service-vm", spec)["subnet_ocid"] == DEV


def test_an_unmapped_tier_is_refused_not_put_in_developments_subnet(monkeypatch):
    """THE failure. A Production machine in the development subnet would build,
    pass every check, and be found by whoever eventually looked."""
    _in_force(monkeypatch)
    with pytest.raises(HTTPException) as raised:
        omain._compute_spec(_payload("Production"), "oci-service-vm")
    assert raised.value.status_code == 400
    assert "Production" in str(raised.value.detail)
    assert DEV not in str(raised.value.detail), "the refusal must not leak another tier's subnet"


def test_the_single_setting_no_longer_wins_once_the_map_is_in_force(monkeypatch):
    """Leaving OCI_COMPUTE_SUBNET_OCID as a fallback for a MAPPED tier would
    quietly undo the whole thing."""
    _in_force(monkeypatch)
    spec = omain._compute_spec(_payload("Development"), "oci-service-vm")
    assert provisioner._oci_vars("r", {}, "oci-service-vm", spec)["subnet_ocid"] != SINGLE


def test_a_request_with_no_tier_is_refused_once_in_force(monkeypatch):
    _in_force(monkeypatch)
    with pytest.raises(HTTPException):
        omain._compute_spec(_payload(""), "oci-service-vm")


# --- The two maps do not interfere -------------------------------------------

def test_a_tier_may_have_compute_without_a_cluster(monkeypatch):
    """Most tiers will run machines long before anyone asks for Kubernetes.
    Demanding five OKE subnets before a single nginx could be built would be the
    rule obstructing the work it protects."""
    _in_force(monkeypatch)
    assert nets.compute_subnet("Development") == DEV
    with pytest.raises(nets.NetworkNotMapped):
        nets.for_tier("Development")


def test_oke_still_resolves_its_own_five(monkeypatch):
    monkeypatch.setenv("OCI_OKE_NETWORKS", json.dumps({"Development": {
        "compute": DEV, "vcn": "v", "api": "a", "node": "n", "pod": "p",
        "lb": "l", "bastion": "b"}}))
    spec = omain._compute_spec({**_payload("Development"),
                                "resource_kind": "oci-oke",
                                "resource_kinds": ["oci-oke"]}, "oci-oke")
    assert spec["node_subnet_id"] == "n"
