"""Orchestrator checks: signature, approval, policy (1.9) + contract hardening (2.5)."""

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as orch
from orchestrator.tests.http_stub import stub_http
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
    http = stub_http(monkeypatch, orch)
    http.get = lambda *a, **k: Resp({"status": approval_status})

    def fake_post(url, *a, **k):
        if url.endswith("/api/cost"):
            return Resp({"totals": {"monthly": current_monthly}})
        return Resp({"result": {"allow": policy_allow}})

    http.post = fake_post


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


def test_refresh_mock_records_and_masks(monkeypatch):
    body = _body(operation="refresh",
                 target={"reference": "REQ-UAT"}, source={"reference": "REQ-PROD"}, mask=True)
    resp = client.post("/refresh", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["refreshed"] is True and resp.json()["masked"] is True
    assert "masked" in resp.json()["summary"]


def test_refresh_rejects_bad_signature():
    body = _body(operation="refresh")
    assert client.post("/refresh", content=body, headers={"X-Signature": "bad"}).status_code == 401


def test_refresh_live_is_unconfigured(monkeypatch):
    monkeypatch.setenv("REFRESH_MODE", "live")
    body = _body(operation="refresh", target={"reference": "T"}, source={"reference": "S"})
    assert client.post("/refresh", content=body, headers=_signed(body)).status_code == 501


def test_restore_mock_verifies(monkeypatch):
    body = _body(operation="restore", target={"reference": "REQ-UAT"},
                 backup={"id": 3, "label": "nightly"})
    resp = client.post("/restore", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["restored"] is True and resp.json()["verified"] is True


def test_restore_live_is_unconfigured(monkeypatch):
    monkeypatch.setenv("RESTORE_MODE", "live")
    body = _body(operation="restore", target={"reference": "T"}, backup={"id": 1, "label": "b"})
    assert client.post("/restore", content=body, headers=_signed(body)).status_code == 501


def test_reduce_mock_records(monkeypatch):
    body = _body(operation="reduce", target={"reference": "REQ-UAT"},
                 reductions=[{"technology": "postgres16", "from": "large", "to": "medium"}])
    resp = client.post("/reduce", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json()["reduced"] is True and "postgres16" in resp.json()["summary"]


def test_reduce_rejects_bad_signature():
    body = _body(operation="reduce")
    assert client.post("/reduce", content=body, headers={"X-Signature": "bad"}).status_code == 401


def test_reduce_live_refuses_a_resource_with_no_capacity(monkeypatch):
    """Live reduce is implemented (GAP-ANALYSIS step 3), but a bucket has no
    capacity to scale down — it refuses rather than pretending to resize."""
    monkeypatch.setenv("REDUCE_MODE", "live")
    body = _body(operation="reduce",
                 target={"reference": "T", "resource_kind": "oci-bucket"},
                 reductions=[{"technology": "x", "from": "large", "to": "small"}])
    assert client.post("/reduce", content=body, headers=_signed(body)).status_code == 501


def test_reduce_live_with_no_reductions_is_a_no_op(monkeypatch):
    monkeypatch.setenv("REDUCE_MODE", "live")
    body = _body(operation="reduce", target={"reference": "T"}, reductions=[])
    resp = client.post("/reduce", content=body, headers=_signed(body))
    assert resp.status_code == 200 and resp.json()["reduced"] is False


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


# --- what was destroyed, said as data (REQ-2026-0226) -------------------------
#
# A FULL teardown deliberately sweeps up workspaces the portal did not name, so
# a stack whose kinds changed since it was built does not leave the old resource
# running and billing. That sweep is why REQ-2026-0226 destroyed the right
# machine when the portal asked for the wrong one:
#
#     portal asked      ['oci-bucket']       the catalogue default 41 of 46
#                                            technologies still carry
#     workspace on disk  'oci-service-vm'
#     destroyed          'oci-service-vm'
#
# Only this layer knew that. It said so in prose inside `summary`, the portal
# marked its ledger from its own list instead, matched nothing, and a destroyed
# VM stayed on the books as active at 790.59 AED a month.

def _destroy_sweeping(monkeypatch, built, asked):
    monkeypatch.setattr(orch.provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setattr(orch.provisioner, "existing_workspaces", lambda ref: list(built))
    # `**k` because destroy now names the workspace explicitly: a placement can
    # put two machines on one resource kind, so the directory is no longer
    # derivable from the kind alone. A stub whose signature is narrower than the
    # function it stands in for fails on the call rather than on the behaviour.
    monkeypatch.setattr(
        orch.provisioner, "terraform_destroy",
        lambda ref, b, t, *a, **k: {"summary": "Destroy complete! Resources: 3 destroyed.",
                                    "output": "..."})
    body = _body(resource_kinds=list(asked))
    return client.post("/destroy", content=body, headers=_signed(body))


def test_destroy_reports_the_kinds_it_actually_destroyed(monkeypatch):
    """THE test. The portal cannot know this — the sweep happens here."""
    resp = _destroy_sweeping(monkeypatch, built=["oci-service-vm"],
                             asked=["oci-bucket"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["kinds"] == ["oci-service-vm"], resp.json()


def test_the_reported_kinds_are_what_was_swept_not_what_was_asked(monkeypatch):
    """Asserting the difference, not just the presence of a field: if this
    returned the request's own list it would agree with the portal and the
    ledger would still be wrong."""
    resp = _destroy_sweeping(monkeypatch, built=["oci-service-vm"],
                             asked=["oci-bucket"])

    assert "oci-bucket" not in resp.json()["kinds"], resp.json()


def test_nothing_to_destroy_reports_an_empty_list_not_a_missing_one(monkeypatch):
    """The portal falls back to its own list when `kinds` is absent, which is
    right for an OLD orchestrator and wrong here — this one knows, and what it
    knows is that nothing was destroyed."""
    resp = _destroy_sweeping(monkeypatch, built=[], asked=["oci-bucket"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["kinds"] == []
