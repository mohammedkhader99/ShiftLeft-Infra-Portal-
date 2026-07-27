"""Authoritative server-side validation for request submission (increment 1.3).

This is the single source of truth for the rules (ARCHITECTURE.md P2 — the
client's validation is UX only). Each failure returns a plain-language message
that states the rule and, where useful, the compliant option (F-UX-10).

Draft saving is deliberately lenient (partial data allowed); these rules apply
only when a request is *submitted*.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CostCentre, Environment, Project, Technology

REQUEST_TYPES = {"create", "add", "resize", "decommission"}
SIZES = {"small", "medium", "large"}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
DEPLOYMENT_TARGETS = {"onprem", "azure", "oci"}
# Simple naming standard for now (F-CAT-04 full engine comes later).
NAME_PATTERN = re.compile(r"^[a-z0-9-]{3,40}$")

# Request types that act on an existing environment rather than creating one.
TARGET_TYPES = {"add", "resize", "decommission"}
# Request types that must carry at least one technology component.
COMPONENT_TYPES = {"create", "add", "resize"}


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

    # Fields common to every request type.
    cost_centre = (data.get("cost_centre_code") or "").strip()
    if not cost_centre:
        errors["cost_centre_code"] = "Select a cost centre so the request can be charged back."
    elif session.scalar(select(CostCentre).where(CostCentre.code == cost_centre)) is None:
        errors["cost_centre_code"] = f"Unknown cost centre '{cost_centre}'."

    if request_type == "create":
        _validate_create_fields(data, session, errors)
    else:  # add | resize | decommission
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

    return errors


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
