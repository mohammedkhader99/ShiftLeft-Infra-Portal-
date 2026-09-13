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
import math
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Request

from common.httpclient import client as http_client

from common import proof_rules
from common.signing import verify
from orchestrator import cluster_discovery
from orchestrator import golden_image
from orchestrator import marketplace
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
    postgres_shapes,
    provisioner,
    resource_state,
)

API_URL = os.getenv("API_URL", "http://localhost:8081")
# 127.0.0.1 rather than "localhost" — see common/httpclient. Compose sets this
# explicitly for the container, so the default only governs a host-run process.
OPA_URL = os.getenv("OPA_URL", "http://127.0.0.1:8181")
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


@app.post("/boot-report")
async def boot_report(request: Request) -> dict:
    """The machines' self-reports for one request, verbatim, for a person to read.

    The portal already reads these to decide whether a request is provisioned,
    and then throws them away. They are the most direct answer to "what did I
    actually get" that exists — the OS, the package versions, whether the service
    is running, whether the port answers — so a requester should be able to see
    the same thing the portal judged.

    SIGNATURE ONLY, like /blueprints and /network/egress: this reads a bucket and
    changes nothing. WHO may see it is the portal's decision, made against the
    signed-in user; this layer holds the credential, not the policy.

    The credential never travels. OCI_BOOT_REPORT_PAR_URL is a pre-authenticated
    URL — anyone holding it could read every machine's report forever, without
    signing in — so the text is fetched here and returned as text. The browser is
    never given the URL.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    reference = str(payload.get("reference") or "")
    if not reference:
        raise HTTPException(status_code=400, detail="No reference given.")

    reports: list[dict] = []
    for kind in (payload.get("resource_kinds") or []):
        expected, why = _report_expected(kind)
        if not expected:
            # Says WHY there is nothing rather than returning an empty panel: "a
            # bucket files no boot report" and "the machine has not answered yet"
            # look identical otherwise, and mean completely different things.
            reports.append({"kind": kind, "available": False, "files": {},
                            "note": why})
            continue
        try:
            found = boot_reports.reports_for(reference, kind)
        except boot_reports.BootReportUnavailable as exc:
            reports.append({"kind": kind, "available": False, "files": {},
                            "note": f"the report store could not be read: {exc}"})
            continue
        if not found:
            reports.append({"kind": kind, "available": False, "files": {},
                            "note": "no machine has reported for this component"})
            continue
        reports.append({"kind": kind, "available": True, "files": found,
                        "note": ""})
    return {"reference": reference, "reports": reports}


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
        # Whether the portal may READ the Marketplace. It never accepts a
        # listing's agreements or chooses one — that is a person's act,
        # and the PII agreement shares their details with the publisher.
        "marketplace_enabled": marketplace.enabled(),
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
        # Certification proof builds (C2). Shown because this is the one gate
        # that lets the portal provision without a Jira approval — an admin
        # should be able to see, at a glance, whether that is switched on and
        # what bounds it.
        # Agent-written blueprints (C5a). The COUNT is the fact an admin needs:
        # "has a model written anything into this orchestrator, and how much".
        "generated_blueprints": len([b for b in blueprint_registry.discover()
                                     if b.get("origin") == "generated"]),
        "generated_blueprints_dir": str(blueprint_registry.GENERATED_DIR),
        # Agent-written technology profiles (C6): a package, a systemd unit
        # and a port that extend a blueprint which already builds machines
        # correctly, rather than a second module for a resource kind that
        # already has one.
        "generated_profiles_dir": str(configure.GENERATED_PROFILE_DIR),
        "generated_profiles": len(configure._generated_profiles()),
        "proof_enabled": proof_rules.enabled(),
        # The reference format THIS service will accept. Published so the API can
        # notice a version skew before a proof rather than during one: on
        # 2026-08-21 the API was rebuilt with a new reference format and the
        # orchestrator was not, so every proof was refused with "not a proof
        # reference" — correct behaviour, and an alarming thing to read.
        "proof_reference_pattern": proof_rules._REFERENCE.pattern,
        "proof_sandbox_tier": os.getenv("CERTIFICATION_SANDBOX_TIER", "") or "(unset — refuses)",
        "proof_cost_cap_monthly": proof_rules.cost_cap(),
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


@app.post("/catalogue/clusters")
async def catalogue_clusters(request: Request) -> dict:
    """Kubernetes clusters in the tenancy, with their node pool capacity.

    Read-only and signature-only, like /catalogue/oci-options: every call
    underneath is a list_*, so this endpoint cannot create, change or destroy
    anything and needs no execution gate. What crosses the boundary is cluster
    OCIDs, names, versions and sizes — a cluster OCID identifies a resource, it
    is not a credential.

    A tenancy that cannot be READ answers 200 with the reason and an empty list,
    the same shape /catalogue/oci-options uses, because a reachable orchestrator
    that cannot see the tenancy is a different thing from an unreachable one and
    the caller has to be able to tell them apart. "You have no clusters" is a
    statement about the requester; "we could not look" is a statement about the
    portal.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        return {"ok": True, **cluster_discovery.fetch()}
    except cluster_discovery.ClusterDiscoveryUnavailable as exc:
        return {"ok": False, "reason": str(exc), "mode": cluster_discovery.mode(),
                "clusters": [], "count": 0}


@app.post("/catalogue/versions")
async def catalogue_versions(request: Request) -> dict:
    """Which VERSIONS of each service the cloud currently offers.

    The portal sells `postgres16` and `oci-oke`, but the cloud decides what it
    will still build. On 2026-08-17 that gap cost four failed requests: the OKE
    module pinned Kubernetes v1.29.1, OCI had retired it, and nothing in the
    portal knew until a real request failed at apply.

    Read-only and signature-only, like /blueprints and /catalogue/oci-options:
    every call underneath is a list_*. It reports what the cloud says; deciding
    what that MEANS for the catalogue is the portal's job.

    A family that cannot be asked is reported as `null` rather than as an empty
    list — "OCI offers no PostgreSQL versions" and "we could not reach OCI" must
    never look the same, because one of them is a reason to withdraw a product.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    families: dict[str, list[str] | None] = {}
    try:
        families["postgres"] = postgres_shapes.versions() or None
    except Exception:  # noqa: BLE001 — one unreachable family must not hide the rest
        families["postgres"] = None
    try:
        families["kubernetes"] = kubernetes_versions.supported() or None
    except Exception:  # noqa: BLE001
        families["kubernetes"] = None
    return {"families": families, "mode": provisioner.provision_mode()}


def _current_monthly(policy_input: dict) -> float | None:
    body = {
        "deployment_target": policy_input.get("deployment_target"),
        "components": policy_input.get("components", []),
    }
    resp = http_client().post(f"{API_URL}/api/cost", json=body, timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("totals", {}).get("monthly")


def _authorise_proof(payload: dict, policy_input: dict,
                     read_only: bool = False) -> dict:
    """Authority for a certification proof build (ARCHITECTURE.md §4).

    The runner may provision without a Jira approval, bounded on every side. The
    orchestrator checks those bounds against ITS OWN configuration rather than
    believing the payload — a caller that could assert its own sandbox tier or
    its own cost cap would have no bounds at all.

    Refusals are 403 and name the limit, so a misconfiguration reads as a
    misconfiguration instead of a mysterious silence.
    """
    if not proof_rules.enabled():
        raise HTTPException(
            status_code=403,
            detail="Proof builds are not enabled on the orchestrator "
                   "(CERTIFICATION_PROOF_ENABLED). Both services must allow them.")

    reference = str(payload.get("reference") or "")
    if not proof_rules.is_proof_reference(reference):
        raise HTTPException(
            status_code=403,
            detail=f"{reference!r} is not a proof reference. A proof may only "
                   f"act under a reference this runner minted.")

    try:
        sandbox = proof_rules.configured_sandbox_tier()
    except proof_rules.ProofRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    tier = str(policy_input.get("environment_tier") or "")
    if tier != sandbox:
        raise HTTPException(
            status_code=403,
            detail=f"A proof may build only in the sandbox tier ({sandbox}); this "
                   f"one asked for {tier or 'no tier'}.")

    # A READ stops here. The remaining two checks — OPA and the cost ceiling —
    # govern what may be BUILT, and /verify builds nothing; it reports on what
    # already exists. Pricing a read is not merely pointless, it is harmful: the
    # pricing call can fail 502, and that would fail a proof whose infrastructure
    # was perfectly healthy, for a reason having nothing to do with the thing
    # under test. Everything above still applies — proofs enabled, a reference
    # this runner minted, and the sandbox tier.
    if read_only:
        return payload

    # Re-check OPA, exactly as for a user request. A proof is not exempt from
    # policy just because it is the portal testing itself.
    try:
        result = http_client().post(
            f"{OPA_URL}/v1/data/infra/authz", json={"input": policy_input}, timeout=5.0
        ).json().get("result", {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-check policy: {exc}")
    if not result.get("allow"):
        raise HTTPException(status_code=403, detail="Policy re-check failed at execution.")

    # THE COST CEILING, applied before anything is built. Priced here rather than
    # taken from the payload for the same reason as the tier.
    try:
        monthly = _current_monthly(policy_input)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not price the proof: {exc}")
    verdict = proof_rules.check_cost(monthly)
    if not verdict.allowed:
        raise HTTPException(status_code=409, detail=verdict.reason)

    return payload


def _authorise(body: bytes, signature: str, read_only: bool = False) -> dict:
    """Verify authenticity + authority + policy + cost. Returns the payload."""
    if not verify(WEBHOOK_SECRET, body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = json.loads(body)
    if payload.get("contract_version") != SUPPORTED_CONTRACT:
        raise HTTPException(status_code=400, detail="Unsupported contract version.")

    policy_input = payload.get("policy_input", {})

    # A CERTIFICATION PROOF carries its own authority (ARCHITECTURE.md §4,
    # "Attest"). Every limit is re-verified HERE, from this service's own
    # environment — the signature proves who sent it, never what they may do,
    # which is the same stance taken on a Jira approval two lines below.
    if payload.get("proof"):
        return _authorise_proof(payload, policy_input, read_only)

    jira_key = payload["jira_key"]

    # Authority: re-verify the approval in Jira (mock = API).
    try:
        approval = http_client().get(f"{API_URL}/api/approvals/{jira_key}",
                                     timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not re-verify approval: {exc}")
    if approval.get("status") != "approved":
        raise HTTPException(status_code=403, detail="Approval not confirmed in Jira.")

    # Re-check OPA policy.
    try:
        result = http_client().post(
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


def _placement_units(payload: dict) -> list[dict] | None:
    """One unit of work per machine the placement says to build (P.13, F-ORC-11).

    THE UNIT OF WORK USED TO BE A RESOURCE KIND, and that was right until a
    placement could put two machines on one kind. "Separated" builds three
    machines that are all `oci-instance`; one workspace per kind collapses them
    into a single VM, and the portal would then build something other than what
    was approved while reporting success — the failure this whole phase exists to
    end, arriving one layer further down.

    Each unit carries its own shape, taken from the placement rather than from
    `_instance_sizing`. That is the other half of the change: sizing from the
    largest component is correct when a machine holds one component and wrong
    when it holds three, because three co-resident components need the SUM.

    A MANAGED HOST BUILDS NO MACHINE AND STILL BUILDS SOMETHING. It used to
    produce no unit on the grounds that the cloud runs it — true of the machine
    and false of the resource: `oci-oke` is `oke-cluster.tf` plus
    `oke-nodepool.tf`, and `oci-postgres` is a DB system Terraform creates. What
    a managed host has no need of is a SHAPE, which is a fact about sizing.

    Dropping them was invisible while a placement was all-managed — the caller
    then saw None and fell back to the kind list, which planned everything — and
    while it was all-VMs. REQ-2026-0315 was neither: two managed hosts and one
    VM, so the units were non-empty, the fallback never ran, and the cluster and
    the database were left out of the plan entirely. The approver approved
    "3 to add" for minio and oracle-free, and apply then went looking for a plan
    for the cluster that nothing had made.

    Two tests sat either side of that: one for an all-managed placement, one for
    a managed host alongside a VM asserting only the VM became a unit. Both
    passed. Neither stood on the boundary, which is where every real request is.

    A host the portal could not size produces no unit either, and this is a
    REFUSAL rather than a default: building a machine at a guessed shape, for a
    request priced without it, is exactly the silent substitution the portal is
    supposed to make impossible. `_refuse_unsized_hosts` turns it into an error
    the requester sees.

    Returns None when the payload carries no placement, and every caller then
    behaves exactly as it did before this existed.
    """
    placement = payload.get("placement")
    if not isinstance(placement, dict):
        return None
    hosts = placement.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        return None

    # A CONTAINER HOST IS NOT A MACHINE, and treating it as one built the wrong
    # thing entirely. A workload placed on a cluster resolves to its own
    # blueprint's resource kind — `oci-service-vm` for Node.js — so this filter
    # turned "provision a Kubernetes cluster and run Node.js on it" into one
    # virtual machine with Node.js on it and NO CLUSTER, and reported success.
    #
    # Refused in `_refuse_unsized_hosts` rather than skipped, because skipping
    # would build the rest of the request and leave the workload silently absent.
    # Nothing in this system deploys into a cluster (private API endpoint, no
    # route from here), so a container host arriving at this layer means an
    # option was offered that should not have been.
    builds = [h for h in hosts
              if h.get("host_mode") != "container"
              and (h.get("host_mode") == "managed" or h.get("resolved"))]

    # How many hosts share each kind. Only a kind with more than one needs its
    # workspaces distinguished, so a request with one host per kind keeps the
    # directory names — and therefore the Terraform state — it already has.
    counts: dict[str, int] = {}
    for host in builds:
        kind = host.get("resource_kind") or ""
        counts[kind] = counts.get(kind, 0) + 1

    units = []
    for host in builds:
        kind = host.get("resource_kind") or ""
        if not kind:
            # Nothing certified says what this host builds. Left out here and
            # refused by the caller: guessing a module is how a request for
            # Python 3.12 once received an empty bucket.
            continue
        host_id = str(host.get("id") or "")
        units.append({
            "kind": kind,
            "host_id": host_id,
            "host_mode": host.get("host_mode") or "vm",
            "workspace": provisioner.workspace_name(
                kind, host_id, shared=counts.get(kind, 0) > 1),
            "components": list(host.get("components") or ()),
            "shape": {"vcpu": host.get("vcpu"),
                      "memory_gb": host.get("memory_gb"),
                      "storage_gb": host.get("storage_gb")},
        })
    return units or None


def _refuse_unsized_hosts(payload: dict) -> None:
    """Stop a build the portal could not size or could not map to a module.

    Both are the same failure wearing different clothes: the plan would be for
    something other than what was approved. A machine with no recorded
    requirement was excluded from the cost the approver signed, and a host with
    no certified blueprint has no module that builds it — so proceeding means
    provisioning at a shape or from a recipe nobody agreed to.
    """
    placement = payload.get("placement")
    if not isinstance(placement, dict):
        return
    unsized, unmapped, on_a_cluster = [], [], []
    for host in placement.get("hosts") or []:
        if host.get("host_mode") == "container":
            on_a_cluster.append(str(host.get("id") or "?"))
        elif host.get("host_mode") == "managed":
            # No shape to resolve — the cloud chooses the machine, and nothing
            # was sized or priced as one, so `resolved` says nothing here.
            #
            # IT STILL NEEDS A MODULE. This used to `continue` past both checks,
            # and `_placement_units` then dropped the host again for being
            # managed, so a request naming a service no certified blueprint
            # builds was planned without it and reported ready to provision —
            # the same silence, arriving twice.
            if not host.get("resource_kind"):
                unmapped.append(str(host.get("id") or "?"))
        elif not host.get("resolved"):
            unsized.append(str(host.get("id") or "?"))
        elif not host.get("resource_kind"):
            unmapped.append(str(host.get("id") or "?"))

    # THE ONE THAT WOULD HAVE BUILT THE WRONG THING. Checked first because its
    # failure is not a missing number or a missing module — it is a layout that
    # resolves to a machine when a cluster was asked for.
    if on_a_cluster:
        raise HTTPException(
            status_code=400,
            # WHAT IT WOULD BUILD, THEN WHAT TO DO, THEN THE DETAIL — in that
            # order, because the end is what a trim eats.
            #
            # This said the same things in the opposite order and ran to 539
            # characters against a 500-character column. REQ-2026-0312 was held
            # up showing "...would provision machines instead of th" — the
            # sentence amputated mid-word, and what it lost was every word
            # telling the requester what to do instead.
            #
            # The switch's own name is deliberately absent: a requester reading
            # this cannot set an environment variable, and naming one tells them
            # their request failed on a setting rather than on a missing network
            # route. That belongs in PLAN.md and in the code, both of which have
            # it.
            #
            # "would provision machines instead of the cluster" is not padding.
            # Rewriting this to lead with the remedy dropped it, and
            # test_the_refusal_says_a_machine_would_have_been_built_instead
            # caught that: a reader told only "unsupported" does not learn that
            # the alternative was silently WRONG rather than absent, which is
            # what REQ-2026-0144 was — an empty bucket, reported as success.
            detail=(f"This cannot be built: nothing in this system deploys "
                    f"into a cluster — the Kubernetes API endpoint is private "
                    f"and the orchestrator has no route to it — so building it "
                    f"would provision machines instead of the cluster that was "
                    f"asked for. Choose a layout that builds machines, or ask "
                    f"for the cluster on its own and deploy into it yourself. "
                    f"(Placement version {placement.get('version')} puts "
                    f"{len(on_a_cluster)} workload group(s) on a cluster: "
                    f"{', '.join(on_a_cluster)}.)"))
    if unsized:
        raise HTTPException(
            status_code=400,
            detail=(f"Placement version {placement.get('version')} has "
                    f"{len(unsized)} machine(s) with no resolved size "
                    f"({', '.join(unsized)}). They were excluded from the "
                    f"approved cost, so building them would provision something "
                    f"nobody approved."))
    if unmapped:
        raise HTTPException(
            status_code=400,
            detail=(f"Placement version {placement.get('version')} has "
                    f"{len(unmapped)} host(s) no certified blueprint builds "
                    f"({', '.join(unmapped)}). Nothing says what to build, so "
                    f"nothing is built."))


def _placement_spec(payload: dict, unit: dict) -> dict:
    """One machine's Terraform inputs, sized by the placement.

    Starts from the ordinary spec so everything else a module needs — the image,
    the subnet, the first-boot configuration — is derived exactly as before, then
    overrides the shape with the one the placement resolved and priced.

    OCPUs rather than vCPUs, because that is what OCI's flex shapes take and what
    the module's variable is named.

    ROUNDED UP, and this is not a detail. `_instance_sizing` uses round(), which
    is safe there only by accident: every sizing anchor in the catalogue has an
    even vCPU count, so round() and ceil() agree. Placement shapes do not — P.6
    adds headroom and deliberately rounds UP, because "a machine that needs 4.8
    vCPU gets 5, never 4 — rounding a requirement down is how headroom becomes a
    shortfall". A 4 vCPU requirement plus 20% is 5, and round(5/2) is 2 in
    Python, which is 4 vCPUs: the headroom would be spent undoing itself and the
    machine built smaller than the figure the approver was shown.
    """
    spec = _compute_spec(payload, unit["kind"])
    shape = unit.get("shape") or {}
    vcpu, memory = shape.get("vcpu"), shape.get("memory_gb")
    if vcpu:
        spec = {**spec, "ocpus": max(1, math.ceil(int(vcpu) / 2))}
    if memory:
        spec = {**spec, "memory_gb": int(memory)}
    if shape.get("storage_gb"):
        spec = {**spec, "boot_volume_gb": int(shape["storage_gb"])}
    return spec


def _work_units(payload: dict) -> list[dict]:
    """Every workspace this request builds, and the inputs for each one.

    THE ONE ANSWER PLAN AND APPLY BOTH USE, and the reason this function exists
    rather than each endpoint working it out. They did work it out separately,
    from the same signed payload, and got different answers: plan followed the
    placement and wrote one workspace per host; apply ignored the placement and
    read one workspace per resource KIND. Wherever those two lists differ —
    every mixed placement, and every layout putting two machines on one kind —
    apply went looking in a directory plan had never written to.

    REQ-2026-0315 is the first request with a placement ever to reach apply, so
    it is also the first to find out. It failed safely and created nothing. The
    other direction is the one to be frightened of: where a workspace of that
    name is left over from an EARLIER layout, apply finds a plan, applies it,
    and reports success for infrastructure nobody approved. `plan_id` closes
    that; this closes the reason it would have been reached.

    `_handoff_payload` says, one layer up: "The full resource list is derived
    here rather than at each call site, so plan, apply, drift, state and destroy
    cannot disagree about what a request consists of — the kind of split that
    lost REQ-2026-0094's second resource." It was right, and the split moved
    down a layer rather than going away.
    """
    _refuse_unsized_hosts(payload)
    units = _placement_units(payload)
    if units is not None:
        return [{**unit, "spec": _placement_spec(payload, unit)} for unit in units]

    # No placement: one workspace per kind, named after it — which is what every
    # request raised before placement existed has on disk, unchanged.
    return [{"kind": kind, "workspace": kind, "host_id": "", "host_mode": "vm",
             "components": [], "shape": {}, "spec": _compute_spec(payload, kind)}
            for kind in _resource_kinds(payload)]


def _plan_id(payload: dict) -> str:
    """The name of the layout a plan was approved for, carried down to the saved
    plan so apply can refuse one made for a different layout.

    The placement version, because that is what changes when a requester changes
    their mind. Empty means no placement at all — every request raised before
    placement existed — and an empty one matches only an empty one.
    """
    placement = payload.get("placement")
    if not isinstance(placement, dict):
        return ""
    return f"v{placement.get('version')}"


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


def _workspace_resource_name(base: str, workspace: str, reference: str,
                             primary: str) -> str:
    """This machine's name, derived from the workspace it lives in.

    KEYED ON THE WORKSPACE BECAUSE THAT IS WHAT BOTH SIDES HAVE. Planning knows
    the placement; destroy and drift find workspaces by listing the directory and
    have nothing else. Deriving the name from the resource KIND on the way down
    and from the placement on the way up would give a machine one name when it was
    built and a different one when it was torn down — so both go through here.

    The existing rule when a kind builds one machine, so nothing already running
    is renamed: Terraform reads a rename as a change to live infrastructure, and
    for a compute instance it can force replacement, destroying and rebuilding a
    working machine.

    When a placement puts SEVERAL machines on one kind they cannot all take that
    name, so the host id in the workspace distinguishes them. Only the machines
    that need it are affected; the single-machine case is byte-identical.
    """
    kind = provisioner.kind_of_workspace(workspace)
    if workspace == kind:
        return _resource_name(base, kind, reference, primary)
    suffix = workspace[len(kind) + len(provisioner.WORKSPACE_SEPARATOR):] or kind
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
            # CEIL, NOT ROUND, and the difference only shows on an odd count.
            # Python's round() is banker's rounding: round(5/2) is 2, which is
            # 4 vCPUs — a requester who asks for 5 gets 4, silently, and the
            # machine is smaller than the one they were priced for.
            #
            # This was safe for as long as it went unnoticed because every sizing
            # anchor in the catalogue is even (2, 4, 8, 16), so round and ceil
            # agree on all of them, and no request in this database has ever
            # carried an odd explicit vCPU. It was a trap waiting for the first
            # person to type 5 into the component detail form, which the form
            # allows.
            #
            # The placement path already rounds up, for the sharper reason that
            # P.6 adds headroom and rounds THAT up: rounding back down here would
            # spend the headroom undoing itself. Both paths now agree.
            best = (max(1, math.ceil(vcpu / 2)), mem)  # 1 OCPU ~ 2 vCPUs on x86 flex
    if best is None:
        best = (1, 8)  # nothing recognisable — a safe default, not a floor
    return {"ocpus": best[0], "memory_gb": best[1]}


# Boot volume when a request names no disk. OCI's own minimum is 50 GB, and the
# modules previously had no disk input at all, so every machine got the image
# default regardless of the size that was priced.
_DEFAULT_BOOT_VOLUME_GB = 50


def _wants_a_data_volume(payload: dict) -> bool:
    """Whether any technology on this request keeps its data on its own volume.

    Asked of the RECIPE, not of the request: a container profile declaring a
    `data_dir` is the only thing that knows a separate volume is needed, and it
    is the same profile the first-boot script will mount.
    """
    try:
        from orchestrator import configure
    except Exception:  # noqa: BLE001 - a missing recipe table means no data volume
        return False
    family = (payload.get("os_family") or "rhel")
    for component in payload.get("policy_input", {}).get("components", []):
        profile = configure.profile_for(component.get("technology_code") or "", family)
        # `or {}` AROUND THE VALUE, not a default on the lookup. `profile_for`
        # returns the key present and set to None for every technology without a
        # container, and a default only applies when a key is MISSING — so this
        # raised AttributeError on nginx, which would have turned a provision
        # into a 500 rather than a request for no data volume.
        if ((profile or {}).get("container") or {}).get("data_dir"):
            return True
    return False


def _data_volume_gb(payload: dict) -> int:
    """Size of the separate data volume, or 0 when nothing asked for one.

    THE REQUESTER'S `storage_gb` SIZES THIS, NOT THE BOOT DISK. When someone
    asks for RabbitMQ with 100 GB they mean 100 GB for their messages, not a
    larger operating system disk — so for a technology whose data lives on its
    own volume the requested figure follows the data, and the boot volume stays
    at the standard size. For everything else nothing changes: `storage_gb`
    continues to size the boot disk exactly as it has since that was fixed.
    """
    if not _wants_a_data_volume(payload):
        return 0
    return _boot_volume_gb(payload)


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
            or _golden_pick(payload, resource_kind)[1]
            or _mapped_image(payload)
            or os.getenv("OCI_COMPUTE_IMAGE_OCID", ""))


def _golden_pick(payload: dict, resource_kind: str = "") -> tuple[str, str]:
    """A previously proven image for one of THIS machine's technologies, or "".

    Deliberately AFTER the requester's own choice. Someone who picked an image
    was shown it, it was priced, and it was approved with the request; swapping
    in a different one would make the approval describe something other than
    what was built — the same reasoning `_image_for` already gives.

    Before the map and the default, though, because those are just "some Oracle
    Linux", and this is that same OS with the software already on it, proven.

    ONE DECISION, RETURNED WHOLE. The image the machine boots and the technology
    it therefore need not install are the same fact. Working them out in two
    places is how they come to disagree — and disagreeing here means either
    installing software that is already present, or skipping an install for
    software that is not.
    """
    golden = payload.get("golden_images") or {}
    if not isinstance(golden, dict):
        return "", ""
    for c in _components_for(payload, resource_kind):
        code = str(c.get("technology_code") or "")
        ocid = golden.get(code)
        if ocid:
            return code, str(ocid)
    return "", ""


def _psql_shape(sizing: dict) -> str:
    """The OCI managed-PostgreSQL shape for a given sizing, ASKED not assembled.

    This used to concatenate a name:

        f"{family}.{sizing['ocpus']}.{sizing['memory_gb']}GB"

    For REQ-2026-0155 that produced `PostgreSQL.VM.Standard.E4.Flex.1.4GB` and
    the apply was rejected with the list of shapes OCI actually publishes. Two
    faults in one line: the E4 family is not offered in me-dubai-1 at all, and
    the portal's "small" (1 OCPU / 4 GB) is below the service floor — no managed
    PostgreSQL is smaller than 2 OCPU / 32 GB. String building cannot fail, so it
    always returned a plausible name and always failed at apply.

    The request's numbers are now a floor, resolved against the published list.
    """
    from orchestrator import postgres_shapes

    shape, _why = postgres_shapes.shape_for_spec(
        int(sizing.get("ocpus", 1) or 1), int(sizing.get("memory_gb", 8) or 8))
    return shape


def _psql_version(components: list[dict]) -> str:
    """The PostgreSQL major version the REQUEST asked for, e.g. postgres16 -> 16.

    The catalogue name is what the requester chose and what Jira approved, so it
    decides the version. It used to come from OCI_PSQL_VERSION, which defaulted
    to 14 — so a postgres16 request built PostgreSQL 14, silently.
    """
    from orchestrator import postgres_shapes

    # The key is technology_code. This tried "technology" and "code" — both
    # guessed, neither present — so REQ-2026-0158 still built 14 with a correct
    # shape beside it. The unit test guessed the same key and passed, proving the
    # assumption self-consistent rather than true. All three spellings are read
    # now, but technology_code is the real one.
    for component in components or []:
        code = str(component.get("technology_code")
                   or component.get("technology")
                   or component.get("code") or "")
        if "postgres" in code.lower():
            version = postgres_shapes.version_from_build(code)
            if version:
                return version
    return os.getenv("OCI_PSQL_VERSION", "")


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
        # The boot disk keeps the standard size when the requested figure has
        # gone to a data volume instead; sizing both from one number would bill
        # for the storage twice.
        "boot_volume_gb": (_DEFAULT_BOOT_VOLUME_GB if _wants_a_data_volume(payload)
                           else _boot_volume_gb(payload)),
        "data_volume_gb": _data_volume_gb(payload),
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
            # WHAT IS ALREADY ON THE IMAGE. Read from the same pick that chose
            # the image above, so the boot script cannot skip an install for
            # software the machine did not actually boot with.
            #
            # Guarded on the image ACTUALLY BEING USED: a requester's own image
            # choice outranks a golden one, and in that case the software is
            # emphatically not preinstalled. Taking the pick without checking
            # which image won would produce a machine that installs nothing and
            # reports everything missing.
            preinstalled=(
                {_golden_pick(payload, resource_kind)[0]}
                if _golden_pick(payload, resource_kind)[1]
                and _image_for(payload, resource_kind)
                    == _golden_pick(payload, resource_kind)[1]
                else set()),
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
        # The version the catalogue name promises, carried alongside the shape so
        # the provisioner does not have to guess which key holds it — guessing
        # that key is why REQ-2026-0155 still built 14.
        "db_version": _psql_version(components),
    }


@app.post("/provision")
async def provision(request: Request) -> dict:
    """Approve handoff: plan only (mock returns a mock result). Creates nothing."""
    body = await request.body()
    payload = _authorise(body, request.headers.get("X-Signature", ""))
    reference, jira_key = payload["reference"], payload.get("jira_key", "")
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
        #
        # WHAT COUNTS AS "EVERY RESOURCE" IS THE PLACEMENT'S ANSWER when there is
        # one — one workspace per host, at the shape the placement resolved — and
        # the kind list otherwise. Two requests with identical components and
        # different placements therefore produce different plans, which is the
        # whole point of carrying the layout down here. `_work_units` is that
        # answer, and apply asks the same function the same question.
        plan_id = _plan_id(payload)
        plans = []
        for unit in _work_units(payload):
            label = unit["workspace"]
            try:
                plans.append((label, provisioner.terraform_plan(
                    reference,
                    _workspace_resource_name(name, label, reference, rkind),
                    tags, unit["kind"], unit["spec"],
                    workspace=label, plan_id=plan_id)))
            except provisioner.ProvisionError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Terraform plan failed for {label}: {exc}")
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
            # WHICH PLACEMENT PRODUCED THIS PLAN. Without it a plan is an
            # orphan: the requester can change their mind, each change is a new
            # version, and "why does this build three machines?" has no answer
            # that can be checked against the record.
            # `.get`, NOT a subscript. A bare payload["placement"] would be a
            # key every handoff has to carry — including a certification proof,
            # which has no placement — and a missing one would then be a 500
            # rather than a clean refusal. test_the_handoff_carries_every_key_the
            # _orchestrator_requires enforces exactly that, having been written
            # after a valid proof returned KeyError: 'idempotency_key' in
            # production. The guard above makes it unreachable today; the guard
            # is not the contract.
            **({"placement_version": (payload.get("placement") or {}).get("version"),
                "placement_option": (payload.get("placement") or {}).get("option_key"),
                "workspaces": [k for k, _p in plans]}
               if isinstance(payload.get("placement"), dict) else {}),
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
    reference, jira_key = payload["reference"], payload.get("jira_key", "")
    key = payload["idempotency_key"]

    if key in _provisioned:  # already created — do not create twice (F-ORC-01)
        return {**_provisioned[key], "idempotent": True}

    name, tags = _bucket_and_tags(payload)
    rkind = _resource_kind(payload)

    # Apply each resource in turn. If a later one fails, the earlier ones are
    # already REAL — so they are reported, not hidden: an error that loses track
    # of created infrastructure leaves it running with nobody aware of it.
    #
    # THE SAME UNITS PLAN MADE, from the same function, so that the plan approved
    # and the plan applied are the same plan. Iterating resource kinds here — one
    # workspace per kind, while plan wrote one per host — is what left
    # REQ-2026-0315 asking for a plan of a cluster that nothing had planned.
    plan_id = _plan_id(payload)
    created: list[dict] = []
    for unit in _work_units(payload):
        workspace, kind = unit["workspace"], unit["kind"]
        rname = _workspace_resource_name(name, workspace, reference, rkind)
        try:
            result = provisioner.terraform_apply(reference, rname, tags, kind,
                                                 unit["spec"], workspace=workspace,
                                                 plan_id=plan_id)
        except provisioner.ProvisionError as exc:
            detail = f"Terraform apply failed for {workspace}: {exc}"
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
    payload = _authorise(body, request.headers.get("X-Signature", ""), read_only=True)
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
            #
            # BUT NOT FOR A MACHINE, and the distinction is the whole point.
            # `_report_expected` says no for several different reasons, and only
            # some of them mean "nothing could ever report":
            #
            #   not a machine, nothing boots   a bucket or a cluster -- substitute
            #   blueprint is exempt            OKE's managed nodes   -- substitute
            #   no PAR configured              the machine COULD have reported
            #                                  and was not asked     -- do NOT
            #
            # A compute instance reaching RUNNING says the VM exists. It says
            # nothing about whether the software installed, which is the entire
            # reason boot reports exist: "Terraform exiting zero says the VM
            # exists, not that anything was installed on it." Substituting a
            # power state for a boot report would certify a machine that came up
            # empty -- the failure this gate was built to stop, arriving through
            # the gate itself.
            #
            # resource_state still answers for machines; it is asked directly,
            # for the live "is it running now" question, which is a different
            # question from "did it become what was promised".
            if (provisioner.provision_mode() == "apply"
                    and kind not in resource_state.COMPUTE_KINDS):
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
        # `kind` here is a DIRECTORY NAME read off disk, which a placement may
        # have suffixed with a host id. The module, the timeout and the manifest
        # vars all key off the real kind, so recover it.
        real = provisioner.kind_of_workspace(kind)
        try:
            result = provisioner.terraform_drift(
                reference, _workspace_resource_name(name, kind, reference, rkind),
                tags, real, _compute_spec(payload, real), workspace=kind)
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




@app.post("/image-state")
async def image_state(request: Request) -> dict:
    """What OCI says about images we captured. Read-only; changes nothing.

    The API holds the golden-image rows and the orchestrator holds the cloud
    credentials, so neither can answer this alone. Signature-verified like every
    other cloud-touching endpoint, and like /state it needs no execution gate:
    it observes.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    ocids = payload.get("image_ocids") or []
    if not isinstance(ocids, list):
        raise HTTPException(status_code=400, detail="image_ocids must be a list.")
    return {"states": golden_image.state_of([str(o) for o in ocids[:200]]),
            "mode": golden_image.mode()}



@app.post("/delete-image")
async def delete_image(request: Request) -> dict:
    """Delete one custom image, named by the caller. Signature-verified.

    The API holds the golden-image rows and decides WHICH image has stopped
    being usable; the orchestrator holds the credentials and does it. Neither
    can do this alone, which is the separation working as intended rather than
    an inconvenience.

    Exactly one OCID per call. A bulk endpoint would make a bug here delete a
    fleet instead of an image.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    ocid = str(payload.get("image_ocid") or "")
    ok, detail = golden_image.delete(ocid)
    return {"image_ocid": ocid, "deleted": ok, "detail": detail,
            "mode": golden_image.mode()}


@app.post("/capture-image")
async def capture_image(request: Request) -> dict:
    """Keep the machine that passed, as a reusable image.

    Called between `verify` and `destroy` on a proof build, so the instance it
    photographs has already been proven healthy. Signature-verified like every
    other endpoint that touches the cloud.

    IT NEVER FAILS THE CALLER. A capture is an optimisation on a path that
    already works: the proof's verdict is decided before this is called, and a
    refusal here must not be able to change it. So every outcome is a 200 with
    `captured: false` and a reason, and there is no error status to raise.
    """
    body = await request.body()
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    payload = json.loads(body)
    reference = payload["reference"]

    # ONLY EVER A PROOF. A capture reaches into a running machine and creates a
    # persistent billable artefact from it; the only machine we have grounds to
    # do that to is one this platform built to prove a recipe. A production
    # environment's instance is not ours to photograph.
    if not payload.get("proof") or not proof_rules.is_proof_reference(reference):
        return {"reference": reference, "captured": False,
                "detail": "Only a proof build's own instance may be captured."}

    components = (payload.get("policy_input", {}).get("components") or [{}])
    code = (payload.get("technology_code")
            or components[0].get("technology_code", ""))

    # A MACHINE HAS NOTHING TO MAKE PERMANENT.
    #
    # compute-vm and rhel9 install nothing by design, so an image of one is a
    # copy of the platform image OCI already gives us free — ~50 GB of storage
    # to skip an install that does not exist. REQ-2026-0210 would have created
    # exactly that.
    if any((c.get("delivers") or "").strip().lower() == "machine"
           for c in components):
        return {"reference": reference, "captured": False,
                "detail": ("This is a bare machine: there is no install to skip, "
                           "so an image of it would duplicate the platform image "
                           "at full storage cost.")}

    # THE NAME PROVISIONING ACTUALLY USED, from the one function that knows it.
    #
    # This asked `_bucket_and_tags` for a BUCKET name and looked for an instance
    # under it. For a proof, whose environment_name IS its reference, that name
    # is the reference doubled — and it is missing the `-instance` suffix and
    # the truncation `_resource_name` applies. So it searched for something that
    # had never existed and reported "nothing to capture" about a live machine.
    #
    # `_resource_list` exists because this happened once already: REQ-2026-0095
    # reported a healthy running environment as deleted out-of-band, for exactly
    # this reason. Its docstring says so. Deriving a name a second way is how
    # both were caused.
    # THE KIND UNDER TEST, not "whatever looks like compute". `_is_compute`
    # matches on substrings and does not recognise `oci-service-vm`, which is
    # the blueprint behind every software-on-a-VM — the exact case golden images
    # exist for. Depending on it here would have made this fix work for nothing.
    # (That gap is wider than this endpoint; it is reported separately.)
    name, _ = _bucket_and_tags(payload)
    primary = _resource_kind(payload)
    target = next((r["name"] for r in _resource_list(payload, name)
                   if r["kind"] == primary), "")
    if not target:
        return {"reference": reference, "captured": False,
                "detail": "This request builds nothing to capture."}

    result = golden_image.capture(reference, technology_code=code,
                                  instance_name=target)
    return {
        "reference": reference,
        "captured": result.ok,
        "image_ocid": result.image_ocid,
        "source_instance_ocid": result.source_instance_ocid,
        "size_gb": result.size_gb,
        "mode": golden_image.mode(),
        "detail": result.detail,
    }


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
    reference = payload["reference"]

    # A PROOF MAY ONLY DESTROY ITS OWN WORK (ARCHITECTURE.md §4, "only what it
    # created"). Checked here as well as in the runner: this endpoint takes
    # authority from the signature alone, so a runner that was wrong — or a
    # payload that was tampered with before signing — would otherwise be able to
    # delete somebody's environment. Scoped by MARKER, never by tier: the sandbox
    # is a real tier full of real work.
    if payload.get("proof") and not proof_rules.may_destroy(reference):
        raise HTTPException(
            status_code=403,
            detail=f"A proof teardown may only target a proof reference; "
                   f"{reference!r} is not one.")

    name, tags = _bucket_and_tags(payload)
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
        return {"destroyed": True, "reference": reference, "kinds": [],
                "summary": "nothing to destroy — no Terraform state for this request"}

    summaries = []
    for kind in targets:
        # A DIRECTORY NAME, not necessarily a resource kind — see the drift loop.
        # Getting this wrong is worse here than anywhere else: a destroy that
        # reaches `_timeout_for` with an unrecognised kind gets the ten-minute
        # default, and a cluster that takes twenty minutes to delete would time
        # out mid-teardown with the resources still live and still billing.
        real = provisioner.kind_of_workspace(kind)
        try:
            result = provisioner.terraform_destroy(
                reference, _workspace_resource_name(name, kind, reference, rkind),
                tags, real, _compute_spec(payload, real), workspace=kind)
        except provisioner.ProvisionError as exc:
            raise HTTPException(
                status_code=400,
                detail=(f"Terraform destroy failed for {kind}: {exc}"
                        + (f" — already destroyed: {'; '.join(summaries)}" if summaries else "")))
        summaries.append(f"{kind}: {result['summary']}")
    _provisioned.pop(payload.get("idempotency_key", ""), None)
    # WHAT WAS DESTROYED, as data rather than as prose in `summary`.
    #
    # This layer is the only one that knows. `targets` above is deliberately not
    # the kind list the portal sent: a full teardown also sweeps up workspaces
    # the portal did not name, precisely so a stack whose kinds changed since it
    # was built does not leave the old resource running and billing.
    #
    # REQ-2026-0226 is what happens when only this end knows. The portal asked
    # to destroy `oci-bucket`; the workspace on disk was `oci-service-vm`; the
    # sweep above destroyed the right machine — and the portal then marked its
    # ledger from its OWN list, matched nothing, and left a destroyed VM
    # recorded as active and billing at 790.59 a month.
    return {"destroyed": True, "reference": reference, "kinds": list(targets),
            "summary": "; ".join(summaries)}
