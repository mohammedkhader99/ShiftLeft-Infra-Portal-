"""Live capacity reduction (GAP-ANALYSIS step 3, F-CAT).

Scaling a provisioned environment down for real. It goes through TERRAFORM rather
than a direct SDK call, because the request's workspace is the source of truth —
a direct API resize would show up as drift and could be reverted by the next
apply.

HONEST LIMIT: these tests prove the routing, the sizing maths, the refusals and
that Terraform is driven with the right inputs. They do NOT prove a real VM or
database resizes — that needs a provisioned resource in a real tenancy. Treat live
reduce as UNVERIFIED until exercised there.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as omain
from orchestrator import provisioner

client = TestClient(omain.app)

_SECRET = omain.WEBHOOK_SECRET


def _signed(payload: dict):
    body = json.dumps(payload).encode()
    sig = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "Content-Type": "application/json"}


def _payload(kind="oci-instance", reductions=None, components=None):
    return {
        "contract_version": 1,
        "reference": "REQ-2026-0100",
        "jira_key": "SDIMD-1",
        "operation": "reduce",
        "target": {
            "reference": "REQ-2026-0042",
            "resource_kind": kind,
            "policy_input": {
                "environment_name": "egate-uat",
                "cost_centre_code": "IMD-1001",
                "data_classification": "internal",
                "components": components if components is not None
                else [{"technology_code": "compute-vm", "size": "large"}],
            },
        },
        "reductions": reductions if reductions is not None
        else [{"technology": "compute-vm", "from": "large", "to": "small"}],
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("REDUCE_MODE", "OCI_PSQL_SHAPE_FAMILY", "CONFIG_ENABLED"):
        monkeypatch.delenv(k, raising=False)


# --- Mock stays the default --------------------------------------------------

def test_mock_reduces_nothing():
    body, headers = _signed(_payload())
    r = client.post("/reduce", content=body, headers=headers)
    assert r.status_code == 200
    assert "nothing resized" in r.json()["summary"]


def test_signature_is_required():
    body = json.dumps(_payload()).encode()
    assert client.post("/reduce", content=body,
                       headers={"X-Signature": "bad"}).status_code == 401


# --- Live routing + refusals -------------------------------------------------

def test_live_refuses_a_resource_with_no_capacity(monkeypatch):
    """A bucket has no size to reduce — refuse rather than pretend."""
    monkeypatch.setenv("REDUCE_MODE", "live")
    body, headers = _signed(_payload(kind="oci-bucket"))
    r = client.post("/reduce", content=body, headers=headers)
    assert r.status_code == 501
    assert "no resizable capacity" in r.json()["detail"]


def test_live_with_no_reductions_does_nothing(monkeypatch):
    monkeypatch.setenv("REDUCE_MODE", "live")
    body, headers = _signed(_payload(reductions=[]))
    r = client.post("/reduce", content=body, headers=headers)
    assert r.json()["reduced"] is False


def test_live_surfaces_a_terraform_failure(monkeypatch):
    monkeypatch.setenv("REDUCE_MODE", "live")

    def _boom(*a, **k):
        raise provisioner.ProvisionError("apply is not enabled (PROVISION_MODE is not 'apply')")
    monkeypatch.setattr(provisioner, "terraform_plan", _boom)
    body, headers = _signed(_payload())
    r = client.post("/reduce", content=body, headers=headers)
    assert r.status_code == 400 and "Resize failed" in r.json()["detail"]


# --- Live resize drives Terraform with the REDUCED sizing --------------------

def _capture_terraform(monkeypatch, captured):
    def _plan(reference, name, tags, kind, sizing):
        captured["plan"] = {"reference": reference, "name": name, "kind": kind, "sizing": sizing}
        return {"summary": "Plan: 0 to add, 1 to change, 0 to destroy.", "output": ""}

    def _apply(reference, name, tags, kind, sizing):
        captured["apply"] = {"reference": reference, "kind": kind, "sizing": sizing}
        return {"summary": "Apply complete!", "outputs": {"instance_ocid": "ocid1.instance..x"}}

    monkeypatch.setattr(provisioner, "terraform_plan", _plan)
    monkeypatch.setattr(provisioner, "terraform_apply", _apply)


def test_live_resize_plans_and_applies_the_smaller_sizing(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("REDUCE_MODE", "live")
    _capture_terraform(monkeypatch, captured)
    body, headers = _signed(_payload())
    r = client.post("/reduce", content=body, headers=headers)
    assert r.status_code == 200
    # It re-planned the TARGET's workspace, not the reduce request's.
    assert captured["plan"]["reference"] == "REQ-2026-0042"
    # 'small' is 2 vCPU / 4 GB -> 1 OCPU / 4 GB, not the original 'large'.
    assert captured["plan"]["sizing"]["memory_gb"] == 4
    assert captured["apply"]["sizing"]["memory_gb"] == 4
    assert captured["apply"]["kind"] == "oci-instance"


def test_live_resize_reports_the_restart(monkeypatch):
    """A resize is not invisible — the response must say the resource restarted."""
    monkeypatch.setenv("REDUCE_MODE", "live")
    _capture_terraform(monkeypatch, {})
    body, headers = _signed(_payload())
    out = client.post("/reduce", content=body, headers=headers).json()
    assert out["disruptive"] is True
    assert "restarted" in out["summary"]


def test_only_the_named_component_is_reduced(monkeypatch):
    """Components not named in the reductions keep their size."""
    captured: dict = {}
    monkeypatch.setenv("REDUCE_MODE", "live")
    _capture_terraform(monkeypatch, captured)
    payload = _payload(
        components=[{"technology_code": "compute-vm", "size": "small"},
                    {"technology_code": "rhel9", "size": "xlarge"}],
        reductions=[{"technology": "compute-vm", "from": "small", "to": "small"}],
    )
    body, headers = _signed(payload)
    client.post("/reduce", content=body, headers=headers)
    # rhel9 stays xlarge (16 vCPU / 128 GB), so sizing still reflects it.
    assert captured["plan"]["sizing"]["memory_gb"] == 128


# --- Managed database sizing -------------------------------------------------

def test_postgres_shape_tracks_the_sizing(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("REDUCE_MODE", "live")
    _capture_terraform(monkeypatch, captured)
    payload = _payload(
        kind="oci-postgres",
        components=[{"technology_code": "postgres16", "size": "large"}],
        reductions=[{"technology": "postgres16", "from": "large", "to": "medium"}],
    )
    body, headers = _signed(payload)
    r = client.post("/reduce", content=body, headers=headers)
    assert r.status_code == 200
    # 'medium' is 4 vCPU / 16 GB -> 2 OCPUs / 16 GB.
    assert captured["plan"]["sizing"]["db_shape"].endswith(".2.16GB")


def test_small_sizes_as_the_catalogue_prices_it():
    """Regression guard. An 8 GB floor used to be applied to EVERY request, so a
    'small' environment — priced at 2 vCPU / 4 GB — was built with 8 GB, and a
    reduction to 'small' silently changed nothing while reporting success."""
    sizing = omain._instance_sizing(
        {"policy_input": {"components": [{"technology_code": "compute-vm", "size": "small"}]}})
    assert sizing == {"ocpus": 1, "memory_gb": 4}


def test_unrecognisable_sizing_still_gets_a_safe_default():
    assert omain._instance_sizing({"policy_input": {"components": []}}) == {"ocpus": 1, "memory_gb": 8}
    assert omain._instance_sizing(
        {"policy_input": {"components": [{"size": "nonsense"}]}}) == {"ocpus": 1, "memory_gb": 8}


def test_postgres_shape_family_is_configurable(monkeypatch):
    monkeypatch.setenv("OCI_PSQL_SHAPE_FAMILY", "PostgreSQL.VM.Standard.E5.Flex")
    shape = omain._psql_shape({"ocpus": 4, "memory_gb": 64})
    assert shape == "PostgreSQL.VM.Standard.E5.Flex.4.64GB"
