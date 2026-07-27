"""Mock orchestrator (1.9) — the execution layer, contract-hardened in 2.5.

A SEPARATE service (ARCHITECTURE.md §3). It does NOT trust the signature for
authority: it independently re-verifies the approval (mock "Jira" = the API) and
re-checks OPA before acting (§4). 2.5 adds the production-grade contract:
- contract version check,
- idempotency (a given request provisions at most once, F-ORC-01),
- a cost re-validation gate that halts if the price drifted above the approved
  estimate beyond a threshold (F-ORC-09).

Execution is still mock — nothing real is created (USE_MOCK). Real Terraform
provisioning into a sandbox is the next increment, behind PROVISION_MODE.
"""

import json
import os

import httpx
from fastapi import FastAPI, HTTPException, Request

from common.signing import verify

API_URL = os.getenv("API_URL", "http://localhost:8081")
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-mock-secret")
SUPPORTED_CONTRACT = "1.0"
COST_DRIFT_THRESHOLD = float(os.getenv("COST_DRIFT_THRESHOLD", "0.10"))  # 10%

app = FastAPI(title="Mock Orchestrator")

# Idempotency ledger: idempotency_key -> the result we returned. In a real
# orchestrator this is durable state; in the mock it is in-process.
_provisioned: dict[str, dict] = {}


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _current_monthly(policy_input: dict) -> float | None:
    """Re-price the request now, to compare against the approved estimate."""
    body = {
        "deployment_target": policy_input.get("deployment_target"),
        "components": policy_input.get("components", []),
    }
    resp = httpx.post(f"{API_URL}/api/cost", json=body, timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("totals", {}).get("monthly")


@app.post("/provision")
async def provision(request: Request) -> dict:
    body = await request.body()

    # 1) Authenticity: verify the HMAC signature. Trust it for identity only.
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = json.loads(body)
    if payload.get("contract_version") != SUPPORTED_CONTRACT:
        raise HTTPException(status_code=400, detail="Unsupported contract version.")

    key = payload["idempotency_key"]
    reference = payload["reference"]
    jira_key = payload["jira_key"]
    policy_input = payload.get("policy_input", {})

    # 2) Idempotency: if we've already provisioned this key, return that result.
    if key in _provisioned:
        return {**_provisioned[key], "idempotent": True}

    # 3) Authority: independently re-verify the approval in Jira (mock = API).
    try:
        approval = httpx.get(f"{API_URL}/api/approvals/{jira_key}", timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-verify approval: {exc}")
    if approval.get("status") != "approved":
        raise HTTPException(status_code=403, detail="Approval not confirmed in Jira.")

    # 4) Re-check OPA policy before executing.
    try:
        result = httpx.post(
            f"{OPA_URL}/v1/data/infra/authz", json={"input": policy_input}, timeout=5.0
        ).json().get("result", {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-check policy: {exc}")
    if not result.get("allow"):
        raise HTTPException(status_code=403, detail="Policy re-check failed at execution.")

    # 5) Cost re-validation gate (F-ORC-09): halt if the price drifted up.
    approved = payload.get("approved_monthly")
    if approved is not None:
        try:
            current = _current_monthly(policy_input)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not re-price: {exc}")
        if current is not None and current > approved * (1 + COST_DRIFT_THRESHOLD):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cost re-validation failed: monthly {current:.2f} exceeds approved "
                    f"{approved:.2f} by more than {int(COST_DRIFT_THRESHOLD * 100)}%."
                ),
            )

    # 6) Mock provision — nothing real is created (USE_MOCK).
    provisioned = {
        "provisioned": True,
        "reference": reference,
        "jira_key": jira_key,
        "verified": {"approval": True, "policy": True, "cost": True},
        "message": f"Mock-provisioned {reference} (no real resources created).",
    }
    _provisioned[key] = provisioned
    return provisioned
