"""Terraform provisioner (increment 2.6a — plan only).

PROVISION_MODE:
- mock  (default): no Terraform, no cloud — the orchestrator returns a mock result.
- plan            : run `terraform plan` against the OCI sandbox and return the
                    plan. NOTHING is created — there is no apply path in 2.6a.
- apply           : NOT enabled yet (2.6b). Rejected here on purpose.

Credentials come from the environment / a mounted key file, supplied by the
reviewer — never held in code or git (P3).
"""

import os
import re
import subprocess
from pathlib import Path

TF_DIR = Path(__file__).resolve().parent / "terraform"


class ProvisionError(RuntimeError):
    """Terraform failed or is misconfigured."""


def provision_mode() -> str:
    return os.getenv("PROVISION_MODE", "mock").strip().lower()


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
    }


def _write_tfvars(variables: dict) -> None:
    import json

    (TF_DIR / "terraform.tfvars.json").write_text(json.dumps(variables), encoding="utf-8")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=str(TF_DIR),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _plan_summary(stdout: str) -> str:
    match = re.search(r"Plan: .*", stdout)
    if match:
        return match.group(0)
    if "No changes." in stdout:
        return "No changes."
    return "plan generated"


def terraform_plan(bucket_name: str, tags: dict) -> dict:
    """Run init + plan against OCI. Returns a summary + output. Creates nothing."""
    missing = [k for k in ("OCI_TENANCY_OCID", "OCI_COMPARTMENT_OCID", "OCI_REGION")
               if not os.getenv(k)]
    if missing:
        raise ProvisionError(f"OCI not configured: missing {', '.join(missing)}")

    _write_tfvars(_oci_vars(bucket_name, tags))

    init = _run(["init", "-input=false", "-no-color"])
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color"])
    if plan.returncode != 0:
        raise ProvisionError(f"terraform plan failed: {plan.stderr[-800:]}")

    return {
        "summary": _plan_summary(plan.stdout),
        "output": plan.stdout[-4000:],
    }
