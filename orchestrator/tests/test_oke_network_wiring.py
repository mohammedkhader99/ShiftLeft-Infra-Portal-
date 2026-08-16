"""A cluster is built in its own tier's network, or it is not built.

ONE VCN PER TIER (owner, 16 Aug 2026). The tier a request names decides which
network its cluster lands in, and the alternative to refusing an unmapped tier is
building a Production cluster inside the Development VCN — which would complete
successfully, pass every check, and be discovered by whoever eventually looked.

Wiring tests, not unit tests: orchestrator/oke_networks.py has its own, and all
of them passed while nothing called it. What matters here is that the resolved
OCIDs actually reach Terraform.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import orchestrator.main as omain
from orchestrator import provisioner

_OCIDS = {"vcn": "ocid1.vcn.dev", "api": "ocid1.subnet.api", "node": "ocid1.subnet.node",
          "pod": "ocid1.subnet.pod", "lb": "ocid1.subnet.lb",
          "bastion": "ocid1.subnet.bastion"}


@pytest.fixture(autouse=True)
def _mapped(monkeypatch):
    monkeypatch.setenv("OCI_OKE_NETWORKS", json.dumps({"Development": _OCIDS}))
    # The Kubernetes version is resolved against what OCI accepts, and there is
    # no OCI here. A configured version is the documented fallback for exactly
    # that case — these tests are about the NETWORK reaching Terraform, and
    # should not fail for a reason that has nothing to do with it.
    monkeypatch.setenv("OCI_OKE_KUBERNETES_VERSION", "v1.36.1")


def _payload(tier="Development"):
    return {"reference": "REQ-2026-0199", "resource_kind": "oci-oke",
            "resource_kinds": ["oci-oke"],
            "policy_input": {"environment_tier": tier, "environment_name": "env",
                             "components": [{"technology_code": "oci-oke",
                                             "size": "small"}]}}


def test_the_resolved_network_reaches_the_spec():
    spec = omain._compute_spec(_payload(), "oci-oke")
    for key, ocid in (("vcn_id", "vcn"), ("api_subnet_id", "api"),
                      ("node_subnet_id", "node"), ("pod_subnet_id", "pod"),
                      ("lb_subnet_id", "lb"), ("bastion_subnet_id", "bastion")):
        assert spec[key] == _OCIDS[ocid], f"{key} did not reach the spec"


def test_the_spec_reaches_terraform():
    """THE wiring. A spec carrying the right OCIDs and a provisioner that drops
    them is a cluster built with no network — which Terraform refuses, but only
    after an approval has been spent."""
    spec = omain._compute_spec(_payload(), "oci-oke")
    tfvars = provisioner._oci_vars("r", {}, "oci-oke", spec)
    for key in ("vcn_id", "api_subnet_id", "node_subnet_id", "pod_subnet_id",
                "lb_subnet_id", "bastion_subnet_id"):
        assert tfvars.get(key), f"{key} never reached Terraform"
    assert tfvars["node_subnet_id"] == _OCIDS["node"]


def test_an_unmapped_tier_is_refused_not_defaulted():
    """The failure this exists to prevent: a Production cluster in the
    Development VCN. It would build, pass, and look like success."""
    with pytest.raises(HTTPException) as raised:
        omain._compute_spec(_payload("Production"), "oci-oke")
    assert raised.value.status_code == 400
    assert "Production" in str(raised.value.detail)


def test_a_request_with_no_tier_is_refused():
    """Older requests carry no tier at all. Silence must not select a network."""
    with pytest.raises(HTTPException):
        omain._compute_spec(_payload(""), "oci-oke")


def test_a_partial_mapping_is_refused(monkeypatch):
    """Four of five subnets is a cluster in the wrong place, and Terraform would
    not notice — every variable it requires would be present."""
    partial = dict(_OCIDS)
    partial.pop("pod")
    monkeypatch.setenv("OCI_OKE_NETWORKS", json.dumps({"Development": partial}))
    with pytest.raises(HTTPException) as raised:
        omain._compute_spec(_payload(), "oci-oke")
    assert "pod" in str(raised.value.detail)


def test_other_blueprints_are_untouched_by_the_map():
    """Only OKE consumes a network map. A service-vm takes the shared compute
    subnet, and breaking that while wiring OKE would take the whole catalogue
    down with it."""
    payload = _payload()
    payload["resource_kind"] = "oci-service-vm"
    payload["policy_input"]["components"] = [{"technology_code": "nginx", "size": "small"}]
    spec = omain._compute_spec(payload, "oci-service-vm")
    assert not spec.get("vcn_id"), "a non-OKE blueprint was handed OKE's network"


def test_an_unmapped_tier_does_not_break_other_blueprints(monkeypatch):
    """A service-vm request must not start failing because OKE has no map."""
    monkeypatch.delenv("OCI_OKE_NETWORKS", raising=False)
    payload = _payload("Production")
    payload["resource_kind"] = "oci-service-vm"
    payload["policy_input"]["components"] = [{"technology_code": "nginx", "size": "small"}]
    assert omain._compute_spec(payload, "oci-service-vm") is not None
