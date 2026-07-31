"""Orchestrator checks: signature, approval, policy (1.9) + contract hardening (2.5)."""

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as orch
from common.signing import sign

client = TestClient(orch.app)

PAYLOAD = {
    "contract_version": "1.0",
    "idempotency_key": "INFRA-1001",
    "jira_key": "INFRA-1001",
    "reference": "REQ-2026-0001",
    "policy_input": {
        "request_type": "create",
        "deployment_target": "onprem",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
    },
    "approved_monthly": 672.0,
}


def _body(**overrides) -> bytes:
    return json.dumps({**PAYLOAD, **overrides}, sort_keys=True).encode()


def _signed(body: bytes) -> dict:
    return {"X-Signature": sign(orch.WEBHOOK_SECRET, body)}


@pytest.fixture(autouse=True)
def clear_ledger():
    orch._provisioned.clear()
    yield
    orch._provisioned.clear()


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def _patch(monkeypatch, *, approval_status="approved", policy_allow=True, current_monthly=672.0):
    monkeypatch.setattr(orch.httpx, "get", lambda *a, **k: Resp({"status": approval_status}))

    def fake_post(url, *a, **k):
        if url.endswith("/api/cost"):
            return Resp({"totals": {"monthly": current_monthly}})
        return Resp({"result": {"allow": policy_allow}})

    monkeypatch.setattr(orch.httpx, "post", fake_post)


def test_valid_signed_approved_compliant_provisions(monkeypatch):
    _patch(monkeypatch)
    body = _body()
    resp = client.post("/provision", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["provisioned"] is True
    assert resp.json()["verified"] == {"approval": True, "policy": True, "cost": True}


def test_bad_signature_is_rejected(monkeypatch):
    _patch(monkeypatch)
    body = _body()
    assert client.post("/provision", content=body, headers={"X-Signature": "bad"}).status_code == 401


def test_unsupported_contract_version_rejected(monkeypatch):
    _patch(monkeypatch)
    body = _body(contract_version="9.9")
    assert client.post("/provision", content=body, headers=_signed(body)).status_code == 400


def test_unapproved_request_is_refused(monkeypatch):
    _patch(monkeypatch, approval_status="pending")
    body = _body()
    assert client.post("/provision", content=body, headers=_signed(body)).status_code == 403


def test_policy_recheck_failure_is_refused(monkeypatch):
    _patch(monkeypatch, policy_allow=False)
    body = _body()
    assert client.post("/provision", content=body, headers=_signed(body)).status_code == 403


def test_cost_drift_above_threshold_halts(monkeypatch):
    # Approved 672; now re-prices at 900 (>10% over) -> refuse.
    _patch(monkeypatch, current_monthly=900.0)
    body = _body()
    resp = client.post("/provision", content=body, headers=_signed(body))
    assert resp.status_code == 409
    assert "cost re-validation" in resp.json()["detail"].lower()


def test_small_cost_drift_within_threshold_passes(monkeypatch):
    # 700 is within 10% of 672 -> allowed.
    _patch(monkeypatch, current_monthly=700.0)
    body = _body()
    assert client.post("/provision", content=body, headers=_signed(body)).status_code == 200


def test_idempotent_replay_provisions_once(monkeypatch):
    _patch(monkeypatch)
    body = _body()
    first = client.post("/provision", content=body, headers=_signed(body)).json()
    second = client.post("/provision", content=body, headers=_signed(body)).json()
    assert first.get("idempotent") is None
    assert second.get("idempotent") is True


def test_plan_mode_previews_and_creates_nothing(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "plan")
    monkeypatch.setattr(
        orch.provisioner, "terraform_plan",
        lambda ref, name, tags, *a: {"summary": "Plan: 1 to add, 0 to change, 0 to destroy.",
                                     "output": "..."},
    )
    body = _body()
    resp = client.post("/provision", content=body, headers=_signed(body)).json()
    assert resp["planned"] is True
    assert resp["provisioned"] is False
    assert "1 to add" in resp["plan_summary"]
    # A plan is not recorded in the idempotency ledger — nothing was created.
    assert orch._provisioned == {}


def test_provision_only_plans_in_apply_mode(monkeypatch):
    # In apply mode, /provision still ONLY plans (creation is the separate /apply).
    _patch(monkeypatch)
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setattr(orch.provisioner, "terraform_plan",
                        lambda ref, b, t, *a: {"summary": "Plan: 1 to add", "output": "..."})
    body = _body()
    resp = client.post("/provision", content=body, headers=_signed(body)).json()
    assert resp["planned"] is True and resp["provisioned"] is False


def test_apply_creates_resource(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setattr(orch.provisioner, "terraform_apply",
                        lambda ref, b, t, *a: {"summary": "Apply complete! Resources: 1 added.",
                                      "outputs": {"bucket_name": b}, "output": "..."})
    body = _body()
    resp = client.post("/apply", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["provisioned"] is True
    assert resp.json()["resource"]["name"]


def test_apply_refused_when_not_apply_mode(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "plan")
    body = _body()
    assert client.post("/apply", content=body, headers=_signed(body)).status_code == 501


def test_apply_is_idempotent(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    calls = {"n": 0}

    def fake_apply(ref, b, t, *a):
        calls["n"] += 1
        return {"summary": "Apply complete!", "outputs": {}, "output": ""}

    monkeypatch.setattr(orch.provisioner, "terraform_apply", fake_apply)
    body = _body()
    client.post("/apply", content=body, headers=_signed(body))
    second = client.post("/apply", content=body, headers=_signed(body)).json()
    assert second.get("idempotent") is True
    assert calls["n"] == 1  # created only once


def test_destroy_removes_resource(monkeypatch):
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setattr(orch.provisioner, "terraform_destroy",
                        lambda ref, b, t, *a: {"summary": "Destroy complete! Resources: 1 destroyed.",
                                      "output": "..."})
    body = _body()
    resp = client.post("/destroy", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["destroyed"] is True


# --- Collision-proof bucket names (increment 2.10) ---------------------------

def test_bucket_name_is_unique_per_request():
    # The bucket name suffixes the reference, so a generic env name like "test"
    # can't collide with an existing bucket (or another request's).
    name = orch._bucket_name({"environment_name": "test"}, "REQ-2026-0027")
    assert name == "test-req-2026-0027"

    a = orch._bucket_name({"environment_name": "test"}, "REQ-2026-0027")
    b = orch._bucket_name({"environment_name": "test"}, "REQ-2026-0099")
    assert a != b  # same env name, different requests -> different buckets


def test_bucket_name_is_oci_safe():
    # Spaces / illegal characters are collapsed to hyphens; result is lowercase.
    name = orch._bucket_name({"environment_name": "My Env!"}, "REQ-2026-0001")
    assert name == "my-env-req-2026-0001"


def test_bucket_and_tags_uses_derived_name():
    bucket, tags = orch._bucket_and_tags({
        "reference": "REQ-2026-0027",
        "policy_input": {"environment_name": "test", "cost_centre_code": "IMD-1001",
                         "data_classification": "internal"},
    })
    assert bucket == "test-req-2026-0027"
    assert tags["reference"] == "REQ-2026-0027"
