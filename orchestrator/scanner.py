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


def _scan_instance(addr: str, after: dict, classification: str) -> list[dict]:
    """Rules for oci_core_instance (C5b).

    Written from a real failure. The OKE bastion carried assign_public_ip = true
    and built perfectly for months — a green apply says nothing about exposure,
    which is exactly why ARCHITECTURE.md §8 keeps security out of the proof's
    verdict.
    """
    out: list[dict] = []
    for vnic in after.get("create_vnic_details") or []:
        if not isinstance(vnic, dict):
            continue
        if vnic.get("assign_public_ip") in (True, "true"):
            out.append(_finding("instance-public-ip", "high", addr,
                                "Instance requests a public IP. Machines here are "
                                "private-only and reached through a bastion."))
    tags = after.get("freeform_tags") or {}
    missing = [x for x in MANDATORY_TAGS if not str(tags.get(x, "")).strip()]
    if missing:
        out.append(_finding("instance-missing-tags", "medium", addr,
                            f"Missing mandatory tag(s): {', '.join(missing)}."))
    return out


def _scan_psql(addr: str, after: dict, classification: str) -> list[dict]:
    """Rules for oci_psql_db_system (C5b)."""
    out: list[dict] = []
    for net in after.get("network_details") or []:
        if isinstance(net, dict) and net.get("is_reader_endpoint_enabled") in (True, "true"):
            out.append(_finding("psql-reader-endpoint", "medium", addr,
                                "A reader endpoint is enabled; confirm it belongs "
                                "in a private subnet before offering this."))
    for cred in after.get("credentials") or []:
        if not isinstance(cred, dict):
            continue
        for details in cred.get("password_details") or []:
            if isinstance(details, dict) and details.get("password_type") != "VAULT_SECRET":
                out.append(_finding("psql-inline-password", "high", addr,
                                    "The admin password is not referenced from a "
                                    "vault secret. Credentials are referenced by "
                                    "OCID and never passed as values."))
    tags = after.get("freeform_tags") or {}
    missing = [x for x in MANDATORY_TAGS if not str(tags.get(x, "")).strip()]
    if missing:
        out.append(_finding("psql-missing-tags", "medium", addr,
                            f"Missing mandatory tag(s): {', '.join(missing)}."))
    return out


def _scan_cluster(addr: str, after: dict, classification: str) -> list[dict]:
    """Rules for oci_containerengine_cluster (C5b)."""
    out: list[dict] = []
    for endpoint in after.get("endpoint_config") or []:
        if isinstance(endpoint, dict) and endpoint.get("is_public_ip_enabled") in (True, "true"):
            out.append(_finding("cluster-public-api", "high", addr,
                                "The Kubernetes API endpoint is public. Clusters "
                                "here are private and reached from inside the VCN."))
    return out


_SCANNERS = {
    "oci_objectstorage_bucket": _scan_bucket,
    "oci_core_instance": _scan_instance,
    "oci_psql_db_system": _scan_psql,
    "oci_containerengine_cluster": _scan_cluster,
}


def scan_plan(plan_json: dict, classification: str | None = None,
              strict: bool = False) -> dict:
    """Scan a `terraform show -json` plan document. Returns
    {findings, counts, high, ok, unreviewed_types}.

    Only resources being created/updated are scanned — deletes carry no new
    misconfiguration.

    STRICT MODE, for agent-written modules (C5b). Normally a resource type with
    no rules is skipped, which is right for recipes people wrote and reviewed. It
    is exactly wrong for one a model wrote: "we have no rules for this" would
    otherwise read as "this is safe", and a generated module full of unfamiliar
    resource types would score a clean pass having been checked for nothing at
    all. In strict mode an unscanned type is itself a finding, so the gap is
    visible instead of silent.
    """
    classification = (classification or "").strip().lower()
    findings: list[dict] = []
    unreviewed: set[str] = set()
    for rc in plan_json.get("resource_changes", []):
        actions = (rc.get("change") or {}).get("actions", [])
        if "create" not in actions and "update" not in actions:
            continue  # deletes / no-ops carry no new misconfiguration
        rtype = rc.get("type", "")
        addr = rc.get("address") or rtype
        scanner = _SCANNERS.get(rtype)
        if scanner is None:
            unreviewed.add(rtype)
            if strict:
                findings.append(_finding(
                    "unreviewed-resource-type", "high", addr,
                    f"No security rules exist for {rtype}. This module was not "
                    f"written by a person, so an unchecked resource type is a gap, "
                    f"not a pass — add a rule or have a human review this one."))
            continue
        after = (rc.get("change") or {}).get("after") or {}
        findings.extend(scanner(addr, after, classification))

    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"findings": findings, "counts": counts,
            "high": counts["high"], "ok": counts["high"] == 0,
            # Reported even when not strict, so the gap is measurable before
            # anyone relies on it.
            "unreviewed_types": sorted(unreviewed)}
