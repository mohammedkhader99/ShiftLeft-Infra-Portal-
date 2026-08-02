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

from orchestrator import drift, scanner

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
        "subnet_ocid": os.getenv("OCI_COMPUTE_SUBNET_OCID", ""),
        # Per-technology image resolved by the orchestrator (sizing["image_ocid"]);
        # falls back to the default image env for a plain/legacy call.
        "image_ocid": sizing.get("image_ocid") or os.getenv("OCI_COMPUTE_IMAGE_OCID", ""),
        # Access — both optional. A custom image with a baked-in password needs
        # neither; user_data (cloud-init) can set/enable a password if required.
        "ssh_authorized_key": os.getenv("OCI_COMPUTE_SSH_AUTHORIZED_KEY", ""),
        "user_data": os.getenv("OCI_COMPUTE_USER_DATA", ""),
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


def _module_dir(cloud: str) -> Path:
    """The Terraform module for a cloud. OCI is the flat (default) module; AWS has
    its own subdir with its own provider."""
    return MODULE_DIR / "aws" if cloud == "aws" else MODULE_DIR


def _require_cloud(cloud: str, resource_kind: str) -> None:
    if cloud == "aws":
        _require_aws()
    else:
        _require_oci()
        if resource_kind == "oci-instance":
            _require_compute()
        elif resource_kind == "oci-postgres":
            _require_psql()


def _cloud_vars(cloud: str, name: str, tags: dict, resource_kind: str, sizing: dict | None) -> dict:
    if cloud == "aws":
        return _aws_vars(name, tags, resource_kind)
    return _oci_vars(name, tags, resource_kind, sizing)


def _workdir(reference: str, cloud: str = "oci") -> Path:
    """Per-request working dir on the persistent volume, seeded with the cloud's
    module. A workdir only ever holds one cloud's .tf (a request's cloud is fixed)."""
    workdir = STATE_ROOT / reference
    workdir.mkdir(parents=True, exist_ok=True)
    for tf in _module_dir(cloud).glob("*.tf"):
        dest = workdir / tf.name
        if not dest.exists():
            shutil.copy(tf, dest)
    return workdir


def _write_tfvars(workdir: Path, variables: dict) -> None:
    (workdir / "terraform.tfvars.json").write_text(json.dumps(variables), encoding="utf-8")


def _run(args: list[str], workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=600,
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
    workdir = _workdir(reference, cloud)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    init = _run(["init", "-input=false", "-no-color"], workdir)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"], workdir)
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
        show = _run(["show", "-json", PLAN_FILE], workdir)
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
    workdir = _workdir(reference, cloud)
    if not (workdir / PLAN_FILE).exists():
        raise ProvisionError("no saved plan for this request — approve (plan) it first")

    init = _run(["init", "-input=false", "-no-color"], workdir)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    apply = _run(["apply", "-input=false", "-no-color", PLAN_FILE], workdir)
    if apply.returncode != 0:
        raise ProvisionError(f"terraform apply failed: {apply.stderr[-1200:]}")

    outputs = {}
    out = _run(["output", "-json"], workdir)
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
    the applied state (F-LCM-09). Read-only — a plan creates nothing."""
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind)
    workdir = _workdir(reference, cloud)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    init = _run(["init", "-input=false", "-no-color"], workdir)
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"], workdir)
    if plan.returncode != 0:
        raise ProvisionError(f"terraform plan failed: {plan.stderr[-800:]}")

    show = _run(["show", "-json", PLAN_FILE], workdir)
    if show.returncode != 0:
        raise ProvisionError(f"terraform show failed: {show.stderr[-800:]}")

    return {**drift.detect_drift(json.loads(show.stdout)),
            "summary": _plan_summary(plan.stdout)}


def terraform_destroy(reference: str, name: str, tags: dict,
                      resource_kind: str = "oci-bucket", sizing: dict | None = None) -> dict:
    """Destroy the resources for this request from its own state."""
    cloud = _cloud_of(resource_kind)
    _require_cloud(cloud, resource_kind)
    workdir = _workdir(reference, cloud)
    _write_tfvars(workdir, _cloud_vars(cloud, name, tags, resource_kind, sizing))

    _run(["init", "-input=false", "-no-color"], workdir)
    destroy = _run(["destroy", "-input=false", "-no-color", "-auto-approve"], workdir)
    if destroy.returncode != 0:
        raise ProvisionError(f"terraform destroy failed: {destroy.stderr[-1200:]}")
    return {
        "summary": _summary(destroy.stdout, r"Destroy complete!.*", "destroy complete"),
        "output": destroy.stdout[-4000:],
    }
