"""The capture endpoint, and the one thing it must never do.

A capture reaches into a RUNNING machine and turns it into a persistent,
billable artefact. The only machine this platform has grounds to do that to is
one it built itself, to prove a recipe, and is about to destroy anyway.

Somebody's production database server is not ours to photograph. That is the
security property this file exists for, and it is checked before anything else
happens — before the compartment is resolved, before the SDK is touched.

Everything else here defends the same contract `api/proof.py` relies on: a
capture reports, it never raises, and it can never change a verdict.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as orch
from common.signing import sign
from orchestrator import golden_image

client = TestClient(orch.app)

PROOF = {
    "contract_version": "1.0",
    "proof": True,
    "reference": "PROOF-OCI-SERVICE-VM-20260825T093000",
    "technology_code": "dotnet8",
    "resource_kind": "oci-service-vm",
    "policy_input": {
        "request_type": "create",
        "deployment_target": "oci",
        "environment_tier": "Development",
        "environment_name": "proof-oci-service-vm-20260825t093000",
        "components": [{"technology_code": "dotnet8", "size": "small"}],
    },
}


def body(**overrides) -> bytes:
    return json.dumps({**PROOF, **overrides}, sort_keys=True).encode()


def signed(b: bytes) -> dict:
    return {"X-Signature": sign(orch.WEBHOOK_SECRET, b)}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("GOLDEN_IMAGES", "true")
    monkeypatch.setenv("CLOUD_STATE_MODE", "mock")


# --- the security line --------------------------------------------------------

def test_a_machine_that_is_not_a_proofs_own_is_never_captured():
    """THE test in this file. A capture is only defensible on a machine this
    platform built to prove a recipe and is about to throw away."""
    b = body(proof=False, reference="REQ-2026-0203")
    r = client.post("/capture-image", content=b, headers=signed(b))

    assert r.status_code == 200
    assert r.json()["captured"] is False
    assert "proof" in r.json()["detail"].lower()


def test_a_reference_that_merely_CLAIMS_to_be_a_proof_is_not_enough():
    """`proof: True` is the caller's own assertion about itself. The reference
    format is checked independently, by the same rule the rest of the
    orchestrator uses — a caller that could assert its own authority has none."""
    b = body(reference="REQ-2026-0203")
    r = client.post("/capture-image", content=b, headers=signed(b))

    assert r.json()["captured"] is False, (
        "a production reference was captured because the payload said 'proof'")


def test_an_unsigned_capture_is_refused():
    b = body()
    assert client.post("/capture-image", content=b).status_code == 401
    assert client.post("/capture-image", content=b,
                       headers={"X-Signature": "not-it"}).status_code == 401


# --- the contract api/proof.py depends on -------------------------------------

def test_a_refusal_is_a_200_not_an_error():
    """`api/proof.py` calls this after the verdict is decided. An HTTP error
    would travel back as a failed handoff and start looking like a failed proof;
    every outcome is therefore a 200 carrying `captured`."""
    b = body(proof=False)
    assert client.post("/capture-image", content=b, headers=signed(b)).status_code == 200


def test_a_healthy_proof_machine_is_captured():
    b = body()
    r = client.post("/capture-image", content=b, headers=signed(b))

    assert r.status_code == 200
    assert r.json()["captured"] is True
    assert r.json()["image_ocid"], "captured, but no image was named"


def test_the_switch_stops_captures():
    """The Admin console toggle. Switching it off must stop new images at once —
    it is the brake on a feature that spends money."""
    import os
    os.environ["GOLDEN_IMAGES"] = "false"
    try:
        b = body()
        r = client.post("/capture-image", content=b, headers=signed(b))
        assert r.json()["captured"] is False
        assert "GOLDEN_IMAGES" in r.json()["detail"]
    finally:
        os.environ["GOLDEN_IMAGES"] = "true"


# --- the module itself never raises -------------------------------------------

def test_capture_reports_rather_than_raising(monkeypatch):
    """Every failure is a Capture, never an exception. The caller has already
    decided the proof's verdict and must not be able to lose it here."""
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    monkeypatch.setattr(golden_image.cloud_state, "_require_oci_creds",
                        lambda: (_ for _ in ()).throw(RuntimeError("no creds")))

    result = golden_image.capture("PROOF-X-1", technology_code="x",
                                  instance_name="proof-x")
    assert result.ok is False
    assert "no creds" in result.detail


def test_a_name_that_could_not_be_an_image_is_refused():
    """The display name reaches an OCI API as an identifier. A catalogue code is
    not a trusted string — the same rule the package candidates follow."""
    for code in ("a; rm -rf /", "$(curl http://x)", "../../etc", ""):
        assert golden_image.capture("PROOF-X-1", technology_code=code,
                                    instance_name="proof-x").ok is False


def test_the_image_names_the_proof_that_justified_it():
    """An image nobody can trace back to a passing build is an image nobody
    dares delete, which is how storage bills grow without an owner."""
    name = golden_image.image_name("dotnet8", "PROOF-OCI-SERVICE-VM-20260825T093000")
    assert "dotnet8" in name
    assert "20260825t093000" in name


def test_capture_mode_follows_cloud_state(monkeypatch):
    """One switch, not two. Capture uses exactly the same credentials, SDK and
    compartment as cloud-state, so a second mode setting could only ever be set
    to a value that contradicts the first."""
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    assert golden_image.mode() == "live"
    monkeypatch.setenv("CLOUD_STATE_MODE", "mock")
    assert golden_image.mode() == "mock"
