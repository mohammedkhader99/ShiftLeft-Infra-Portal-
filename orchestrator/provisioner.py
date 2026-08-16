"""Terraform provisioner (2.6a plan, 2.6b apply/destroy, 2.6c hardening).

PROVISION_MODE: mock (no cloud) | plan (terraform plan) | apply (enables the
separate apply/destroy). Credentials come from the environment / a mounted key
file, never held in code or git (P3).

Hardening (2.6c):
- Each request gets its OWN working directory under a persistent volume
  (TF_STATE_DIR, default /tfstate), so concurrent requests never share state and
  state survives orchestrator restarts.
- Plan is saved (`-out=tfplan`); apply runs that exact saved plan, so what is
  created is exactly what was reviewed.
- A shared provider plugin cache (baked at build) keeps init fast.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from orchestrator import blueprint_registry, drift, scanner

MODULE_DIR = Path(__file__).resolve().parent / "terraform"
STATE_ROOT = Path(os.getenv("TF_STATE_DIR", "/tfstate"))
PLAN_FILE = "tfplan"


class ProvisionError(RuntimeError):
    """Terraform failed or is misconfigured."""


def provision_mode() -> str:
    return os.getenv("PROVISION_MODE", "mock").strip().lower()


def _require_oci() -> None:
    missing = [k for k in ("OCI_TENANCY_OCID", "OCI_COMPARTMENT_OCID", "OCI_REGION")
               if not os.getenv(k)]
    if missing:
        raise ProvisionError(f"OCI not configured: missing {', '.join(missing)}")


# Compute provisioning genuinely needs only an existing subnet and an OS image.
# Access is image-dependent: an SSH public key (OCI_COMPUTE_SSH_AUTHORIZED_KEY)
# and/or cloud-init user-data (OCI_COMPUTE_USER_DATA, e.g. to set a password on a
# custom image) are BOTH optional. All are the customer's to supply via .env / a
# vault — never held in code or git.
_REQUIRED_COMPUTE_VARS = ("OCI_COMPUTE_SUBNET_OCID",)


def _require_compute() -> None:
    missing = [k for k in _REQUIRED_COMPUTE_VARS if not os.getenv(k)]
    # An OS image can come from the default var or the per-technology map.
    if not os.getenv("OCI_COMPUTE_IMAGE_OCID") and not os.getenv("OCI_COMPUTE_IMAGE_MAP"):
        missing.append("OCI_COMPUTE_IMAGE_OCID (a default image) or OCI_COMPUTE_IMAGE_MAP (per-technology images)")
    if missing:
        raise ProvisionError(
            "Compute (VM) provisioning is not configured: set "
            + ", ".join(missing)
            + " — an existing subnet OCID and an OS image."
        )


# Managed PostgreSQL (GAP-ANALYSIS.md step 2) is DOUBLE-GATED, because this
# deployment can run autonomously (AUTO_PROVISION + PROVISION_MODE=apply) and a
# managed DB system is expensive: it needs both the infrastructure inputs AND an
# explicit OCI_PSQL_ENABLED opt-in. Absent either, a real apply refuses with a
# clear message rather than silently creating a billable database.
_REQUIRED_PSQL_VARS = ("OCI_PSQL_SUBNET_OCID", "OCI_PSQL_ADMIN_SECRET_OCID")


def psql_enabled() -> bool:
    return os.getenv("OCI_PSQL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _require_psql() -> None:
    if not psql_enabled():
        raise ProvisionError(
            "Managed PostgreSQL provisioning is disabled. Set OCI_PSQL_ENABLED=true "
            "to allow it — this creates a real, billable OCI Database with "
            "PostgreSQL system."
        )
    missing = [k for k in _REQUIRED_PSQL_VARS if not os.getenv(k)]
    if missing:
        raise ProvisionError(
            "Managed PostgreSQL provisioning is not configured: set "
            + ", ".join(missing)
            + " — an existing private DB subnet OCID and the OCI Vault SECRET OCID "
            "holding the admin password (the password itself is never given to the "
            "portal)."
        )


def dns_enabled() -> bool:
    return os.getenv("OCI_DNS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _require_dns() -> None:
    """Creating a real DNS record needs the opt-in and a zone to create it in.
    Without both, refuse rather than plan a record into an empty zone name."""
    if not dns_enabled():
        raise ProvisionError(
            "DNS record creation is disabled. Set OCI_DNS_ENABLED=true to allow it."
        )
    if not os.getenv("OCI_DNS_ZONE"):
        raise ProvisionError(
            "DNS is not configured: set OCI_DNS_ZONE to the zone records are created "
            "in (e.g. internal.example.com)."
        )


def _require_manifest(resource_kind: str) -> None:
    """Enforce the preconditions a blueprint declares for itself.

    A recipe added as a manifest has no hand-written gate here, so without this
    it would reach Terraform and fail with a provider error instead of a sentence
    naming the setting that is missing.
    """
    if not resource_kind:
        return
    manifest = blueprint_registry.for_resource_kind(resource_kind)
    if not manifest or manifest.get("ready", True):
        return
    missing = ", ".join(manifest.get("missing_config") or [])
    raise ProvisionError(
        f"Blueprint '{manifest.get('ref', resource_kind)}' is not configured: set "
        f"{missing}."
    )


def _oci_vars(name: str, tags: dict, resource_kind: str = "oci-bucket",
              sizing: dict | None = None) -> dict:
    sizing = sizing or {}
    return {
        "tenancy_ocid": os.getenv("OCI_TENANCY_OCID", ""),
        "user_ocid": os.getenv("OCI_USER_OCID", ""),
        "fingerprint": os.getenv("OCI_FINGERPRINT", ""),
        "private_key_path": os.getenv("OCI_PRIVATE_KEY_PATH", "/secrets/oci_api_key.pem"),
        "region": os.getenv("OCI_REGION", ""),
        "compartment_ocid": os.getenv("OCI_COMPARTMENT_OCID", ""),
        # The module branches on resource_kind: a bucket uses bucket_name, an
        # instance uses instance_name + the compute vars below. The unused set is
        # ignored (count = 0), so empty strings are fine.
        "resource_kind": resource_kind,
        "bucket_name": name if resource_kind == "oci-bucket" else "",
        # Optional compute compartment (empty = same as buckets); lets the VM live
        # in a different compartment than object storage.
        "compute_compartment_ocid": os.getenv("OCI_COMPUTE_COMPARTMENT_OCID", ""),
        "instance_name": name if resource_kind == "oci-instance" else "",
        # Shape must be compatible with the image; a *.Flex shape is sized below.
        "instance_shape": os.getenv("OCI_COMPUTE_SHAPE", "VM.Standard.E4.Flex"),
        "instance_ocpus": int(sizing.get("ocpus", 1)),
        "instance_memory_gb": int(sizing.get("memory_gb", 8)),
        # The requested disk. The modules already declared this variable and
        # defaulted it; nothing ever supplied a value, so storage was priced and
        # approved and then quietly ignored at build time.
        "boot_volume_size_in_gbs": int(sizing.get("boot_volume_gb", 50)),
        # OS family of the chosen image. A blueprint that renders its own
        # first-boot script needs it to pick package names, the systemd unit, the
        # firewall tool and the certificate paths. Blueprints that delegate to
        # configure.py get the same value baked into user_data instead, and
        # modules declaring no such variable ignore it.
        "os_family": (sizing.get("os_family") or "rhel"),
        # Where the machine PUTs its own evidence. Only blueprints declaring
        # `boot_report: template` take this as a variable — the rest already have
        # it baked into the user_data configure.py rendered, and passing it to
        # them would put an undeclared-variable warning in every apply log.
        # Removed again below for those; see _cloud_vars.
        "boot_report_url": (sizing.get("boot_report_url") or ""),
        # The tier's own subnet when the per-tier map is in force,
        # otherwise the single configured one — which is the migration
        # state, and exactly what every machine used before.
        "subnet_ocid": (sizing.get("compute_subnet")
                        or os.getenv("OCI_COMPUTE_SUBNET_OCID", "")),
        # Per-technology image resolved by the orchestrator (sizing["image_ocid"]);
        # falls back to the default image env for a plain/legacy call.
        "image_ocid": sizing.get("image_ocid") or os.getenv("OCI_COMPUTE_IMAGE_OCID", ""),
        # Access — both optional. A custom image with a baked-in password needs
        # neither; user_data (cloud-init) can set/enable a password if required.
        "ssh_authorized_key": os.getenv("OCI_COMPUTE_SSH_AUTHORIZED_KEY", ""),
        # Per-request first-boot configuration (GAP-ANALYSIS step 4) when the
        # orchestrator rendered one; otherwise the static env default, so existing
        # deployments behave exactly as before.
        "user_data": sizing.get("user_data") or os.getenv("OCI_COMPUTE_USER_DATA", ""),
        # Ports the generic service blueprint opens. Empty for every other kind,
        # whose modules do not declare the variable and ignore it.
        "service_ports": list(sizing.get("service_ports") or []),
        # How many nodes a clustered resource gets, from the request's size. A
        # single-machine blueprint ignores it; without it a Kubernetes node pool
        # would silently use its own default and the size the approver priced
        # would mean nothing.
        "node_count": int(sizing.get("node_count", 1)),
        # --- OKE (Kubernetes) -------------------------------------------------
        # This blueprint builds its own VCN, so it takes network inputs of its own
        # rather than the shared compute subnet.
        "oke_bastion_allowed_cidr": os.getenv("OCI_OKE_BASTION_CIDR", ""),
        # The network OKE was GIVEN, resolved per environment tier by the
        # orchestrator. Empty for every other resource kind, which takes the
        # shared compute subnet instead. Terraform refuses a required
        # variable left empty, so a wiring mistake stops at plan rather than
        # building a cluster somewhere unintended.
        "vcn_id": sizing.get("vcn_id", ""),
        "api_subnet_id": sizing.get("api_subnet_id", ""),
        "node_subnet_id": sizing.get("node_subnet_id", ""),
        "pod_subnet_id": sizing.get("pod_subnet_id", ""),
        "lb_subnet_id": sizing.get("lb_subnet_id", ""),
        "bastion_subnet_id": sizing.get("bastion_subnet_id", ""),
        "oke_kubernetes_version": os.getenv("OCI_OKE_KUBERNETES_VERSION", ""),
        "oke_cluster_type": os.getenv("OCI_OKE_CLUSTER_TYPE", ""),
        # --- Kafka ------------------------------------------------------------
        # Where the Kafka archive is fetched from. It is a pre-authenticated URL
        # into Object Storage, so it lives in the environment rather than in the
        # manifest: a manifest is committed to git, and this grants read access
        # to a bucket.
        "kafka_source_url": os.getenv("OCI_KAFKA_SOURCE_URL", ""),
        "tags": tags,
        # Customer-managed encryption key for sensitive data (F-SEC-04); empty
        # falls back to Oracle-managed encryption in the module.
        "kms_key_id": os.getenv("OCI_KMS_KEY_OCID", ""),
        # --- Managed PostgreSQL (resource_kind = oci-postgres) ---------------
        # The admin password is referenced by VAULT SECRET OCID and never passed
        # as a value, so it never reaches the plan file or Terraform state.
        "db_compartment_ocid": os.getenv("OCI_PSQL_COMPARTMENT_OCID", ""),
        "db_name": name if resource_kind == "oci-postgres" else "",
        "db_version": os.getenv("OCI_PSQL_VERSION", "14"),
        "db_shape": sizing.get("db_shape") or os.getenv(
            "OCI_PSQL_SHAPE", "PostgreSQL.VM.Standard.E4.Flex.2.32GB"),
        "db_instance_count": int(sizing.get("db_instance_count", 1)),
        "db_subnet_ocid": os.getenv("OCI_PSQL_SUBNET_OCID", ""),
        "db_storage_iops": int(os.getenv("OCI_PSQL_STORAGE_IOPS", "75000")),
        "db_admin_username": os.getenv("OCI_PSQL_ADMIN_USERNAME", "pgadmin"),
        "db_admin_secret_ocid": os.getenv("OCI_PSQL_ADMIN_SECRET_OCID", ""),
        "db_admin_secret_version": int(os.getenv("OCI_PSQL_ADMIN_SECRET_VERSION", "1")),
        # --- DNS record (GAP-ANALYSIS step 5) ---------------------------------
        # Blank dns_name = no record, so every existing environment plans exactly
        # as before until a DNS request supplies one.
        "dns_name": sizing.get("dns_name", ""),
        "dns_type": sizing.get("dns_type", "") or "A",
        "dns_value": sizing.get("dns_value", ""),
        "dns_zone": os.getenv("OCI_DNS_ZONE", ""),
        "dns_ttl": int(os.getenv("OCI_DNS_TTL", "300")),
        "dns_compartment_ocid": os.getenv("OCI_DNS_COMPARTMENT_OCID", ""),
    }


# --- AWS (multi-cloud): S3 bucket provisioning -------------------------------
# Credentials are the standard AWS env vars, read by the Terraform AWS provider —
# never held in code or git (P3). Mock mode creates nothing; a real apply needs
# these set, or it refuses (mirroring the OCI gate).

def _require_aws() -> None:
    missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION")
               if not os.getenv(k)]
    if missing:
        raise ProvisionError(f"AWS not configured: missing {', '.join(missing)}")


def _aws_vars(name: str, tags: dict, resource_kind: str = "aws-bucket") -> dict:
    return {
        "region": os.getenv("AWS_REGION", ""),
        "resource_kind": resource_kind,
        "bucket_name": name if resource_kind == "aws-bucket" else "",
        "tags": tags,
        # Customer-managed encryption key ARN for sensitive data (F-SEC-04); empty
        # falls back to AWS-managed (SSE-S3) in the module.
        "kms_key_arn": os.getenv("AWS_KMS_KEY_ARN", ""),
    }


def _cloud_of(resource_kind: str) -> str:
    """Which cloud a resource_kind targets: 'aws' for aws-*, else 'oci'."""
    return "aws" if (resource_kind or "").startswith("aws-") else "oci"


def _module_dir(cloud: str, resource_kind: str = "") -> Path:
    """Where this resource kind's Terraform lives.

    A blueprint manifest may name its own module directory, and that takes
    precedence — this is what makes blueprints pluggable: adding a recipe is
    adding a folder and a manifest, not editing dispatch code.

    Falls back to the legacy layout (AWS in its subdir, OCI in the flat root
    module) when no manifest claims the kind, or when a manifest says `module: .`
    because its recipe is still a branch inside the shared module.
    """
    if resource_kind:
        manifest = blueprint_registry.for_resource_kind(resource_kind) or {}
        module = (manifest.get("module") or "").strip()
        if module and module != ".":
            candidate = MODULE_DIR / module
            if candidate.is_dir():
                return candidate
    return MODULE_DIR / "aws" if cloud == "aws" else MODULE_DIR


def _require_cloud(cloud: str, resource_kind: str, creating: bool = True) -> None:
    """Check what this operation needs.

    CREDENTIALS are always required — nothing can talk to a cloud without them.

    The per-service OPT-IN switches (compute, managed PostgreSQL) are
    **create-only**. They exist to stop the portal accidentally creating
    something billable; applying them to destroy would trap an expensive resource:
    turn the switch off after provisioning — the natural thing to do — and you
    could no longer tear the resource down through the portal, only by hand in the
    cloud console, losing the audit trail. Destroying is the safe direction: it
    stops cost, it never starts it. The same applies to a read-only drift check.

    So `creating=False` (destroy / drift) keeps the credential requirement and
    skips the spend gates.
    """
    if cloud == "aws":
        _require_aws()
        return
    _require_oci()
    if not creating:
        return
    if resource_kind == "oci-instance":
        _require_compute()
    elif resource_kind == "oci-postgres":
        _require_psql()
    else:
        _require_manifest(resource_kind)


def _cloud_vars(cloud: str, name: str, tags: dict, resource_kind: str, sizing: dict | None) -> dict:
    base = _aws_vars(name, tags, resource_kind) if cloud == "aws"         else _oci_vars(name, tags, resource_kind, sizing)
    # A blueprint may set defaults its own module needs, without those having to
    # be known by this code. Declared in the manifest, so adding a recipe with
    # unusual inputs stays a data change.
    manifest = blueprint_registry.for_resource_kind(resource_kind) or {} if resource_kind else {}
    extra = manifest.get("vars")
    if isinstance(extra, dict):
        base = {**base, **extra}
    # Which variable carries the environment name is the module's business, so
    # the manifest names it. Applied AFTER the merge: the per-request name must
    # beat any static default. Without this a new blueprint got "" and failed at
    # plan time on its own name validation.
    name_var = manifest.get("name_var")
    if name_var:
        base[name_var] = name
    # Same precedence for the OS image: an admin's per-technology choice is a
    # deliberate decision and beats the blueprint's own default, which in turn
    # beats the shared compute default already in the base set.
    if sizing and sizing.get("image_ocid_explicit"):
        base["image_ocid"] = sizing["image_ocid_explicit"]
    # Only a module that renders its own cloud-init declares boot_report_url.
    # Handing it to the others is harmless but noisy, and a log full of routine
    # warnings is where a real one goes unread.
    if manifest.get("boot_report") != "template":
        base.pop("boot_report_url", None)
    return base


def workspace_path(reference: str, resource_kind: str = "") -> Path:
    """Where this request's Terraform state for one resource kind lives.

    A stack has one workspace per resource, under <reference>/<kind>. Terraform
    only reads .tf files in its own directory, so the subdirectories are
    invisible to each other.

    LEGACY: requests provisioned before this layout keep their flat
    <reference> directory forever. Their state file is there, and a workspace
    that cannot find its state believes the resource does not exist — it would
    plan to create a second one and could never destroy the first. Real, running
    infrastructure is on the other end of this decision, so the flat layout wins
    whenever a state file is sitting in it.
    """
    flat = STATE_ROOT / reference
    if (flat / "terraform.tfstate").exists():
        return flat
    return flat / resource_kind if resource_kind else flat


def existing_workspaces(reference: str) -> dict[str, Path]:
    """Resource kind -> workspace, for every workspace this request actually has
    state in. Used by destroy and drift so an operation can never silently skip a
    resource, and never fail on one that was never built."""
    found: dict[str, Path] = {}
    flat = STATE_ROOT / reference
    if not flat.is_dir():
        return found
    if (flat / "terraform.tfstate").exists():
        found[""] = flat          # legacy flat layout: the kind is the request's
        return found
    for child in sorted(flat.iterdir()):
        if child.is_dir() and (child / "terraform.tfstate").exists():
            found[child.name] = child
    return found


def _workdir(reference: str, cloud: str = "oci", resource_kind: str = "") -> Path:
    """Per-request working dir on the persistent volume, seeded with the module.

    A workdir only ever holds one module (a request's cloud and kind are fixed).

    A dedicated blueprint directory is copied WHOLE — templates, scripts and any
    other support files — because a module that uses templatefile() would
    otherwise arrive without the thing it renders. The legacy shared module is
    still copied as *.tf only: its directory now contains the per-blueprint
    subfolders, which must not be dragged into every workspace.
    """
    workdir = workspace_path(reference, resource_kind)
    workdir.mkdir(parents=True, exist_ok=True)
    source = _module_dir(cloud, resource_kind)
    legacy = source in (MODULE_DIR, MODULE_DIR / "aws")

    if legacy:
        for tf in source.glob("*.tf"):
            dest = workdir / tf.name
            if not dest.exists():
                shutil.copy(tf, dest)
        return workdir

    for item in source.iterdir():
        # Never copy provider caches or state from the source tree.
        if item.name.startswith(".") or item.name.startswith("terraform.tfstate"):
            continue
        dest = workdir / item.name
        if dest.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy(item, dest)
    return workdir


def _write_tfvars(workdir: Path, variables: dict) -> None:
    (workdir / "terraform.tfvars.json").write_text(json.dumps(variables), encoding="utf-8")


DEFAULT_COMMAND_TIMEOUT = 600


def _timeout_for(resource_kind: str) -> int:
    """How long a single Terraform command may run for this blueprint.

    Ten minutes suits a VM or a bucket and is far too short for a Kubernetes
    cluster, which OCI takes 10-20 minutes to build. A timeout that fires part
    way through an apply is the worst outcome available: the resources exist and
    are billing, but Terraform never recorded them, so a later destroy cannot
    find them. Each blueprint declares what it needs.
    """
    manifest = blueprint_registry.for_resource_kind(resource_kind) if resource_kind else None
    try:
        return max(60, int((manifest or {}).get("command_timeout_seconds")
                           or DEFAULT_COMMAND_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_COMMAND_TIMEOUT


def _run(args: list[str], workdir: Path,
         timeout: int = DEFAULT_COMMAND_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _plan_summary(stdout: str) -> str:
    match = re.search(r"Plan: .*", stdout)
    if match:
        return match.group(0)
    if "No changes." in stdout:
        return "No changes."
    return "plan generated"


def _summary(stdout: str, pattern: str, fallback: str) -> str:
    match = re.search(pattern, stdout)
    return match.group(0) if match else fallback


def terraform_plan(reference: str, name: str, tags: dict,
                   resource_kind: str = "oci-bucket", sizing: dict | None = None) -> dict:
    """Init + plan in the request's workspace, saving the plan. Creates nothing."""
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind)
    workdir = _workdir(reference, cloud, resource_kind)
    tmo = _timeout_for(resource_kind)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    init = _run(["init", "-input=false", "-no-color"], workdir, timeout=tmo)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"], workdir, timeout=tmo)
    if plan.returncode != 0:
        raise ProvisionError(f"terraform plan failed: {plan.stderr[-800:]}")

    return {"summary": _plan_summary(plan.stdout), "output": plan.stdout[-4000:],
            "scan": _scan_saved_plan(workdir, tags.get("classification"))}


_EMPTY_SCAN = {"findings": [], "counts": {"high": 0, "medium": 0, "low": 0},
               "high": 0, "ok": True}


def _scan_saved_plan(workdir: Path, classification: str | None) -> dict:
    """Best-effort IaC scan of the saved plan (F-SEC-03/04) via `terraform show
    -json`. Never breaks the plan — a scan hiccup returns an empty (ok) result."""
    try:
        show = _run(["show", "-json", PLAN_FILE], workdir, timeout=tmo)
        if show.returncode != 0:
            return {**_EMPTY_SCAN, "error": "terraform show failed"}
        return scanner.scan_plan(json.loads(show.stdout), classification)
    except Exception as exc:  # noqa: BLE001
        return {**_EMPTY_SCAN, "error": str(exc)}


def terraform_apply(reference: str, name: str, tags: dict,
                    resource_kind: str = "oci-bucket", sizing: dict | None = None) -> dict:
    """Apply the EXACT saved plan for this request. CREATES the resource."""
    if provision_mode() != "apply":
        raise ProvisionError("apply is not enabled (PROVISION_MODE is not 'apply')")
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind)
    workdir = _workdir(reference, cloud, resource_kind)
    tmo = _timeout_for(resource_kind)
    if not (workdir / PLAN_FILE).exists():
        raise ProvisionError("no saved plan for this request — approve (plan) it first")

    init = _run(["init", "-input=false", "-no-color"], workdir, timeout=tmo)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    apply = _run(["apply", "-input=false", "-no-color", PLAN_FILE], workdir, timeout=tmo)
    if apply.returncode != 0:
        raise ProvisionError(f"terraform apply failed: {apply.stderr[-1200:]}")

    outputs = {}
    out = _run(["output", "-json"], workdir, timeout=tmo)
    if out.returncode == 0:
        try:
            outputs = {k: v.get("value") for k, v in json.loads(out.stdout).items()}
        except Exception:  # noqa: BLE001
            outputs = {}

    return {
        "summary": _summary(apply.stdout, r"Apply complete!.*", "apply complete"),
        "outputs": outputs,
        "output": apply.stdout[-4000:],
    }


def terraform_drift(reference: str, name: str, tags: dict,
                    resource_kind: str = "oci-bucket", sizing: dict | None = None) -> dict:
    """Re-plan a provisioned request's existing workspace and detect drift from
    the applied state (F-LCM-09). Read-only — a plan creates nothing, so the
    spend gates don't apply (creating=False): you must still be able to inspect an
    existing resource after the switch that created it has been turned off."""
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind, creating=False)
    workdir = _workdir(reference, cloud, resource_kind)
    tmo = _timeout_for(resource_kind)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    init = _run(["init", "-input=false", "-no-color"], workdir, timeout=tmo)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"], workdir, timeout=tmo)
    if plan.returncode != 0:
        raise ProvisionError(f"terraform plan failed: {plan.stderr[-800:]}")

    show = _run(["show", "-json", PLAN_FILE], workdir, timeout=tmo)
    if show.returncode != 0:
        raise ProvisionError(f"terraform show failed: {show.stderr[-800:]}")

    return {**drift.detect_drift(json.loads(show.stdout)),
            "summary": _plan_summary(plan.stdout)}


def terraform_destroy(reference: str, name: str, tags: dict,
                      resource_kind: str = "oci-bucket", sizing: dict | None = None) -> dict:
    """Destroy the resources for this request from its own state.

    creating=False: tearing down must never be blocked by the switch that gated
    creation, or an expensive resource becomes stuck (see _require_cloud)."""
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind, creating=False)
    workdir = _workdir(reference, cloud, resource_kind)
    tmo = _timeout_for(resource_kind)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    _run(["init", "-input=false", "-no-color"], workdir, timeout=tmo)
    destroy = _run(["destroy", "-input=false", "-no-color", "-auto-approve"], workdir, timeout=tmo)
    if destroy.returncode != 0:
        raise ProvisionError(f"terraform destroy failed: {destroy.stderr[-1200:]}")
    return {
        "summary": _summary(destroy.stdout, r"Destroy complete!.*", "destroy complete"),
        "output": destroy.stdout[-4000:],
    }
