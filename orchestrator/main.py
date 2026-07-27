"""Mock orchestrator (increment 1.9) — the execution layer.

A SEPARATE service (ARCHITECTURE.md §3). It receives the signed approval
handoff and, crucially, does NOT trust the signature for authority: it
independently re-verifies the approval (in mock "Jira" = the API) and re-checks
OPA before doing anything (ARCHITECTURE.md §4). In mock mode it "provisions"
nothing real — it just returns a provisioned result.
"""

import json
import os

import httpx
from fastapi import FastAPI, HTTPException, Request

from common.signing import verify

API_URL = os.getenv("API_URL", "http://localhost:8081")
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-mock-secret")

app = FastAPI(title="Mock Orchestrator")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/provision")
async def provision(request: Request) -> dict:
    body = await request.body()

    # 1) Authenticity: verify the HMAC signature. Trust it for identity only.
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = json.loads(body)
    jira_key = payload["jira_key"]
    reference = payload["reference"]
    policy_input = payload.get("policy_input", {})

    # 2) Authority: independently re-verify the approval in Jira (mock = API).
    try:
        approval = httpx.get(f"{API_URL}/api/approvals/{jira_key}", timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-verify approval: {exc}")
    if approval.get("status") != "approved":
        raise HTTPException(status_code=403, detail="Approval not confirmed in Jira.")

    # 3) Re-check OPA policy before executing.
    try:
        result = httpx.post(
            f"{OPA_URL}/v1/data/infra/authz", json={"input": policy_input}, timeout=5.0
        ).json().get("result", {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-check policy: {exc}")
    if not result.get("allow"):
        raise HTTPException(status_code=403, detail="Policy re-check failed at execution.")

    # 4) Mock provision — nothing real is created (USE_MOCK).
    return {
        "provisioned": True,
        "reference": reference,
        "jira_key": jira_key,
        "verified": {"approval": True, "policy": True},
        "message": f"Mock-provisioned {reference} (no real resources created).",
    }
