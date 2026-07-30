"""IaC security scanner (F-SEC-03/04).

A dependency-free scan of the Terraform *plan* (from `terraform show -json`) for
security misconfigurations, before anything is applied. This complements the OPA
gate (which checks the request) with checks on the generated infrastructure —
shift-left security on the IaC itself.

Deliberately not tfsec/checkov: a small built-in ruleset keeps the orchestrator
image offline-safe and lets the rules target exactly what this portal provisions
(OCI object storage today). The dispatch is by resource type, so new resource
types get their own rules without touching the caller.
"""

MANDATORY_TAGS = ("managed_by", "reference", "cost_centre", "classification")
# OCI bucket access types that expose objects publicly.
PUBLIC_ACCESS = {"ObjectRead", "ObjectReadWithoutList"}
SENSITIVE = {"restricted", "confidential"}


def _finding(rule: str, severity: str, resource: str, message: str) -> dict:
    return {"rule": rule, "severity": severity, "resource": resource, "message": message}


def _scan_bucket(addr: str, after: dict, classification: str) -> list[dict]:
    """Rules for oci_objectstorage_bucket."""
    out: list[dict] = []

    access = after.get("access_type") or "NoPublicAccess"
    if access in PUBLIC_ACCESS:
        out.append(_finding("bucket-public-access", "high", addr,
                            f"Bucket allows public access (access_type={access}); "
                            f"object storage should not be publicly readable."))

    tags = after.get("freeform_tags") or {}
    missing = [t for t in MANDATORY_TAGS if not str(tags.get(t, "")).strip()]
    if missing:
        out.append(_finding("bucket-missing-tags", "medium", addr,
                            f"Missing mandatory tag(s): {', '.join(missing)}."))

    if classification in SENSITIVE and not str(after.get("kms_key_id") or "").strip():
        out.append(_finding("bucket-no-cmk", "high", addr,
                            f"{classification.title()} data in a bucket without a "
                            f"customer-managed encryption key (kms_key_id)."))

    if (after.get("versioning") or "Disabled") != "Enabled":
        out.append(_finding("bucket-versioning-disabled", "low", addr,
                            "Object versioning is not enabled (no recovery from "
                            "accidental overwrite or delete)."))
    return out


_SCANNERS = {
    "oci_objectstorage_bucket": _scan_bucket,
}


def scan_plan(plan_json: dict, classification: str | None = None) -> dict:
    """Scan a `terraform show -json` plan document. Returns
    {findings, counts, high, ok}. Only resources being created/updated are scanned."""
    classification = (classification or "").strip().lower()
    findings: list[dict] = []
    for rc in plan_json.get("resource_changes", []):
        actions = (rc.get("change") or {}).get("actions", [])
        if "create" not in actions and "update" not in actions:
            continue  # deletes / no-ops carry no new misconfiguration
        scanner = _SCANNERS.get(rc.get("type", ""))
        if scanner is None:
            continue
        after = (rc.get("change") or {}).get("after") or {}
        findings.extend(scanner(rc.get("address") or rc.get("type"), after, classification))

    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"findings": findings, "counts": counts,
            "high": counts["high"], "ok": counts["high"] == 0}
