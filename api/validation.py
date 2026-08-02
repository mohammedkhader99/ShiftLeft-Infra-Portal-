"""Authoritative server-side validation for request submission (increment 1.3).

This is the single source of truth for the rules (ARCHITECTURE.md P2 — the
client's validation is UX only). Each failure returns a plain-language message
that states the rule and, where useful, the compliant option (F-UX-10).

Draft saving is deliberately lenient (partial data allowed); these rules apply
only when a request is *submitted*.
"""

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Backup, CostCentre, Environment, Project, Request, Subsidiary, Technology

REQUEST_TYPES = {"create", "add", "resize", "decommission", "refresh", "restore", "clone"}
SIZES = {"small", "medium", "large", "xlarge"}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
SENSITIVE_CLASSIFICATIONS = {"restricted", "confidential"}  # a refresh must mask these
DEPLOYMENT_TARGETS = {"onprem", "azure", "oci"}
# Environment tier ladder (increment 6.2, from the UX brief).
ENV_TIERS = {"dev", "test", "sit", "uat", "preprod", "prod", "dr"}
NONPROD_TIERS = {"dev", "test", "sit", "uat", "preprod"}
# Ordered ladder for "refresh a lower env from a higher one" (F-LCM-03). DR sits
# with prod (it mirrors prod).
TIER_ORDER = {"dev": 0, "test": 1, "sit": 2, "uat": 3, "preprod": 4, "prod": 5, "dr": 5}

# Advanced options (6.5, from the UX brief). Value spec per key:
#   frozenset -> the value must be one of these; "bool" -> a boolean;
#   None -> free text. Every option is optional. The first four drive cost
#   (see api/pricing.py); the rest are captured for the approver.
_BOOL = "bool"
ADVANCED_OPTIONS: dict[str, object] = {
    # cost-affecting
    "high_availability": _BOOL,
    "backup_retention": frozenset({"none", "7", "30", "90"}),
    "monitoring_level": frozenset({"none", "basic", "enhanced"}),
    "support_tier": frozenset({"standard", "business", "premium"}),
    # capture-only
    "region": frozenset({"uae-north", "uae-central", "eu-west", "us-east", "ap-south"}),
    "availability_zone": frozenset({"single", "az-1", "az-2", "az-3", "multi-az"}),
    "database_version": None,
    "encryption": frozenset({"none", "at-rest", "in-transit", "at-rest-and-in-transit"}),
    "disaster_recovery": frozenset({"none", "backup-restore", "warm-standby", "active-active"}),
    "logging_level": frozenset({"none", "standard", "verbose"}),
    "storage_tier": frozenset({"standard", "performance", "archive"}),
    "autoscaling": _BOOL,
    "network_type": frozenset({"public", "private", "isolated"}),
    "firewall_profile": frozenset({"default", "restricted", "custom"}),
    "private_endpoint": _BOOL,
    "public_endpoint": _BOOL,
    "dns": frozenset({"none", "internal", "external"}),
    "certificates": frozenset({"none", "self-signed", "ca-signed"}),
    "secrets_management": frozenset({"none", "vault", "cloud-kms"}),
    "compliance_profile": frozenset({"none", "iso-27001", "pci-dss", "hipaa", "uae-ia"}),
}
# Governance metadata value sets (increment 6.1, from the UX brief).
PRIORITIES = {"low", "medium", "high", "critical"}
CRITICALITIES = {"tier1", "tier2", "tier3", "tier4"}
MIN_JUSTIFICATION = 20
# Simple naming standard for now (F-CAT-04 full engine comes later).
NAME_PATTERN = re.compile(r"^[a-z0-9-]{3,40}$")

# Request types that add to / resize an existing seeded environment.
TARGET_TYPES = {"add", "resize"}
# Request types that must carry at least one technology component. Clone provisions
# a new environment with the source's stack, so it carries components too.
COMPONENT_TYPES = {"create", "add", "resize", "clone"}
# Request types that must carry the governance metadata (all but decommission,
# which inherits its context from the request it tears down).
METADATA_TYPES = {"create", "add", "resize", "clone"}


def validate_submission(data: dict, session: Session) -> dict[str, str]:
    """Return {field: message} for every broken rule. Empty dict means valid.

    `data['components']` is a list of {technology_code, size} dicts.
    """
    errors: dict[str, str] = {}

    request_type = (data.get("request_type") or "").strip()
    if request_type not in REQUEST_TYPES:
        errors["request_type"] = ("Choose a request type (create, add, resize, decommission, "
                                  "refresh, restore or clone).")
        # Without a valid type we can't check type-specific rules.
        return errors

    # Cost centre is required for every type except those that operate on an
    # existing provisioned request (they inherit its context).
    if request_type not in ("decommission", "refresh", "restore"):
        cost_centre = (data.get("cost_centre_code") or "").strip()
        if not cost_centre:
            errors["cost_centre_code"] = (
                "Select a cost centre so the request can be charged back."
            )
        elif session.scalar(select(CostCentre).where(CostCentre.code == cost_centre)) is None:
            errors["cost_centre_code"] = f"Unknown cost centre '{cost_centre}'."

        # Subsidiary is optional, but must be a known one if given.
        subsidiary = (data.get("subsidiary") or "").strip()
        if subsidiary and session.scalar(
            select(Subsidiary).where(Subsidiary.code == subsidiary)
        ) is None:
            errors["subsidiary"] = f"Unknown subsidiary '{subsidiary}'."

    if request_type == "create":
        _validate_create_fields(data, session, errors)
    elif request_type == "clone":
        _validate_clone_fields(data, session, errors)
    elif request_type == "decommission":
        _validate_decommission_fields(data, session, errors)
    elif request_type == "refresh":
        _validate_refresh_fields(data, session, errors)
    elif request_type == "restore":
        _validate_restore_fields(data, session, errors)
    else:  # add | resize
        target = (data.get("target_environment") or "").strip()
        if not target:
            errors["target_environment"] = (
                f"Select the existing environment to {request_type}."
            )
        elif session.scalar(
            select(Environment).where(Environment.name == target)
        ) is None:
            errors["target_environment"] = f"Unknown environment '{target}'."

    if request_type in COMPONENT_TYPES:
        target = (data.get("deployment_target") or "").strip()
        if target not in DEPLOYMENT_TARGETS:
            errors["deployment_target"] = (
                "Choose where it runs: on-prem, Azure or OCI."
            )
        _validate_components(data, session, errors)

    if request_type in METADATA_TYPES:
        _validate_metadata(data, errors)
        _validate_advanced(data.get("advanced_options"), errors)

    return errors


def _validate_advanced(options, errors: dict[str, str]) -> None:
    """Advanced options are optional, but any provided must be known + valid (6.5)."""
    if not options:
        return
    if not isinstance(options, dict):
        errors["advanced_options"] = "Advanced options must be a set of key/value choices."
        return
    for key, value in options.items():
        if value in (None, "", "none"):
            continue  # unset / explicit 'none' is always fine
        spec = ADVANCED_OPTIONS.get(key)
        if spec is None and key not in ADVANCED_OPTIONS:
            errors[f"advanced.{key}"] = f"Unknown advanced option '{key}'."
        elif spec == _BOOL:
            if not isinstance(value, bool):
                errors[f"advanced.{key}"] = f"'{key}' must be true or false."
        elif isinstance(spec, frozenset) and value not in spec:
            errors[f"advanced.{key}"] = (
                f"'{value}' is not a valid {key.replace('_', ' ')}."
            )


def _validate_metadata(data: dict, errors: dict[str, str]) -> None:
    """Governance metadata required at submission for provisioning requests (6.1).

    Owners are optional this round (free-text name/email); the rest are required
    so approvers get a justification, priority, criticality and a target date.
    """
    justification = (data.get("business_justification") or "").strip()
    if len(justification) < MIN_JUSTIFICATION:
        errors["business_justification"] = (
            f"Give a business justification of at least {MIN_JUSTIFICATION} characters "
            "so approvers understand why this is needed."
        )

    priority = (data.get("priority") or "").strip().lower()
    if priority not in PRIORITIES:
        errors["priority"] = "Select a priority (low, medium, high or critical)."

    criticality = (data.get("business_criticality") or "").strip().lower()
    if criticality not in CRITICALITIES:
        errors["business_criticality"] = (
            "Select a business criticality (tier1, tier2, tier3 or tier4)."
        )

    # The value is a date at submit (from the model) or an ISO string in
    # direct-dict callers; accept either.
    raw_date = data.get("required_delivery_date")
    delivery: date | None = None
    if isinstance(raw_date, date):
        delivery = raw_date
    elif isinstance(raw_date, str) and raw_date.strip():
        try:
            delivery = date.fromisoformat(raw_date.strip())
        except ValueError:
            errors["required_delivery_date"] = "Enter a valid delivery date (YYYY-MM-DD)."
    if "required_delivery_date" not in errors:
        if delivery is None:
            errors["required_delivery_date"] = "Choose the required delivery date."
        elif delivery < datetime.now(timezone.utc).date():
            errors["required_delivery_date"] = (
                "The required delivery date can't be in the past."
            )


def _validate_decommission_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Decommission targets a previously provisioned request (by reference) and
    tears down a chosen subset of its technology stack."""
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        errors["source_reference"] = "Select the provisioned request to decommission."
        return
    source = session.scalar(select(Request).where(Request.reference == source_ref))
    if source is None:
        errors["source_reference"] = f"Unknown request '{source_ref}'."
        return
    if source.status != "provisioned":
        errors["source_reference"] = (
            f"{source_ref} is not currently provisioned (status: {source.status}); "
            "only provisioned requests can be decommissioned."
        )
        return

    # At least one technology, each drawn from the source request's own stack.
    source_techs = {c.technology_code for c in source.components if c.technology_code}
    selected = [
        c for c in (data.get("components") or [])
        if (c.get("technology_code") or "").strip()
    ]
    if not selected:
        errors["components"] = "Select at least one technology to decommission."
        return
    for component in selected:
        tech = (component.get("technology_code") or "").strip()
        if tech not in source_techs:
            errors["components"] = (
                f"'{tech}' is not part of {source_ref}; choose from its technology stack."
            )
            break


def _validate_refresh_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Refresh (F-LCM-03) copies data from a higher environment down into a lower
    one. Guardrails: the target is a provisioned NON-PROD env; the source is a
    provisioned env NOT LOWER than the target; the two differ. Masking of sensitive
    source data is enforced downstream (auto)."""
    target_ref = (data.get("source_reference") or "").strip()          # env being refreshed
    from_ref = (data.get("refresh_from_reference") or "").strip()      # copy-from (higher)

    if not target_ref:
        errors["source_reference"] = "Select the environment to refresh."
    else:
        target = session.scalar(select(Request).where(Request.reference == target_ref))
        if target is None:
            errors["source_reference"] = f"Unknown request '{target_ref}'."
        elif target.status != "provisioned":
            errors["source_reference"] = (
                f"{target_ref} is not provisioned (status: {target.status}); only a "
                "provisioned environment can be refreshed.")
        elif (target.environment_tier or "").strip().lower() not in NONPROD_TIERS:
            errors["source_reference"] = "Only non-production environments can be refreshed (never prod/DR)."

    if not from_ref:
        errors["refresh_from_reference"] = "Select the source environment to refresh from."
    elif from_ref == target_ref:
        errors["refresh_from_reference"] = "The source and target must be different environments."
    else:
        src = session.scalar(select(Request).where(Request.reference == from_ref))
        if src is None:
            errors["refresh_from_reference"] = f"Unknown request '{from_ref}'."
        elif src.status != "provisioned":
            errors["refresh_from_reference"] = f"The source {from_ref} must be a provisioned environment."
        elif "source_reference" not in errors:  # only compare tiers if the target is valid
            target = session.scalar(select(Request).where(Request.reference == target_ref))
            stier = (src.environment_tier or "").strip().lower()
            ttier = (target.environment_tier or "").strip().lower() if target else ""
            if TIER_ORDER.get(stier, 0) < TIER_ORDER.get(ttier, 0):
                errors["refresh_from_reference"] = (
                    f"Refresh only from a higher or equal environment — {stier or '?'} is lower "
                    f"than {ttier or '?'}.")


def _validate_restore_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Restore (F-LCM-06) rolls a provisioned environment back to one of ITS OWN
    backups. Guardrails: the target is provisioned; the chosen backup exists and
    belongs to the target. (Approval-governed — a restore is destructive.)"""
    target_ref = (data.get("source_reference") or "").strip()
    backup_id = data.get("restore_backup_id")

    target = None
    if not target_ref:
        errors["source_reference"] = "Select the environment to restore."
    else:
        target = session.scalar(select(Request).where(Request.reference == target_ref))
        if target is None:
            errors["source_reference"] = f"Unknown request '{target_ref}'."
        elif target.status != "provisioned":
            errors["source_reference"] = (
                f"{target_ref} is not provisioned (status: {target.status}); only a provisioned "
                "environment can be restored.")

    if not backup_id:
        errors["restore_backup_id"] = "Select a backup to restore from."
    else:
        backup = session.get(Backup, int(backup_id)) if str(backup_id).isdigit() else None
        if backup is None:
            errors["restore_backup_id"] = "Unknown backup."
        elif target_ref and backup.reference != target_ref:
            errors["restore_backup_id"] = "That backup belongs to a different environment."


def _validate_create_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    project = (data.get("project_code") or "").strip()
    if not project:
        errors["project_code"] = "Select the project this environment belongs to."
    elif session.scalar(select(Project).where(Project.code == project)) is None:
        errors["project_code"] = f"Unknown project '{project}'."

    name = (data.get("environment_name") or "").strip()
    if not name:
        errors["environment_name"] = "Enter a name for the new environment."
    elif not NAME_PATTERN.match(name):
        errors["environment_name"] = (
            "Environment name must be lowercase letters, digits and hyphens "
            "(e.g. egate-uat), 3–40 characters."
        )

    classification = (data.get("data_classification") or "").strip()
    if classification not in CLASSIFICATIONS:
        errors["data_classification"] = (
            "Select a data classification (public, internal, confidential or restricted)."
        )

    tier = (data.get("environment_tier") or "").strip().lower()
    if tier not in ENV_TIERS:
        errors["environment_tier"] = (
            "Select the environment tier (dev, test, sit, uat, preprod, prod or dr)."
        )


def _validate_clone_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Clone provisions a NEW environment mirroring an existing provisioned one.
    Same create-field rules for the new environment (project, name, tier,
    classification — checked by _validate_create_fields; its stack + target are
    validated by the shared component rules), plus a valid provisioned source to
    copy from. A clone is a new billable environment, so it's charged + quota'd
    like a create."""
    _validate_create_fields(data, session, errors)
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        errors["source_reference"] = "Select the provisioned environment to clone."
        return
    source = session.scalar(select(Request).where(Request.reference == source_ref))
    if source is None:
        errors["source_reference"] = f"Unknown request '{source_ref}'."
    elif source.status != "provisioned":
        errors["source_reference"] = (
            f"{source_ref} is not provisioned (status: {source.status}); only a provisioned "
            "environment can be cloned.")
    elif (data.get("environment_name") or "").strip() and \
            (data.get("environment_name") or "").strip() == (source.environment_name or ""):
        errors["environment_name"] = "Give the clone a different name from the source environment."


def _validate_components(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Require at least one component, each a valid technology + size."""
    components = data.get("components") or []
    # Ignore fully-blank rows (a stray empty row shouldn't count).
    filled = [
        c for c in components
        if (c.get("technology_code") or "").strip() or (c.get("size") or "").strip()
    ]
    if not filled:
        errors["components"] = "Add at least one component (a technology and its size)."
        return

    for index, component in enumerate(filled):
        technology = (component.get("technology_code") or "").strip()
        size = (component.get("size") or "").strip()
        if not technology:
            errors[f"component_{index}_technology"] = "Select a technology for this component."
        else:
            tech = session.scalar(
                select(Technology).where(Technology.code == technology)
            )
            if tech is None:
                errors[f"component_{index}_technology"] = f"Unknown technology '{technology}'."
            elif tech.lifecycle_state == "eol":
                errors[f"component_{index}_technology"] = (
                    f"{tech.name} is end-of-life and can no longer be requested."
                )
        if size not in SIZES:
            errors[f"component_{index}_size"] = "Choose a size: small, medium or large."
