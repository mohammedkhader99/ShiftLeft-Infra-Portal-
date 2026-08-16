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
from orchestrator import (
    backups,
    blueprint_registry,
    boot_reports,
    cloud_catalogue,
    cloud_state,
    configure,
    kubernetes_versions,
    network_egress,
    oke_networks,
    provisioner,
    resource_state,
)

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


@app.post("/network/egress")
async def network_egress_report(request: Request) -> dict:
    # NOT named network_egress: at module level that rebinds the imported module
    # to this function, so `network_egress.report()` below resolves to the
    # function and raises AttributeError. Every unit test passed — they exercise
    # the module directly — and the endpoint failed on the first real call.
    """What the subnet the portal builds VMs in can actually reach.

    Discovery, exactly like /blueprints: the portal cannot see the tenancy, so it
    must ask the layer that can rather than assume. Only the signature is checked
    — this reads route tables and changes nothing, and it is called while a
    requester is filling in a form, long before any approval exists to re-verify.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if provisioner.provision_mode() != "apply":
        # Mock mode builds nothing, so no network constrains it. Saying "unknown"
        # here would put a warning on every screen of the demo path.
        return {"known": False, "families": [],
                "reason": "not building real machines, so no network limits apply"}
    return network_egress.report()


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
        # OKE. Reported as set/not-set or by NAME, never by value: an admin
        # needs to know whether a cluster can be built, not to read the network
        # topology off a status page.
        "oke_bastion_cidr_set": bool(os.getenv("OCI_OKE_BASTION_CIDR")),
        # Which environment tiers have a network to build a cluster in. By
        # NAME, not by value: the OCIDs are topology, and an admin needs to
        # know which tiers are buildable, not to read the network off a
        # status page. An unmapped tier is refused, so this is the list of
        # tiers that can have a cluster at all.
        "oke_mapped_tiers": oke_networks.mapped_tiers(),
        "oke_kubernetes_version": os.getenv("OCI_OKE_KUBERNETES_VERSION", "") or "(module default)",
        "oke_cluster_type": os.getenv("OCI_OKE_CLUSTER_TYPE", "") or "BASIC_CLUSTER",
        # Set-or-not: the value is a pre-authenticated URL, which is a credential.
        "kafka_source_set": bool(os.getenv("OCI_KAFKA_SOURCE_URL")),
        "backup_mode": backups.backup_mode(),
        "restore_mode": backups.restore_mode(),
        "reduce_mode": os.getenv("REDUCE_MODE", "mock").strip().lower(),
        "refresh_mode": os.getenv("REFRESH_MODE", "mock").strip().lower(),
        # Cloud option catalogue (read-only listing for the request form). The
        # allow-lists are reported as set-or-not AND by count, because "nothing
        # is offered" and "the tenancy is unreachable" look identical otherwise.
        "catalogue_mode": cloud_catalogue.mode(),
        "catalogue_shapes_allowed": len(cloud_catalogue.shape_allowlist()),
        "catalogue_image_filter_set": bool(cloud_catalogue.image_filter()),
        # Boot self-reporting. Set-or-not, never by value: the PAR URL is a
        # credential. Without it a machine still builds, it just cannot tell the
        # portal whether its software actually came up.
        "boot_report_configured": bool(os.getenv("OCI_BOOT_REPORT_PAR_URL")),
    }


@app.post("/catalogue/oci-options")
async def catalogue_oci_options(request: Request) -> dict:
    """What the tenancy actually offers: allowed compute shapes and OS images.

    The API asks for this hourly and caches it, because listing shapes needs OCI
    credentials and the API holds none — same reasoning as /posture, same signed
    channel. **Read-only**: every call underneath is a list_*, so this endpoint
    cannot create, change or destroy anything, and needs no execution gate.

    What crosses the boundary is shape names, image OCIDs and display names. An
    image OCID identifies a public Oracle image; it is not a credential.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        return {"ok": True, **cloud_catalogue.fetch()}
    except cloud_catalogue.CloudCatalogueUnavailable as exc:
        # A reachable orchestrator that cannot read the tenancy is a different
        # thing from an unreachable one, and the admin console shows them
        # differently — so answer 200 with the reason rather than erroring.
        return {"ok": False, "reason": str(exc), "mode": cloud_catalogue.mode(),
                "shapes": [], "images": []}


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

# size -> boot volume GB, the third column of db.seed.SIZES. Kept separate
# because the shape and the disk are different Terraform inputs.
_SIZE_STORAGE = {"small": 50, "medium": 200, "large": 500, "xlarge": 1000}


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


def _resource_list(payload: dict, base_name: str) -> list[dict]:
    """The request's resources as {kind, name} — the shape the cloud-state layer
    describes and actuates.

    Built from the SAME naming rule provisioning uses. When it was not, reconcile
    looked for a name nothing had been created under and reported a healthy,
    running environment as deleted out-of-band (REQ-2026-0095). It also reported
    only the primary kind, so the rest of a stack was invisible to it.
    """
    reference, primary = payload["reference"], _resource_kind(payload)
    return [{"kind": k, "name": _resource_name(base_name, k, reference, primary)}
            for k in _resource_kinds(payload)]


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
    return _fit_name(f"{base}-{suffix}", base, suffix, reference, kind)


def _short_reference(reference: str) -> str:
    """A request reference squeezed to its identifying part: REQ-2026-0128 -> 26-0128.

    The prefix and the century carry no information — every reference has them —
    and they cost 6 of the very few characters a name has.
    """
    parts = [p for p in (reference or "").split("-") if p]
    if len(parts) >= 3 and len(parts[-2]) == 4 and parts[-2].isdigit():
        return f"{parts[-2][2:]}-{parts[-1]}"
    return (reference or "").lower()


def _fit_name(natural: str, base: str, suffix: str, reference: str, kind: str) -> str:
    """`natural` if the module accepts it, else the same name compressed to fit.

    Names are composed as <environment>-<reference>-<suffix>, and the modules cap
    them (31 for a VM, 24 for OKE). Nothing checked, so an environment name of
    seven characters produced a 32-character name and Terraform refused it at
    PLAN time — AFTER the request had been approved in Jira. On the tightest
    blueprint that left a budget of six characters for an environment name, which
    almost no real name meets.

    ONLY over-long names are changed. Every resource that already exists has a
    name its module accepted, so it takes this function's first branch and is
    untouched — renaming a live compute instance changes its hostname, which
    Terraform implements by destroying and rebuilding the machine.

    Compression is deterministic and keeps the reference, which is what makes the
    name unique; the environment name is what gets trimmed.
    """
    limit = blueprint_registry.name_limit_for_kind(kind)
    if not limit or len(natural) <= limit:
        return natural

    short_ref = _short_reference(reference)
    env = base[: -(len(reference) + 1)] if base.lower().endswith(reference.lower()) else base
    compact = f"{env}-{short_ref}-{suffix}"
    if len(compact) <= limit:
        return compact

    # Still too long: trim the environment name, never the reference — the
    # reference is what keeps the name unique across requests.
    budget = limit - len(short_ref) - len(suffix) - 2  # two hyphens
    if budget < 1:
        # A suffix so long that no environment name fits. Drop the environment
        # rather than emit something the module will refuse; the reference still
        # identifies it, and the workspace path still records which request it is.
        return f"{short_ref}-{suffix}"[:limit]
    return f"{env[:budget].rstrip('-')}-{short_ref}-{suffix}"


def _explicit_int(component: dict, field: str) -> int | None:
    """An explicit number from the component detail form, or None if unset."""
    raw = component.get(field)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _component_shape(component: dict) -> tuple[int, int]:
    """(vcpu, memory_gb) for one component: its explicit choice, else its size.

    The explicit values come from the component detail form and are already the
    ones the API priced, so preferring them here is what keeps the approved cost
    and the built machine the same thing.
    """
    vcpu, mem = _SIZES.get((component.get("size") or "").lower(), (0, 0))
    return (_explicit_int(component, "vcpu") or vcpu,
            _explicit_int(component, "memory_gb") or mem)


def _instance_sizing(payload: dict) -> dict:
    """OCI flex-shape sizing (ocpus, memory_gb) from the largest component.

    The fallback applies only when NO component resolves to a shape. It used to
    be a floor applied to every request, which meant a 'small' environment —
    priced by the catalogue at 2 vCPU / 4 GB — was actually built with 8 GB, and a
    reduction down to 'small' silently changed nothing while reporting success.
    Sizing now matches what the catalogue prices.
    """
    best: tuple[int, int] | None = None
    for c in payload.get("policy_input", {}).get("components", []):
        vcpu, mem = _component_shape(c)
        if mem and (best is None or mem > best[1]):
            best = (max(1, round(vcpu / 2)), mem)  # 1 OCPU ~ 2 vCPUs on x86 flex
    if best is None:
        best = (1, 8)  # nothing recognisable — a safe default, not a floor
    return {"ocpus": best[0], "memory_gb": best[1]}


# Boot volume when a request names no disk. OCI's own minimum is 50 GB, and the
# modules previously had no disk input at all, so every machine got the image
# default regardless of the size that was priced.
_DEFAULT_BOOT_VOLUME_GB = 50


def _boot_volume_gb(payload: dict) -> int:
    """Boot volume for the instance: the largest disk any component asks for.

    Storage was priced from the first increment and never reached Terraform, so a
    'large' request paid for 500 GB and was built on the image default. The
    offered values all clear OCI's 50 GB minimum because they come from the
    sizing anchors.
    """
    sizes = []
    for c in payload.get("policy_input", {}).get("components", []):
        explicit = _explicit_int(c, "storage_gb")
        anchored = _SIZE_STORAGE.get((c.get("size") or "").lower())
        if explicit or anchored:
            sizes.append(explicit or anchored)
    return max(sizes) if sizes else _DEFAULT_BOOT_VOLUME_GB


# size -> how many nodes a clustered resource gets. A Kubernetes node pool sized
# purely by CPU and memory would build the same number of nodes for every request,
# so the size an approver saw priced would not be the size that got built.
_NODE_COUNTS = {"small": 2, "medium": 3, "large": 5, "xlarge": 7}


def _node_count(payload: dict) -> int:
    """Node count for the LARGEST component size in the request, matching how
    _instance_sizing picks the shape."""
    sizes = [str(c.get("size", "")).strip().lower()
             for c in payload.get("policy_input", {}).get("components", [])]
    known = [_NODE_COUNTS[s] for s in sizes if s in _NODE_COUNTS]
    return max(known) if known else 1


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


def _chosen_image(payload: dict, resource_kind: str = "") -> str:
    """An OS image the REQUESTER picked on the component detail form, or "".

    Scoped to the components this resource builds, so a stack whose two machines
    were asked for different images gets each one right instead of the first one
    twice. Where two components on the SAME machine disagree, the first wins —
    there is only one machine, so something has to.
    """
    for c in _components_for(payload, resource_kind):
        if (c.get("image") or "").strip():
            return c["image"].strip()
    return ""


def _os_family_for(payload: dict, resource_kind: str = "") -> str:
    """The OS family of the image THIS machine boots.

    The API resolves it from the requester's chosen image and passes it on the
    component, because only the API has the cached image catalogue. Empty falls
    back to CONFIG_OS_FAMILY inside configure.render, which is what every request
    naming no image has always got.

    Scoped per resource: a stack whose two machines run different operating
    systems must not have one family applied to both.
    """
    for c in _components_for(payload, resource_kind):
        family = (c.get("os_family") or "").strip().lower()
        if family:
            return family
    return ""


def _image_for(payload: dict, resource_kind: str = "") -> str:
    """The OS image for this request's compute component, in precedence order:
    what the requester chose on the form, then a per-technology image from
    OCI_COMPUTE_IMAGE_MAP, then the default OCI_COMPUTE_IMAGE_OCID.

    The requester's choice comes first because they were shown it, it was priced
    and approved with the request, and silently substituting a different OS would
    make the approval describe something other than what was built.
    """
    return (_chosen_image(payload, resource_kind)
            or _mapped_image(payload)
            or os.getenv("OCI_COMPUTE_IMAGE_OCID", ""))


def _psql_shape(sizing: dict) -> str:
    """The OCI managed-PostgreSQL flex shape for a given sizing, e.g.
    PostgreSQL.VM.Standard.E4.Flex.2.32GB. The family is configurable because
    available shapes differ by region and change over time."""
    family = os.getenv("OCI_PSQL_SHAPE_FAMILY", "PostgreSQL.VM.Standard.E4.Flex").strip()
    return f"{family}.{int(sizing.get('ocpus', 1))}.{int(sizing.get('memory_gb', 8))}GB"


def _components_for(payload: dict, resource_kind: str) -> list[dict]:
    """The request's components that THIS resource is responsible for.

    A stack builds several resources, and each must be told to install only its
    own software. Rendering the whole request's components into every VM put
    Apache on the nginx machine, where httpd took port 80 first and nginx failed
    to start — a request that reported success with a third of it dead.

    The manifest already answers this: each declares what it builds. A resource
    kind with no manifest (the legacy shared module) keeps every component, which
    is what it did before and is correct for it — it branches internally.
    """
    components = payload.get("policy_input", {}).get("components", [])
    manifest = blueprint_registry.for_resource_kind(resource_kind) if resource_kind else None
    builds = set(manifest.get("builds") or []) if manifest else set()
    if not builds:
        return components
    return [c for c in components if c.get("technology_code") in builds]


def _compute_spec(payload: dict, resource_kind: str = "") -> dict:
    """The compute instance's Terraform inputs: sizing, the resolved OS image, and
    the first-boot configuration that turns a bare VM into a working service
    (GAP-ANALYSIS step 4). user_data is "" when configuration is off or no chosen
    technology has a template, which leaves the previous behaviour untouched.

    `resource_kind` scopes the first-boot configuration to the components this
    resource actually builds. Sizing stays whole-request: the largest component
    sets the shape, as before.
    """
    components = _components_for(payload, resource_kind)
    sizing = _instance_sizing(payload)

    # ONE VCN PER TIER. The cluster is built in the network belonging to the tier
    # the request names, and in no other. An unmapped tier is REFUSED here rather
    # than defaulted: a production cluster silently built into the development VCN
    # would look exactly like success, and this is the last place that can stop it.
    #
    # Only OKE consumes a network map today. The other blueprints take the shared
    # compute subnet, which is a separate contract and unchanged by this.
    # Ordinary machines are built one network per tier too. Until now every
    # resource kind except OKE took a single OCI_COMPUTE_SUBNET_OCID whatever
    # tier the request named, so a Production nginx landed in the DEVELOPMENT
    # subnet — the exact failure the OKE map exists to prevent, left open for the
    # twelve technologies people actually raise.
    #
    # Refused rather than defaulted, for the same reason and with the same force.
    if resource_kind != "oci-oke":
        tier = (payload.get("policy_input", {}).get("environment_tier") or "").strip()
        try:
            subnet = oke_networks.compute_subnet(tier)
        except oke_networks.NetworkNotMapped as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if subnet:
            # "" means no tier declares one yet, so the single configured subnet
            # still serves every tier and nothing changes.
            sizing = {**sizing, "compute_subnet": subnet}

    if resource_kind == "oci-oke":
        tier = (payload.get("policy_input", {}).get("environment_tier") or "").strip()
        try:
            network = oke_networks.for_tier(tier)
        except oke_networks.NetworkNotMapped as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The Kubernetes version OCI will actually accept. The module carried a
        # hard-coded v1.29.1, retired since it was written, and REQ-2026-0148 —
        # the first cluster anyone asked this portal for — failed at apply
        # because of it. A cloud retires versions on its own schedule and nothing
        # in this repository can be edited often enough to keep up.
        version, why = kubernetes_versions.resolve(
            os.getenv("OCI_OKE_KUBERNETES_VERSION", ""))
        if not version:
            raise HTTPException(status_code=400, detail=why)
        sizing = {
            **sizing,
            "oke_kubernetes_version": version,
            "vcn_id": network["vcn"],
            "api_subnet_id": network["api"],
            "node_subnet_id": network["node"],
            "pod_subnet_id": network["pod"],
            "lb_subnet_id": network["lb"],
            "bastion_subnet_id": network["bastion"],
        }
    return {
        **sizing,
        # The disk the request was priced for. Previously never sent, so every
        # machine got the image default however much storage was paid for.
        "boot_volume_gb": _boot_volume_gb(payload),
        "image_ocid": _image_for(payload, resource_kind),
        # Carried separately so a blueprint with its own image lookup can honour
        # a deliberate choice while ignoring the shared default. The requester's
        # own pick counts as deliberate — more so than an admin's map.
        "image_ocid_explicit": (_chosen_image(payload, resource_kind)
                                or _mapped_image(payload)),
        # Resolved once and used twice: baked into user_data for blueprints that
        # delegate to configure.py, and passed as a Terraform variable for those
        # that render their own first-boot script (apache-httpd). One source, so
        # the two paths cannot disagree about which OS a machine is.
        "os_family": _os_family_for(payload, resource_kind) or configure.os_family(),
        # Same URL the generic path bakes into user_data, exposed as a
        # variable for blueprints that render their own cloud-init.
        "boot_report_url": configure.boot_report_url(
            payload.get("reference", ""), resource_kind),
        "user_data": configure.render(
            components,
            _os_family_for(payload, resource_kind),
            # Where this machine PUTs its own evidence. Empty when no PAR is
            # configured, in which case nothing about the boot changes.
            configure.boot_report_url(payload.get("reference", ""), resource_kind),
        ),
        # The ports the installed service listens on, from the same profiles that
        # produced user_data — so the network rules and the OS firewall agree.
        "service_ports": configure.ports_for(
            components, _os_family_for(payload, resource_kind)),
        # How many nodes a clustered resource gets for this request's size.
        "node_count": _node_count(payload),
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

    if mode in ("plan", "apply"):
        # Every resource in the stack is planned. A failure on any one fails the
        # whole handoff: a stack that is half-plannable must not look ready.
        plans = []
        for kind in kinds:
            try:
                plans.append((kind, provisioner.terraform_plan(
                    reference, _resource_name(name, kind, reference, rkind), tags, kind,
                    _compute_spec(payload, kind))))
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

    # Apply each resource in turn. If a later one fails, the earlier ones are
    # already REAL — so they are reported, not hidden: an error that loses track
    # of created infrastructure leaves it running with nobody aware of it.
    created: list[dict] = []
    for kind in kinds:
        rname = _resource_name(name, kind, reference, rkind)
        try:
            result = provisioner.terraform_apply(reference, rname, tags, kind,
                                                 _compute_spec(payload, kind))
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


def _report_expected(kind: str) -> tuple[bool, str]:
    """Whether a machine of this kind should have reported, and if not, why not.

    Stated as a reason rather than a bare False so that "nothing to check here"
    can never be confused with "checked and found nothing" — the second is a
    failure and the first is not.
    """
    if provisioner.provision_mode() != "apply":
        return False, "mock mode built no real machine"
    manifest = blueprint_registry.for_resource_kind(kind) or {}
    if not manifest.get("os_families"):
        return False, "not a machine — nothing boots, so nothing can report"
    if manifest.get("boot_report") == "none":
        return False, "blueprint is exempt from boot reporting"
    if not boot_reports.bucket():
        return False, "no OCI_BOOT_REPORT_PAR_URL is configured, so no machine was asked"
    return True, ""


@app.post("/verify")
async def verify_boot(request: Request) -> dict:
    """What the machines of a request say about themselves, in their own words.

    READ-ONLY, and it decides nothing. The orchestrator holds the cloud
    credentials, so it is the only layer that can read the bucket; what a broken
    machine MEANS for a request is the portal's call, and the portal makes it.
    That split is the same one that keeps approval in Jira: whoever can act must
    not also be the one who judges the outcome.

    Callable in every PROVISION_MODE. In mock mode there is no machine to ask, so
    every resource comes back not-applicable and the portal proceeds as before —
    a verification step that failed closed in mock mode would stop the demo path
    the whole project is built to run on.
    """
    body = await request.body()
    payload = _authorise(body, request.headers.get("X-Signature", ""))
    reference = payload["reference"]

    name, _tags = _bucket_and_tags(payload)
    primary = _resource_kind(payload)

    resources: list[dict] = []
    for kind in _resource_kinds(payload):
        expected, why = _report_expected(kind)
        if not expected:
            # No machine to ask — so ask the RESOURCE. A cluster, a bucket and a
            # managed database file no boot report and never will, and until now
            # that read as "nothing to prove". It is not: it means the proof has
            # to come from the thing itself reaching a working state.
            if provisioner.provision_mode() == "apply":
                try:
                    health = resource_state.check(
                        kind, _resource_name(name, kind, reference, primary))
                except resource_state.StateUnavailable as exc:
                    resources.append({"kind": kind, "expected": True,
                                      "state": "unreadable", "note": str(exc)})
                    continue
                if health["state"] != "unknown":
                    resources.append({
                        "kind": kind, "expected": True, "state": health["state"],
                        "problems": ([health["detail"]]
                                     if health["state"] == "broken" else []),
                        "note": health["detail"]})
                    continue
                why = health["detail"]
            resources.append({"kind": kind, "expected": False,
                              "state": "not-applicable", "note": why})
            continue
        try:
            found = boot_reports.reports_for(reference, kind)
        except boot_reports.BootReportUnavailable as exc:
            # The bucket itself is unreadable. NOT the same as a machine that has
            # not reported yet, and not something waiting longer will fix.
            resources.append({"kind": kind, "expected": True,
                              "state": "unreadable", "note": str(exc)})
            continue
        if not found:
            resources.append({"kind": kind, "expected": True, "state": "waiting",
                              "note": "no machine has reported yet"})
            continue
        problems: list[str] = []
        for name, text in sorted(found.items()):
            result = boot_reports.verdict(text)
            problems += [f"{name}: {p}" for p in result["problems"]]
        resources.append({
            "kind": kind, "expected": True,
            "state": "ok" if not problems else "broken",
            "problems": problems,
            "machines": sorted(found),
            # The machine's own words, so the portal can show a human WHY rather
            # than a verdict they have to take on trust.
            "reports": found,
        })

    checked = [r for r in resources if r["expected"]]
    return {
        "reference": reference,
        "resources": resources,
        "checked": len(checked),
        "waiting": [r["kind"] for r in checked if r["state"] == "waiting"],
        "broken": [r["kind"] for r in checked if r["state"] == "broken"],
        "unreadable": [r["kind"] for r in checked if r["state"] == "unreadable"],
        # Nothing left to wait for — every machine that owes a report has filed one.
        "settled": not any(r["state"] == "waiting" for r in checked),
        # True with nothing to check: a bucket request has no machine to disbelieve.
        "all_ok": all(r["state"] == "ok" for r in checked),
    }


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
    # Only what was actually built: a stack provisioned before multi-resource has
    # one workspace, and checking a kind that was never applied would report a
    # whole environment as broken because of a resource that does not exist.
    built = provisioner.existing_workspaces(reference)
    checked = [k for k in kinds if k in built] or ([kinds[0]] if built else [])

    changes, summaries = [], []
    for kind in checked:
        try:
            result = provisioner.terraform_drift(
                reference, _resource_name(name, kind, reference, rkind), tags, kind,
                _compute_spec(payload, kind))
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
        actual = cloud_state.describe(reference, _resource_list(payload, name))
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
        result = cloud_state.actuate(reference, _resource_list(payload, name), action)
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

    # A PARTIAL teardown removes exactly what it names; a FULL one removes
    # everything on disk.
    #
    # Sweeping up untracked workspaces is right when a whole environment is being
    # retired: a stack whose kinds changed since it was built would otherwise
    # leave the old resource running, untracked and still billing. It is very
    # wrong when the request names a subset — a decommission of one component in
    # a two-component stack destroyed both machines, because "anything on disk"
    # included the one nobody asked to remove.
    #
    # `partial` is set by the portal, which is the only layer that knows whether
    # the requester picked some components or all of them. Absent (an older
    # portal), the sweep behaviour is unchanged.
    built = provisioner.existing_workspaces(reference)
    partial = bool(payload.get("partial_destroy"))
    if "" in built:  # legacy flat workspace holds exactly one resource
        targets = [_resource_kind(payload)]
    elif partial:
        targets = [k for k in kinds if k in built]
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
                reference, _resource_name(name, kind, reference, rkind), tags, kind,
                _compute_spec(payload, kind))
        except provisioner.ProvisionError as exc:
            raise HTTPException(
                status_code=400,
                detail=(f"Terraform destroy failed for {kind}: {exc}"
                        + (f" — already destroyed: {'; '.join(summaries)}" if summaries else "")))
        summaries.append(f"{kind}: {result['summary']}")
    _provisioned.pop(payload.get("idempotency_key", ""), None)
    return {"destroyed": True, "reference": reference, "summary": "; ".join(summaries)}
