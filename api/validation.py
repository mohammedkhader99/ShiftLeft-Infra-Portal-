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

from db.models import CostCentre, Environment, Project, Request, Subsidiary, Technology

REQUEST_TYPES = {"create", "add", "resize", "decommission"}
SIZES = {"small", "medium", "large"}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
DEPLOYMENT_TARGETS = {"onprem", "azure", "oci"}
# Governance metadata value sets (increment 6.1, from the UX brief).
PRIORITIES = {"low", "medium", "high", "critical"}
CRITICALITIES = {"tier1", "tier2", "tier3", "tier4"}
MIN_JUSTIFICATION = 20
# Simple naming standard for now (F-CAT-04 full engine comes later).
NAME_PATTERN = re.compile(r"^[a-z0-9-]{3,40}$")

# Request types that add to / resize an existing seeded environment.
TARGET_TYPES = {"add", "resize"}
# Request types that must carry at least one technology component.
COMPONENT_TYPES = {"create", "add", "resize"}
# Request types that must carry the governance metadata (all but decommission,
# which inherits its context from the request it tears down).
METADATA_TYPES = {"create", "add", "resize"}


def validate_submission(data: dict, session: Session) -> dict[str, str]:
    """Return {field: message} for every broken rule. Empty dict means valid.

    `data['components']` is a list of {technology_code, size} dicts.
    """
    errors: dict[str, str] = {}

    request_type = (data.get("request_type") or "").strip()
    if request_type not in REQUEST_TYPES:
        errors["request_type"] = "Choose a request type (create, add, resize or decommission)."
        # Without a valid type we can't check type-specific rules.
        return errors

    # Cost centre is required for every type except decommission, which inherits
    # it from the provisioned request it tears down.
    if request_type != "decommission":
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
    elif request_type == "decommission":
        _validate_decommission_fields(data, session, errors)
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

    return errors


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
