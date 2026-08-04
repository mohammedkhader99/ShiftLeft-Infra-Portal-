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
import logging
import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request

from common.signing import verify
from orchestrator import backups, blueprint_registry, cloud_state, configure, provisioner

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

# Use uvicorn's logger so the line actually surfaces in the container logs.
_trace_log = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def _trace(request: Request, call_next):
    """Distributed tracing (F-OPS-04): honour the API's X-Trace-Id — log it with
    the operation and echo it back, so the hop is followable end-to-end."""
    tid = request.headers.get("X-Trace-Id", "")
    response = await call_next(request)
    if tid:
        response.headers["X-Trace-Id"] = tid
        _trace_log.info("trace=%s %s %s -> %s", tid, request.method, request.url.path,
                        response.status_code)
    return response


_provisioned: dict[str, dict] = {}  # idempotency ledger for real creations


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/blueprints")
async def blueprints(request: Request) -> dict:
    """Which build recipes THIS orchestrator actually ships.

    Discovery, not configuration: the portal must never advertise a blueprint the
    layer that executes doesn't have. The portal records which of these are
    *certified* for use; this endpoint answers only 'what is present'.

    Discovered by scanning orchestrator/blueprints/*.yaml, so adding a recipe is
    adding a manifest file — not editing this code. Each manifest also declares
    what it needs to run, letting the portal show 'certified but not configured'
    instead of failing at apply time.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    return {"available": blueprint_registry.discover()}


@app.post("/posture")
async def posture(request: Request) -> dict:
    """Report which execution gates are open in THIS process, for the admin
    console's posture view.

    The API cannot answer this itself: these switches are read by the
    orchestrator's environment, not the API's, so the API would be guessing — and
    a posture view that guesses is worse than none. Signature-verified because it
    enumerates configuration; it returns only booleans and mode names, never a
    credential, an OCID or a secret.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    return {
        "provision_mode": provisioner.provision_mode(),
        "cloud_state_mode": os.getenv("CLOUD_STATE_MODE", "mock").strip().lower(),
        "actuate_enabled": os.getenv("OCI_ACTUATE_ENABLED", "false").strip().lower()
                           in ("1", "true", "yes", "on"),
        "psql_enabled": provisioner.psql_enabled(),
        "psql_configured": bool(os.getenv("OCI_PSQL_SUBNET_OCID")
                                and os.getenv("OCI_PSQL_ADMIN_SECRET_OCID")),
        "dns_mode": os.getenv("DNS_MODE", "mock").strip().lower(),
        "dns_enabled": provisioner.dns_enabled(),
        "dns_zone_set": bool(os.getenv("OCI_DNS_ZONE")),
        "config_enabled": configure.enabled(),
        "backup_mode": backups.backup_mode(),
        "restore_mode": backups.restore_mode(),
        "reduce_mode": os.getenv("REDUCE_MODE", "mock").strip().lower(),
        "refresh_mode": os.getenv("REFRESH_MODE", "mock").strip().lower(),
    }


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
    """The PRIMARY kind. Operations that act on one resource (stop/start, DNS)
    still use this; provisioning uses _resource_kinds."""
    return payload.get("resource_kind", "oci-bucket")


def _resource_kinds(payload: dict) -> list[str]:
    """Every resource this request builds, in a stable order.

    A stack is more than one thing. An older portal sends only `resource_kind`,
    so fall back to it rather than refusing the handoff.
    """
    kinds = payload.get("resource_kinds")
    if isinstance(kinds, list):
        clean = [str(k) for k in kinds if k]
        if clean:
            return clean
    return [_resource_kind(payload)]


def _merge_scans(scans: list[dict]) -> dict:
    """Combine the IaC scan results of a stack into one verdict. A high-severity
    finding on ANY resource has to reach the gate, so findings and counts add up
    rather than the last one winning."""
    if not scans:
        return {}
    findings: list[dict] = []
    counts = {"high": 0, "medium": 0, "low": 0}
    for s in scans:
        findings.extend(s.get("findings") or [])
        for sev, n in (s.get("counts") or {}).items():
            counts[sev] = counts.get(sev, 0) + n
    return {"findings": findings, "counts": counts,
            "high": counts["high"], "ok": counts["high"] == 0}


def _resource_name(base: str, kind: str, reference: str, primary: str) -> str:
    """The resource's own name.

    A stack needs its parts told apart, but a resource that ALREADY EXISTS must
    keep the name it was built with. Terraform reads a rename as a change to live
    infrastructure — and for a compute instance the hostname change can force
    replacement, destroying and rebuilding a running machine.

    Which is why this cannot key off "how many kinds does the request derive
    today": REQ-2026-0094 was built when it derived one kind and now derives two,
    so that rule proposed renaming its running VM. The legacy flat workspace is
    the durable record that a resource predates suffixed names, and it holds
    exactly one resource — the primary kind.
    """
    if kind == primary and "" in provisioner.existing_workspaces(reference):
        return base
    suffix = kind.split("-", 1)[1] if "-" in kind else kind
    return f"{base}-{suffix}"


def _instance_sizing(payload: dict) -> dict:
    """OCI flex-shape sizing (ocpus, memory_gb) from the largest component size.

    The fallback applies only when NO component resolves to a known size. It used
    to be a floor applied to every request, which meant a 'small' environment —
    priced by the catalogue at 2 vCPU / 4 GB — was actually built with 8 GB, and a
    reduction down to 'small' silently changed nothing while reporting success.
    Sizing now matches what the catalogue prices.
    """
    best: tuple[int, int] | None = None
    for c in payload.get("policy_input", {}).get("components", []):
        vcpu, mem = _SIZES.get((c.get("size") or "").lower(), (0, 0))
        if mem and (best is None or mem > best[1]):
            best = (max(1, round(vcpu / 2)), mem)  # 1 OCPU ~ 2 vCPUs on x86 flex
    if best is None:
        best = (1, 8)  # nothing recognisable — a safe default, not a floor
    return {"ocpus": best[0], "memory_gb": best[1]}


def _mapped_image(payload: dict) -> str:
    """An image chosen DELIBERATELY for one of this request's technologies via
    OCI_COMPUTE_IMAGE_MAP (JSON: technology_code -> image OCID), or "".

    Kept separate from the shared default because a blueprint that resolves its
    own image (Apache looks up the latest Oracle Linux for its shape) must be
    able to tell "an admin picked this image for this technology" from "nobody
    said, so here is the default for the shared compute module".
    """
    try:
        mapping = json.loads(os.getenv("OCI_COMPUTE_IMAGE_MAP", "") or "{}")
    except (ValueError, TypeError):
        mapping = {}
    if isinstance(mapping, dict):
        for c in payload.get("policy_input", {}).get("components", []):
            code = c.get("technology_code")
            if code and mapping.get(code):
                return mapping[code]
    return ""


def _image_for(payload: dict) -> str:
    """The OS image for this request's compute component: a per-technology image
    from OCI_COMPUTE_IMAGE_MAP if one matches a component, else the default
    OCI_COMPUTE_IMAGE_OCID."""
    return _mapped_image(payload) or os.getenv("OCI_COMPUTE_IMAGE_OCID", "")


def _psql_shape(sizing: dict) -> str:
    """The OCI managed-PostgreSQL flex shape for a given sizing, e.g.
    PostgreSQL.VM.Standard.E4.Flex.2.32GB. The family is configurable because
    available shapes differ by region and change over time."""
    family = os.getenv("OCI_PSQL_SHAPE_FAMILY", "PostgreSQL.VM.Standard.E4.Flex").strip()
    return f"{family}.{int(sizing.get('ocpus', 1))}.{int(sizing.get('memory_gb', 8))}GB"


def _compute_spec(payload: dict) -> dict:
    """The compute instance's Terraform inputs: sizing, the resolved OS image, and
    the first-boot configuration that turns a bare VM into a working service
    (GAP-ANALYSIS step 4). user_data is "" when configuration is off or no chosen
    technology has a template, which leaves the previous behaviour untouched."""
    components = payload.get("policy_input", {}).get("components", [])
    sizing = _instance_sizing(payload)
    return {
        **sizing,
        "image_ocid": _image_for(payload),
        # Carried separately so a blueprint with its own image lookup can honour
        # a deliberate per-technology image while ignoring the shared default.
        "image_ocid_explicit": _mapped_image(payload),
        "user_data": configure.render(components),
        # The ports the installed service listens on, from the same profiles that
        # produced user_data — so the network rules and the OS firewall agree.
        "service_ports": configure.ports_for(components),
        # The managed-PostgreSQL shape for the same sizing, so a database scales
        # with the request like a VM does. Unused for non-database resources.
        "db_shape": _psql_shape(sizing),
    }


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
    kinds = _resource_kinds(payload)
    sizing = _compute_spec(payload)

    if mode in ("plan", "apply"):
        # Every resource in the stack is planned. A failure on any one fails the
        # whole handoff: a stack that is half-plannable must not look ready.
        plans = []
        for kind in kinds:
            try:
                plans.append((kind, provisioner.terraform_plan(
                    reference, _resource_name(name, kind, reference, rkind), tags, kind, sizing)))
            except provisioner.ProvisionError as exc:
                raise HTTPException(status_code=400,
                                    detail=f"Terraform plan failed for {kind}: {exc}")
        scan = _merge_scans([p["scan"] for _k, p in plans if p.get("scan")])
        # IaC scan gate (F-SEC-03/04): block on HIGH findings only when enforced;
        # otherwise the findings are reported in the response and audited by the API.
        if scan.get("high", 0) > 0 and _iac_scan_enforce():
            raise HTTPException(
                status_code=409,
                detail=(f"IaC scan blocked: {scan['high']} high-severity finding(s) in the "
                        f"terraform plan."))
        summary = "; ".join(f"{k}: {p['summary']}" for k, p in plans)
        return {
            "provisioned": False, "planned": True, "reference": reference, "jira_key": jira_key,
            "verified": verified, "plan_summary": summary,
            "plan_output": "\n\n".join(p["output"] for _k, p in plans),
            "scan": scan, "resource_kinds": kinds,
            "message": f"Terraform plan for {reference}: {summary} — nothing created.",
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
    kinds = _resource_kinds(payload)
    sizing = _compute_spec(payload)

    # Apply each resource in turn. If a later one fails, the earlier ones are
    # already REAL — so they are reported, not hidden: an error that loses track
    # of created infrastructure leaves it running with nobody aware of it.
    created: list[dict] = []
    for kind in kinds:
        rname = _resource_name(name, kind, reference, rkind)
        try:
            result = provisioner.terraform_apply(reference, rname, tags, kind, sizing)
        except provisioner.ProvisionError as exc:
            detail = f"Terraform apply failed for {kind}: {exc}"
            if created:
                detail += (f" — {len(created)} resource(s) WERE created and are live: "
                           + ", ".join(f"{c['kind']} {c['name']}" for c in created))
            raise HTTPException(status_code=400, detail=detail)
        created.append({"kind": kind, "name": rname, "region": os.getenv("OCI_REGION"),
                        "outputs": result["outputs"], "summary": result["summary"]})

    summary = "; ".join(f"{c['kind']}: {c['summary']}" for c in created)
    provisioned = {
        "provisioned": True, "reference": reference, "jira_key": jira_key,
        "verified": {"approval": True, "policy": True, "cost": True},
        # `resource` stays for older callers; `resources` is the whole stack.
        "resource": created[0] if created else None,
        "resources": created,
        "summary": summary,
        "message": f"Provisioned {reference}: {summary}",
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
    rkind = _resource_kind(payload)
    kinds = _resource_kinds(payload)
    sizing = _compute_spec(payload)
    # Only what was actually built: a stack provisioned before multi-resource has
    # one workspace, and checking a kind that was never applied would report a
    # whole environment as broken because of a resource that does not exist.
    built = provisioner.existing_workspaces(reference)
    checked = [k for k in kinds if k in built] or ([kinds[0]] if built else [])

    changes, summaries = [], []
    for kind in checked:
        try:
            result = provisioner.terraform_drift(
                reference, _resource_name(name, kind, reference, rkind), tags, kind, sizing)
        except provisioner.ProvisionError as exc:
            raise HTTPException(status_code=400, detail=f"Drift check failed for {kind}: {exc}")
        changes.extend(result.get("changes") or [])
        summaries.append(f"{kind}: {result.get('summary', '')}")
    return {"reference": reference, "drift": bool(changes), "changes": changes,
            "count": len(changes),
            "summary": "; ".join(summaries) or "nothing provisioned"}


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


@app.post("/dns")
async def dns(request: Request) -> dict:
    """Create a DNS record naming a provisioned environment (GAP-ANALYSIS step 5).

    Signature-verified. The record is planned into the TARGET environment's own
    Terraform workspace, so it shares that environment's lifecycle — tearing the
    environment down removes its name too, rather than leaving a record pointing
    at nothing. Mock records the intent; live creates a real record.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    target = payload.get("target") or {}
    record = payload.get("record") or {}
    tgt = target.get("reference")
    fqdn_part = f"{record.get('name', '')}"

    if os.getenv("DNS_MODE", "mock").strip().lower() != "live":
        return {"created": True, "mock": True,
                "summary": f"Mock DNS record '{fqdn_part}' for {tgt} — nothing created."}

    try:
        provisioner._require_dns()
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=501, detail=str(exc))

    synthetic = {"policy_input": target.get("policy_input") or {}, "reference": tgt}
    name, tags = _bucket_and_tags(synthetic)
    rkind = target.get("resource_kind") or "oci-bucket"
    sizing = {
        **_compute_spec(synthetic),
        "dns_name": record.get("name", ""),
        "dns_type": record.get("type", "A"),
        "dns_value": record.get("value", ""),
    }
    try:
        plan = provisioner.terraform_plan(tgt, name, tags, rkind, sizing)
        applied = provisioner.terraform_apply(tgt, name, tags, rkind, sizing)
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=400, detail=f"DNS record creation failed: {exc}")
    outputs = applied.get("outputs", {})
    return {
        "created": True,
        "mock": False,
        "domain": outputs.get("dns_domain", ""),
        "points_to": record.get("value") or outputs.get("instance_private_ip", ""),
        "plan_summary": plan.get("summary"),
        "summary": (f"Created DNS record {outputs.get('dns_domain') or fqdn_part} for {tgt}. "
                    f"{applied.get('summary', 'apply complete')}"),
    }


@app.post("/backup")
async def backup(request: Request) -> dict:
    """Take a backup of a provisioned environment's managed database (F-LCM-06).
    Signature-verified. Mock records it; live (BACKUP_MODE=live) calls the real OCI
    API and returns the backup OCID the portal stores for a later restore."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    target = payload.get("target") or {}
    label = (payload.get("backup") or {}).get("label") or "backup"
    try:
        return backups.create_backup(target, label)
    except backups.BackupError as exc:
        raise HTTPException(status_code=501, detail=str(exc))


@app.post("/restore")
async def restore(request: Request) -> dict:
    """Restore a provisioned environment from a backup (F-LCM-06).

    Signature-verified. Mock records it; live (RESTORE_MODE=live) performs a real
    OCI restore — which creates a NEW database system, because OCI managed
    PostgreSQL has no in-place rollback. The response says so explicitly so the
    portal never implies the original database was rewound.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    target = payload.get("target") or {}
    bk = payload.get("backup") or {}
    if backups.restore_mode() != "live":
        tgt = target.get("reference")
        return {"restored": True, "verified": True,
                "summary": f"Mock-restored {tgt} to backup '{bk.get('label')}' — verified, no data changed."}
    try:
        return backups.restore_to_new_system(
            target,
            backup_id=bk.get("backup_id") or "",
            new_display_name=f"{target.get('reference', 'env')}-restored",
        )
    except backups.BackupError as exc:
        raise HTTPException(status_code=501, detail=str(exc))


@app.post("/reduce")
async def reduce(request: Request) -> dict:
    """Scale chosen components of a provisioned environment down (F-CAT).
    Signature-verified. Mock records it (resizes nothing); live (REDUCE_MODE=live)
    is an extension point — a real resize job (Terraform/scripts)."""
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    target = payload.get("target") or {}
    tgt = target.get("reference")
    reductions = payload.get("reductions") or []
    parts = ", ".join(f"{r.get('technology')} {r.get('from')}→{r.get('to')}" for r in reductions)

    if os.getenv("REDUCE_MODE", "mock").strip().lower() != "live":
        return {"reduced": True,
                "summary": f"Mock-reduced {tgt}: {parts or 'no changes'} — nothing resized."}

    # Live resize goes through TERRAFORM, not a direct SDK call: the request's
    # workspace is the source of truth, so re-planning with the smaller sizing
    # keeps state consistent. A direct API resize would show up as drift and could
    # be reverted by the next apply.
    if not reductions:
        return {"reduced": False, "summary": "No reductions supplied — nothing to do."}
    rkind = target.get("resource_kind") or "oci-bucket"
    if rkind not in ("oci-instance", "oci-postgres"):
        raise HTTPException(status_code=501, detail=(
            f"'{rkind}' has no resizable capacity — only compute instances and managed "
            "databases can be scaled down."))

    # Apply the reductions to the target's components, then size from the result.
    policy_input = dict(target.get("policy_input") or {})
    new_size = {str(r.get("technology")): str(r.get("to")) for r in reductions if r.get("to")}
    components = [
        {**c, "size": new_size.get(c.get("technology_code"), c.get("size"))}
        for c in (policy_input.get("components") or [])
    ]
    policy_input["components"] = components
    synthetic = {"policy_input": policy_input, "reference": tgt}
    name, tags = _bucket_and_tags(synthetic)
    sizing = _compute_spec(synthetic)

    try:
        plan = provisioner.terraform_plan(tgt, name, tags, rkind, sizing)
        applied = provisioner.terraform_apply(tgt, name, tags, rkind, sizing)
    except provisioner.ProvisionError as exc:
        raise HTTPException(status_code=400, detail=f"Resize failed: {exc}")
    return {
        "reduced": True,
        "plan_summary": plan.get("summary"),
        "outputs": applied.get("outputs", {}),
        # Resizing real infrastructure is not free of impact: a flex-shape change
        # restarts the instance, and a database shape change restarts the service.
        "disruptive": True,
        "summary": (f"Resized {tgt}: {parts}. {applied.get('summary', 'apply complete')}. "
                    "The resource restarted as part of the change."),
    }


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
    reference = payload["reference"]
    rkind = _resource_kind(payload)
    kinds = _resource_kinds(payload)
    sizing = _compute_spec(payload)

    # Destroy is driven by the workspaces that EXIST, not by what the request
    # asked for. A stack whose kinds changed since it was built would otherwise
    # leave the old resource running with nothing tracking it, and still bill for
    # it. Anything on disk gets torn down.
    built = provisioner.existing_workspaces(reference)
    if "" in built:  # legacy flat workspace holds exactly one resource
        targets = [_resource_kind(payload)]
    else:
        targets = ([k for k in kinds if k in built]
                   + [k for k in built if k not in kinds])

    if not targets:
        _provisioned.pop(payload.get("idempotency_key", ""), None)
        return {"destroyed": True, "reference": reference,
                "summary": "nothing to destroy — no Terraform state for this request"}

    summaries = []
    for kind in targets:
        try:
            result = provisioner.terraform_destroy(
                reference, _resource_name(name, kind, reference, rkind), tags, kind, sizing)
        except provisioner.ProvisionError as exc:
            raise HTTPException(
                status_code=400,
                detail=(f"Terraform destroy failed for {kind}: {exc}"
                        + (f" — already destroyed: {'; '.join(summaries)}" if summaries else "")))
        summaries.append(f"{kind}: {result['summary']}")
    _provisioned.pop(payload.get("idempotency_key", ""), None)
    return {"destroyed": True, "reference": reference, "summary": "; ".join(summaries)}
