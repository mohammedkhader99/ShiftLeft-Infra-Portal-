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


def _require_oci() -> None:
    missing = [k for k in ("OCI_TENANCY_OCID", "OCI_COMPARTMENT_OCID", "OCI_REGION")
               if not os.getenv(k)]
    if missing:
        raise ProvisionError(f"OCI not configured: missing {', '.join(missing)}")


def _summary(stdout: str, pattern: str, fallback: str) -> str:
    import re
    match = re.search(pattern, stdout)
    return match.group(0) if match else fallback


def terraform_plan(bucket_name: str, tags: dict) -> dict:
    """Run init + plan against OCI. Returns a summary + output. Creates nothing."""
    _require_oci()
    _write_tfvars(_oci_vars(bucket_name, tags))

    init = _run(["init", "-input=false", "-no-color"])
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    plan = _run(["plan", "-input=false", "-no-color"])
    if plan.returncode != 0:
        raise ProvisionError(f"terraform plan failed: {plan.stderr[-800:]}")

    return {"summary": _plan_summary(plan.stdout), "output": plan.stdout[-4000:]}


def terraform_apply(bucket_name: str, tags: dict) -> dict:
    """Run init + apply against OCI. CREATES the resource. Requires apply mode."""
    if provision_mode() != "apply":
        raise ProvisionError("apply is not enabled (PROVISION_MODE is not 'apply')")
    _require_oci()
    _write_tfvars(_oci_vars(bucket_name, tags))

    init = _run(["init", "-input=false", "-no-color"])
    if init.returncode != 0:
        raise ProvisionError(f"terraform init failed: {init.stderr[-800:]}")

    apply = _run(["apply", "-input=false", "-no-color", "-auto-approve"])
    if apply.returncode != 0:
        raise ProvisionError(f"terraform apply failed: {apply.stderr[-1200:]}")

    outputs = {}
    out = _run(["output", "-json"])
    if out.returncode == 0:
        try:
            import json
            outputs = {k: v.get("value") for k, v in json.loads(out.stdout).items()}
        except Exception:  # noqa: BLE001
            outputs = {}

    return {
        "summary": _summary(apply.stdout, r"Apply complete!.*", "apply complete"),
        "outputs": outputs,
        "output": apply.stdout[-4000:],
    }


def terraform_destroy(bucket_name: str, tags: dict) -> dict:
    """Run destroy against OCI — removes the resource (rollback / cleanup)."""
    _require_oci()
    _write_tfvars(_oci_vars(bucket_name, tags))

    _run(["init", "-input=false", "-no-color"])
    destroy = _run(["destroy", "-input=false", "-no-color", "-auto-approve"])
    if destroy.returncode != 0:
        raise ProvisionError(f"terraform destroy failed: {destroy.stderr[-1200:]}")
    return {
        "summary": _summary(destroy.stdout, r"Destroy complete!.*", "destroy complete"),
        "output": destroy.stdout[-4000:],
    }
