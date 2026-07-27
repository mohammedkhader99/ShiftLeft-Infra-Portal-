"""Increment 1.9 checks: the orchestrator verifies signature, approval, policy."""

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as orch
from common.signing import sign

client = TestClient(orch.app)

PAYLOAD = {
    "jira_key": "INFRA-1001",
    "reference": "REQ-2026-0001",
    "policy_input": {"request_type": "create"},
}
BODY = json.dumps(PAYLOAD, sort_keys=True).encode()


def _patch(monkeypatch, *, approval_status="approved", policy_allow=True):
    class Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    monkeypatch.setattr(
        orch.httpx, "get", lambda *a, **k: Resp({"status": approval_status})
    )
    monkeypatch.setattr(
        orch.httpx, "post", lambda *a, **k: Resp({"result": {"allow": policy_allow}})
    )


def test_valid_signed_approved_compliant_provisions(monkeypatch):
    _patch(monkeypatch)
    sig = sign(orch.WEBHOOK_SECRET, BODY)
    resp = client.post("/provision", content=BODY, headers={"X-Signature": sig})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provisioned"] is True
    assert body["verified"] == {"approval": True, "policy": True}


def test_bad_signature_is_rejected(monkeypatch):
    _patch(monkeypatch)
    resp = client.post("/provision", content=BODY, headers={"X-Signature": "deadbeef"})
    assert resp.status_code == 401


def test_unapproved_request_is_refused(monkeypatch):
    _patch(monkeypatch, approval_status="pending")
    sig = sign(orch.WEBHOOK_SECRET, BODY)
    resp = client.post("/provision", content=BODY, headers={"X-Signature": sig})
    assert resp.status_code == 403


def test_policy_recheck_failure_is_refused(monkeypatch):
    _patch(monkeypatch, policy_allow=False)
    sig = sign(orch.WEBHOOK_SECRET, BODY)
    resp = client.post("/provision", content=BODY, headers={"X-Signature": sig})
    assert resp.status_code == 403
