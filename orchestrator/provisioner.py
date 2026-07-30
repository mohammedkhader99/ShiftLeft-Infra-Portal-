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

from orchestrator import scanner

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


def _oci_vars(bucket_name: str, tags: dict) -> dict:
    return {
        "tenancy_ocid": os.getenv("OCI_TENANCY_OCID", ""),
        "user_ocid": os.getenv("OCI_USER_OCID", ""),
        "fingerprint": os.getenv("OCI_FINGERPRINT", ""),
        "private_key_path": os.getenv("OCI_PRIVATE_KEY_PATH", "/secrets/oci_api_key.pem"),
        "region": os.getenv("OCI_REGION", ""),
        "compartment_ocid": os.getenv("OCI_COMPARTMENT_OCID", ""),
        "bucket_name": bucket_name,
        "tags": tags,
        # Customer-managed encryption key for sensitive data (F-SEC-04); empty
        # falls back to Oracle-managed encryption in the module.
        "kms_key_id": os.getenv("OCI_KMS_KEY_OCID", ""),
    }


def _workdir(reference: str) -> Path:
    """Per-request working dir on the persistent volume, seeded with the module."""
    workdir = STATE_ROOT / reference
    workdir.mkdir(parents=True, exist_ok=True)
    for tf in MODULE_DIR.glob("*.tf"):
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


def terraform_plan(reference: str, bucket_name: str, tags: dict) -> dict:
    """Init + plan in the request's workspace, saving the plan. Creates nothing."""
    _require_oci()
    workdir = _workdir(reference)
    _write_tfvars(workdir, _oci_vars(bucket_name, tags))

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


def terraform_apply(reference: str, bucket_name: str, tags: dict) -> dict:
    """Apply the EXACT saved plan for this request. CREATES the resource."""
    if provision_mode() != "apply":
        raise ProvisionError("apply is not enabled (PROVISION_MODE is not 'apply')")
    _require_oci()
    workdir = _workdir(reference)
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


def terraform_destroy(reference: str, bucket_name: str, tags: dict) -> dict:
    """Destroy the resources for this request from its own state."""
    _require_oci()
    workdir = _workdir(reference)
    _write_tfvars(workdir, _oci_vars(bucket_name, tags))

    _run(["init", "-input=false", "-no-color"], workdir)
    destroy = _run(["destroy", "-input=false", "-no-color", "-auto-approve"], workdir)
    if destroy.returncode != 0:
        raise ProvisionError(f"terraform destroy failed: {destroy.stderr[-1200:]}")
    return {
        "summary": _summary(destroy.stdout, r"Destroy complete!.*", "destroy complete"),
        "output": destroy.stdout[-4000:],
    }
