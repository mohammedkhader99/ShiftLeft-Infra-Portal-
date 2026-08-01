"""Mock orchestrator (1.9) — execution layer. Contract-hardened (2.5), real OCI
plan (2.6a) and apply/destroy (2.6b).

A SEPARATE service (ARCHITECTURE.md §3). It never trusts the signature for
authority: it independently re-verifies the approval (mock "Jira" = the API) and
re-checks OPA + cost before acting (§4). PROVISION_MODE gates real work:
- mock  : returns a mock result, creates nothing.
- plan  : real `terraform plan` (still creates nothing).
- apply : /provision still only plans; the separate /apply endpoint creates.
"""

import json
import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request

from common.signing import verify
from orchestrator import cloud_state, provisioner

API_URL = os.getenv("API_URL", "http://localhost:8081")
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-mock-secret")
SUPPORTED_CONTRACT = "1.0"
COST_DRIFT_THRESHOLD = float(os.getenv("COST_DRIFT_THRESHOLD", "0.10"))


def _iac_scan_enforce() -> bool:
    """Whether HIGH-severity IaC findings block the handoff (F-SEC-03/04). Off by
    default: findings are scanned + reported but never block until enforced."""
    return os.getenv("IAC_SCAN_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(title="Mock Orchestrator")

_provisioned: dict[str, dict] = {}  # idempotency ledger for real creations


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _current_monthly(policy_input: dict) -> float | None:
    body = {
        "deployment_target": policy_input.get("deployment_target"),
        "components": policy_input.get("components", []),
    }
    resp = httpx.post(f"{API_URL}/api/cost", json=body, timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("totals", {}).get("monthly")


def _authorise(body: bytes, signature: str) -> dict:
    """Verify authenticity + authority + policy + cost. Returns the payload."""
    if not verify(WEBHOOK_SECRET, body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = json.loads(body)
    if payload.get("contract_version") != SUPPORTED_CONTRACT:
        raise HTTPException(status_code=400, detail="Unsupported contract version.")

    jira_key = payload["jira_key"]
    policy_input = payload.get("policy_input", {})

    # Authority: re-verify the approval in Jira (mock = API).
    try:
        approval = httpx.get(f"{API_URL}/api/approvals/{jira_key}", timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-verify approval: {exc}")
    if approval.get("status") != "approved":
        raise HTTPException(status_code=403, detail="Approval not confirmed in Jira.")

    # Re-check OPA policy.
    try:
        result = httpx.post(
            f"{OPA_URL}/v1/data/infra/authz", json={"input": policy_input}, timeout=5.0
        ).json().get("result", {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-check policy: {exc}")
    if not result.get("allow"):
        raise HTTPException(status_code=403, detail="Policy re-check failed at execution.")

    # Cost re-validation gate (F-ORC-09).
    approved = payload.get("approved_monthly")
    if approved is not None:
        try:
            current = _current_monthly(policy_input)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not re-price: {exc}")
        if current is not None and current > approved * (1 + COST_DRIFT_THRESHOLD):
            raise HTTPException(
                status_code=409,
                detail=(f"Cost re-validation failed: monthly {current:.2f} exceeds approved "
                        f"{approved:.2f} by more than {int(COST_DRIFT_THRESHOLD * 100)}%."),
            )
    return payload


def _bucket_name(policy_input: dict, reference: str) -> str:
    """A globally-unique, OCI-safe bucket name derived from the request.

    The environment name alone can collide with a bucket that already exists in
    the tenancy (e.g. a generic 'test'), so we suffix the request reference,
    which is unique per request. The environment name stays the human-facing
    label everywhere else; this is just the physical bucket's name.
    """
    env = (policy_input.get("environment_name") or "env").strip().lower()
    raw = f"{env}-{reference}".lower()
    # OCI bucket names allow letters, digits, hyphens, underscores and periods.
    safe = re.sub(r"[^a-z0-9._-]+", "-", raw)
    safe = re.sub(r"-+", "-", safe).strip("-")  # collapse runs of hyphens
    return safe or reference.lower()


def _bucket_and_tags(payload: dict) -> tuple[str, dict]:
    policy_input = payload.get("policy_input", {})
    bucket = _bucket_name(policy_input, payload["reference"])
    tags = {
        "managed_by": "infra-portal",
        "reference": payload["reference"],
        "cost_centre": str(policy_input.get("cost_centre_code", "")),
        "classification": str(policy_input.get("data_classification", "")),
    }
    if payload.get("ttl_expiry"):
        tags["ttl_expiry"] = str(payload["ttl_expiry"])
    return bucket, tags


# size -> (vcpu, memory_gb), mirroring db.seed.SIZES; used to size the compute
# flex shape when a request provisions an oci-instance.
_SIZES = {"small": (2, 4), "medium": (4, 16), "large": (8, 64), "xlarge": (16, 128)}


def _resource_kind(payload: dict) -> str:
    """oci-bucket (default) | oci-instance — what this request provisions/acts on."""
    return payload.get("resource_kind", "oci-bucket")


def _instance_sizing(payload: dict) -> dict:
    """OCI flex-shape sizing (ocpus, memory_gb) from the largest component size."""
    best = (1, 8)  # a safe minimum
    for c in payload.get("policy_input", {}).get("components", []):
        vcpu, mem = _SIZES.get((c.get("size") or "").lower(), (0, 0))
        if mem > best[1]:
            best = (max(1, round(vcpu / 2)), mem)  # 1 OCPU ~ 2 vCPUs on x86 flex
    return {"ocpus": best[0], "memory_gb": best[1]}


def _image_for(payload: dict) -> str:
    """The OS image for this request's compute component: a per-technology image
    from OCI_COMPUTE_IMAGE_MAP (JSON: technology_code -> image OCID) if one
    matches a component, else the default OCI_COMPUTE_IMAGE_OCID."""
    try:
        mapping = json.loads(os.getenv("OCI_COMPUTE_IMAGE_MAP", "") or "{}")
    except (ValueError, TypeError):
        mapping = {}
    if isinstance(mapping, dict):
        for c in payload.get("policy_input", {}).get("components", []):
            code = c.get("technology_code")
            if code and mapping.get(code):
                return mapping[code]
    return os.getenv("OCI_COMPUTE_IMAGE_OCID", "")


def _compute_spec(payload: dict) -> dict:
    """The compute instance's Terraform inputs: sizing + the resolved OS image."""
    return {**_instance_sizing(payload), "image_ocid": _image_for(payload)}


@app.post("/provision")
async def provision(request: Request) -> dict:
    """Approve handoff: plan only (mock returns a mock result). Creates nothing."""
    body = await request.body()
    payload = _authorise(body, request.headers.get("X-Signature", ""))
    reference, jira_key = payload["reference"], payload["jira_key"]
    key = payload["idempotency_key"]

    if key in _provisioned:
        return {**_provisioned[key], "idempotent": True}

    mode = provisioner.provision_mode()
    verified = {"approval": True, "policy": True, "cost": True}
    name, tags = _bucket_and_tags(payload)
    rkind = _resource_kind(payload)
    sizing = _compute_spec(payload)

    if mode in ("plan", "apply"):
        try:
            plan = provisioner.terraform_plan(reference, name, tags, rkind, sizing)
        except provisioner.ProvisionError as exc:
            raise HTTPException(status_code=400, detail=f"Terraform plan failed: {exc}")
        scan = plan.get("scan", {})
        # IaC scan gate (F-SEC-03/04): block on HIGH findings only when enforced;
        # otherwise the findings are reported in the response and audited by the API.
        if scan.get("high", 0) > 0 and _iac_scan_enforce():
            raise HTTPException(
                status_code=409,
                detail=(f"IaC scan blocked: {scan['high']} high-severity finding(s) in the "
                        f"terraform plan."))
        return {
            "provisioned": False, "planned": True, "reference": reference, "jira_key": jira_key,
            "verified": verified, "plan_summary": plan["summary"], "plan_output": plan["output"],
            "scan": scan,
            "message": f"Terraform plan for {reference}: {plan['summary']} — nothing created.",
        }

    provisioned = {
        "provisioned": True, "reference": reference, "jira_key": jira_key,
        "verified": verified,
        "message": f"Mock-provisioned {reference} (no real resources created).",
    }
    if rkind == "oci-instance":  # record a mock instance so the control plane can act
        provisioned["resource"] = {"kind": "oci-instance", "name": name,
                                   "region": os.getenv("OCI_REGION"), "outputs": {}}
    _provisioned[key] = provisioned
    return provisioned


@app.post("/apply")
async def apply(request: Request) -> dict:
    """Explicit apply: CREATES the real resource. Only in PROVISION_MODE=apply."""
    if provisioner.provision_mode() != "apply":
        raise HTTPException(status_code=501, detail="Real apply is not enabled (PROVISION_MODE).")

    body = await request.body()
    payload = _authorise(body, request.headers.get("X-Signature", ""))
    reference, jira_key = payload["reference"], payload["jira_key"]
    key = payload["idempotency_key"]

    if key in _provisioned:  # already created — do not create twice (F-ORC-01)
        return {**_provisioned[key], "idempotent": True}

    name, tags = _bucket_and_tags(payload)
    rkind = _resource_kind(payload)
    try:
        result = provisioner.terraform_apply(reference, name, tags, rkind, _compute_spec(payload))
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=400, detail=f"Terraform apply failed: {exc}")

    provisioned = {
        "provisioned": True, "reference": reference, "jira_key": jira_key,
        "verified": {"approval": True, "policy": True, "cost": True},
        "resource": {"kind": rkind, "name": name,
                     "region": os.getenv("OCI_REGION"), "outputs": result["outputs"]},
        "summary": result["summary"],
        "message": f"Provisioned {reference}: {result['summary']}",
    }
    _provisioned[key] = provisioned
    return provisioned


@app.post("/drift")
async def drift(request: Request) -> dict:
    """Detect drift for a provisioned request (F-LCM-09): re-plan its workspace
    and report changes vs the applied state. Signed + re-verified; read-only
    (runs `terraform plan`, creates nothing). Real terraform only (apply mode)."""
    if provisioner.provision_mode() != "apply":
        raise HTTPException(status_code=501,
                            detail="Drift detection needs apply mode (real terraform).")
    body = await request.body()
    payload = _authorise(body, request.headers.get("X-Signature", ""))
    reference = payload["reference"]
    name, tags = _bucket_and_tags(payload)
    try:
        result = provisioner.terraform_drift(reference, name, tags, _resource_kind(payload),
                                             _compute_spec(payload))
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=400, detail=f"Drift check failed: {exc}")
    return {"reference": reference, **result}


@app.post("/state")
async def state(request: Request) -> dict:
    """Report the actual cloud state of a request's resources (read-only cloud
    sync). Signature-verified; observes only, changes nothing — so it works in any
    provision mode (mock reflects the registry; live queries the provider APIs)."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    reference = payload["reference"]
    name, _ = _bucket_and_tags(payload)
    try:
        actual = cloud_state.describe(reference, [{"kind": _resource_kind(payload), "name": name}])
    except cloud_state.CloudStateUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    return {"reference": reference, "resources": actual, "mode": cloud_state.mode()}


@app.post("/actuate")
async def actuate(request: Request) -> dict:
    """Stop or start a request's resources (cloud-sync increment 2 — actuation).

    Signature-verified. The API has already RBAC-gated the operator, so the
    re-verification here is the signature (like /state and /destroy) — a full
    approval re-check would be wrong for a reversible operational action. Mock
    models the action and changes no real cloud; live calls the provider APIs, so
    it works in any provision mode."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    action = str(payload.get("action", "")).strip().lower()
    if action not in ("stop", "start"):
        raise HTTPException(status_code=400, detail="action must be 'stop' or 'start'.")
    reference = payload["reference"]
    name, _ = _bucket_and_tags(payload)
    try:
        result = cloud_state.actuate(reference, [{"kind": _resource_kind(payload), "name": name}], action)
    except cloud_state.CloudStateUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    return {"reference": reference, "action": action, "resources": result,
            "mode": cloud_state.mode()}


@app.post("/refresh")
async def refresh(request: Request) -> dict:
    """Refresh a lower environment from a higher one, masking sensitive source data
    (F-LCM-03). Signature-verified. Mock records the refresh (copies nothing); live
    (REFRESH_MODE=live) is an extension point — a real data-copy + masking job
    (Ansible/scripts + the source/target data plumbing)."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    if os.getenv("REFRESH_MODE", "mock").strip().lower() == "live":
        raise HTTPException(status_code=501, detail=(
            "Live environment refresh is not configured. Wire the data-copy + masking "
            "job (Ansible/scripts) in orchestrator/main.refresh to enable it."))
    tgt = (payload.get("target") or {}).get("reference")
    src = (payload.get("source") or {}).get("reference")
    masked = bool(payload.get("mask"))
    return {"refreshed": True, "masked": masked,
            "summary": (f"Mock-refreshed {tgt} from {src}"
                        + (" (sensitive data masked)" if masked else "") + " — no data copied.")}


@app.post("/restore")
async def restore(request: Request) -> dict:
    """Restore a provisioned environment to a backup, and VERIFY it (F-LCM-06).
    Signature-verified. Mock records it + reports verified (restores nothing); live
    (RESTORE_MODE=live) is an extension point — a real restore + verification job."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    if os.getenv("RESTORE_MODE", "mock").strip().lower() == "live":
        raise HTTPException(status_code=501, detail=(
            "Live restore is not configured. Wire the restore + verification job in "
            "orchestrator/main.restore to enable it."))
    tgt = (payload.get("target") or {}).get("reference")
    bk = (payload.get("backup") or {}).get("label")
    return {"restored": True, "verified": True,
            "summary": f"Mock-restored {tgt} to backup '{bk}' — verified, no data changed."}


@app.post("/destroy")
async def destroy(request: Request) -> dict:
    """Destroy the resource (rollback / cleanup). Signed, and apply mode only."""
    if provisioner.provision_mode() != "apply":
        raise HTTPException(status_code=501, detail="Destroy is only available in apply mode.")
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    name, tags = _bucket_and_tags(payload)
    try:
        result = provisioner.terraform_destroy(payload["reference"], name, tags,
                                               _resource_kind(payload), _compute_spec(payload))
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=400, detail=f"Terraform destroy failed: {exc}")
    _provisioned.pop(payload.get("idempotency_key", ""), None)
    return {"destroyed": True, "reference": payload["reference"], "summary": result["summary"]}
