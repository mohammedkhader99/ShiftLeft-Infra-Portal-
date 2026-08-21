"""The orchestrator re-verifies a proof's authority itself (C2).

ARCHITECTURE.md §4 lets the certification runner provision without a Jira
approval, bounded by a sandbox tier and a cost cap. The orchestrator checks those
bounds against ITS OWN configuration rather than believing the payload, for the
same reason it re-verifies a Jira approval instead of trusting the signature: a
caller that could assert its own limits would have none.

Every test here sends a correctly SIGNED payload. The signature is never the
question — it proves who sent the request, never what they may do.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as orch
from common.signing import sign

client = TestClient(orch.app)

PROOF = {
    "contract_version": "1.0",
    "proof": True,
    "reference": "PROOF-OCI-OKE-20260821T093000",
    "policy_input": {
        "request_type": "create",
        "deployment_target": "oci",
        "environment_tier": "Development",
        "components": [{"technology_code": "oci-oke", "size": "small"}],
    },
}


def body(**overrides) -> bytes:
    merged = {**PROOF, **overrides}
    return json.dumps(merged, sort_keys=True).encode()


def signed(b: bytes) -> dict:
    return {"X-Signature": sign(orch.WEBHOOK_SECRET, b)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("CERTIFICATION_PROOF_ENABLED", "CERTIFICATION_SANDBOX_TIER",
                "CERTIFICATION_COST_CAP_MONTHLY"):
        monkeypatch.delenv(key, raising=False)
    orch._provisioned.clear()
    yield
    orch._provisioned.clear()


def allow(monkeypatch, tier="Development", cap="250", monthly=10.0):
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")
    monkeypatch.setenv("CERTIFICATION_SANDBOX_TIER", tier)
    monkeypatch.setenv("CERTIFICATION_COST_CAP_MONTHLY", cap)
    monkeypatch.setattr(orch, "_current_monthly", lambda pi: monthly)
    # OPA still applies to a proof; allow it so each test isolates one bound.
    monkeypatch.setattr(orch.httpx, "post",
                        lambda *a, **k: type("R", (), {
                            "json": lambda self=None: {"result": {"allow": True}}})())


def authorise(b: bytes) -> dict:
    return orch._authorise(b, sign(orch.WEBHOOK_SECRET, b))


# --- Each bound, checked here and not taken on trust -------------------------

def test_a_proof_is_refused_when_the_orchestrator_has_not_enabled_them(monkeypatch):
    """Both services must allow proofs. The API saying so is not enough."""
    monkeypatch.setattr(orch, "_current_monthly", lambda pi: 10.0)
    with pytest.raises(Exception) as exc:
        authorise(body())
    assert "403" in str(exc.value) or "not enabled" in str(exc.value).lower()


def test_a_proof_may_not_act_under_someone_elses_reference(monkeypatch):
    """The defence against a proof-flagged payload aimed at a real environment."""
    allow(monkeypatch)
    with pytest.raises(Exception) as exc:
        authorise(body(reference="REQ-2026-0153"))
    assert "not a proof reference" in str(exc.value)


def test_a_proof_may_not_build_outside_the_sandbox_tier(monkeypatch):
    """The bound that keeps proofs away from Production."""
    allow(monkeypatch, tier="Development")
    payload = json.loads(body())
    payload["policy_input"]["environment_tier"] = "Production"
    with pytest.raises(Exception) as exc:
        authorise(json.dumps(payload, sort_keys=True).encode())
    assert "sandbox tier" in str(exc.value)
    assert "Production" in str(exc.value)


def test_the_orchestrator_prices_the_proof_itself(monkeypatch):
    """The cap is applied to the orchestrator's OWN pricing.

    A payload that could carry its own price would have no ceiling — the same
    reason the Jira approval is re-verified rather than asserted.
    """
    allow(monkeypatch, cap="250", monthly=1240.0)
    with pytest.raises(Exception) as exc:
        authorise(body(approved_monthly=1.0))   # a lie in the payload
    assert "1,240" in str(exc.value) and "250" in str(exc.value)


def test_policy_still_applies_to_a_proof(monkeypatch):
    """A proof is not exempt from OPA just because it is the portal testing
    itself."""
    allow(monkeypatch)
    monkeypatch.setattr(orch.httpx, "post",
                        lambda *a, **k: type("R", (), {
                            "json": lambda self=None: {"result": {"allow": False}}})())
    with pytest.raises(Exception) as exc:
        authorise(body())
    assert "Policy re-check failed" in str(exc.value)


def test_a_well_formed_proof_within_every_bound_is_authorised(monkeypatch):
    allow(monkeypatch)
    payload = authorise(body())
    assert payload["reference"] == PROOF["reference"]
    # And it never acquired a Jira key it does not have.
    assert not payload.get("jira_key")


def test_a_proof_never_reaches_the_jira_check(monkeypatch):
    """If it did, the runner would need an approval that by design does not
    exist, and every proof would fail for the wrong reason."""
    allow(monkeypatch)
    called = []
    monkeypatch.setattr(orch.httpx, "get",
                        lambda *a, **k: called.append(a) or type("R", (), {
                            "json": lambda self=None: {"status": "approved"}})())
    authorise(body())
    assert called == [], "a proof asked Jira for an approval"


# --- Teardown, defended a second time at the executor ------------------------

def test_destroy_refuses_a_proof_aimed_at_a_real_environment(monkeypatch):
    """Defence in depth. /destroy takes authority from the signature alone, so a
    runner that was wrong — or a payload tampered with before signing — must
    still not be able to delete somebody's work."""
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    b = body(reference="REQ-2026-0153")
    r = client.post("/destroy", content=b, headers=signed(b))
    assert r.status_code == 403, r.text
    assert "not one" in r.text or "proof reference" in r.text


def test_destroy_is_unaffected_for_ordinary_requests(monkeypatch):
    """The new guard must not touch the normal teardown path — it applies only to
    payloads that declare themselves a proof."""
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    payload = json.loads(body(reference="REQ-2026-0153"))
    payload.pop("proof")
    b = json.dumps(payload, sort_keys=True).encode()
    r = client.post("/destroy", content=b, headers=signed(b))
    assert r.status_code != 403, "an ordinary teardown was blocked by the proof guard"
