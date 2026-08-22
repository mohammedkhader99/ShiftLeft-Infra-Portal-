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

from api import blueprint_capabilities, component_options
from db.models import (ENVIRONMENT_TIERS, Backup, Blueprint, CostCentre, Environment, Project,
                       ProvisionedResource, Request, Subsidiary, Technology)

REQUEST_TYPES = {"dns", "create", "add", "resize", "decommission", "refresh", "restore",
                 "clone", "sandbox", "temporary", "reduce", "dr",
                 # Asking the infrastructure team for a CAPABILITY — backup,
                 # centralised logging, monitoring — rather than for a component.
                 # These have no package, no archive and no cloud resource, so
                 # they carry no sizing and no price and never reach the build
                 # path: there is nothing there to fail at. What they do carry is
                 # the justification, the approval and the audit trail, which is
                 # the whole reason to raise them here rather than by email.
                 "platform-service"}
SIZES = {"small", "medium", "large", "xlarge"}
# Size ladder for the reduce-capacity guardrail (F-CAT): new size must rank below.
SIZE_ORDER = {"small": 0, "medium": 1, "large": 2, "xlarge": 3}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
SENSITIVE_CLASSIFICATIONS = {"restricted", "confidential"}  # a refresh must mask these
DEPLOYMENT_TARGETS = {"onprem", "azure", "oci", "aws", "gcp"}
# Environment tier ladder (increment 6.2, from the UX brief).
# The environment tiers a REQUEST may name. Single source of truth, mirroring
# db.models.ENVIRONMENT_TIERS — a test asserts the two stay in step.
#
# This became load-bearing on 16 Aug 2026: under one VCN per tier, a request's
# tier decides which network its infrastructure is built in. It was
# dev|test|sit|uat|preprod|prod|dr, and mapped onto the six the estate actually
# runs. sit and preprod had no equivalent and eight requests used them, so the
# owner folded them into the nearest tier rather than carrying two more networks:
#
#   dev -> Development   test -> Test    sit     -> Pre-Test
#   uat -> UAT           prod -> Production      preprod -> UAT
#
# dr is not here. It was proposed and deliberately deferred: disaster recovery
# generally implies a different REGION, which the network map has no dimension
# for. Re-adding it is a decision, not a tidy-up.
ENV_TIERS = set(ENVIRONMENT_TIERS)

# What a request written before that change becomes. Kept in code so a database
# restored from an older dump converges, rather than carrying tiers that map to
# no network and refuse every OKE request with a confusing reason.
LEGACY_ENV_TIERS: dict[str, str] = {
    "dev": "Development",
    "test": "Test",
    "sit": "Pre-Test",
    "uat": "UAT",
    "preprod": "UAT",
    "prod": "Production",
}
# Every tier except Production. Derived, so adding a tier cannot forget to
# classify it — the old hand-written list would silently have treated a new tier
# as production and refused sandboxes on it.
NONPROD_TIERS = {t for t in ENV_TIERS if t != "Production"}


def normalise_tier(value: str | None) -> str:
    """A submitted tier as one of ENVIRONMENT_TIERS, or "" if it is none of them.

    LIBERAL IN WHAT IT ACCEPTS. Saved drafts, API clients and 37 stored requests
    all carry the old lowercase spellings, and the vocabulary changed underneath
    them on 16 Aug 2026. Refusing those would break working integrations to
    enforce a rename, so an old spelling is translated rather than rejected —
    while what gets STORED is always canonical, because the tier now decides
    which VCN a request's infrastructure is built in.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    for tier in ENV_TIERS:
        if raw.lower() == tier.lower():
            return tier
    return LEGACY_ENV_TIERS.get(raw.lower(), "")
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
# Request types that provision a NEW environment (create + clone + the short-lived
# sandbox/temporary). They carry components, metadata, and are charged + quota'd.
CREATE_LIKE_TYPES = {"create", "clone", "sandbox", "temporary", "dr"}
# Request types that must carry at least one technology component.
COMPONENT_TYPES = {"create", "add", "resize", "clone", "sandbox", "temporary", "dr"}
# Request types that must carry the governance metadata (all but decommission,
# which inherits its context from the request it tears down).
METADATA_TYPES = {"create", "add", "resize", "clone", "sandbox", "temporary", "dr"}

# Request types that ACT ON an existing provisioned request rather than creating
# something new. Two of these in flight against the same target is a real
# problem, not a cosmetic one: two Jira tickets for one piece of work, two
# approvers spending time on it, and an orchestrator eventually asked to tear
# down the same resource twice — where the second attempt either fails
# confusingly or, worse, destroys something a later request has since rebuilt.
#
# Clone and DR are deliberately absent: they READ a source to build something
# new, so raising two is a legitimate thing to want.
SOURCE_ACTING_TYPES = {"decommission", "refresh", "restore", "reduce"}

# Types whose unit of work is a SUBSET of the stack, so two requests only clash
# when they name overlapping technologies. Refresh and restore act on the whole
# environment, so any second one against the same source clashes.
COMPONENT_SCOPED_TYPES = {"decommission", "reduce"}

# A request that could still act on its target. A `*-failed` request has stopped,
# and blocking on one forever would make retrying impossible; a draft is not a
# commitment to anything and would block the requester against themselves.
IN_FLIGHT_STATUSES = ("submitted", "approved", "planned", "in-progress")


def validate_submission(data: dict, session: Session) -> dict[str, str]:
    """Return {field: message} for every broken rule. Empty dict means valid.

    `data['components']` is a list of {technology_code, size} dicts.
    """
    errors: dict[str, str] = {}

    request_type = (data.get("request_type") or "").strip()
    if request_type not in REQUEST_TYPES:
        errors["request_type"] = ("Choose a request type (create, add, resize, decommission, "
                                  "refresh, restore, clone, sandbox or temporary).")
        # Without a valid type we can't check type-specific rules.
        return errors

    # Cost centre is required for every type except those that operate on an
    # existing provisioned request (they inherit its context).
    if request_type not in ("decommission", "refresh", "restore", "reduce", "dns"):
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
    elif request_type == "sandbox":
        _validate_shortlived_fields(data, session, errors, require_expiry=False)
    elif request_type == "temporary":
        _validate_shortlived_fields(data, session, errors, require_expiry=True)
    elif request_type == "dr":
        _validate_dr_fields(data, session, errors)
    elif request_type == "decommission":
        _validate_decommission_fields(data, session, errors)
    elif request_type == "refresh":
        _validate_refresh_fields(data, session, errors)
    elif request_type == "restore":
        _validate_restore_fields(data, session, errors)
    elif request_type == "reduce":
        _validate_reduce_fields(data, session, errors)
    elif request_type == "dns":
        _validate_dns_fields(data, session, errors)
    elif request_type == "platform-service":
        _validate_platform_service_fields(data, session, errors)
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

    # The environment name has to fit the resource names it will be composed
    # into, which depends on which technologies were chosen — so it runs after
    # the components are known, not inside the name-format check above.
    if request_type in COMPONENT_TYPES:
        _validate_environment_name_fits(data, errors)

    # Last, and driven off SOURCE_ACTING_TYPES rather than sitting inside any one
    # type's validator: a new request type that acts on an existing resource gets
    # this guard by adding itself to that set, instead of by someone remembering
    # to copy the check.
    _validate_not_already_in_flight(data, session, errors)

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
    # Which of the source's components are still RUNNING. After a partial
    # decommission some are already gone, and asking to tear one down a second
    # time would hand the orchestrator a workspace that no longer exists.
    live = _live_technologies(session, source)
    for component in selected:
        tech = (component.get("technology_code") or "").strip()
        if tech not in source_techs:
            errors["components"] = (
                f"'{tech}' is not part of {source_ref}; choose from its technology stack."
            )
            break
        if live is not None and tech not in live:
            errors["components"] = (
                f"'{tech}' has already been decommissioned from {source_ref}. "
                + (f"Still running: {', '.join(sorted(live))}."
                   if live else "Nothing is left to decommission.")
            )
            break


def _live_technologies(session: Session, source: Request) -> set[str] | None:
    """The source's technologies whose resource is still active, or None when
    nothing has been provisioned (so no opinion can be formed).

    Joins components to resources through the same blueprint mapping
    provisioning uses, because a component carries no lifecycle of its own.
    """
    rows = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == source.reference)
    ).all()
    if not rows:
        return None
    live_kinds = {r.kind for r in rows if r.lifecycle_state == "active"}
    target = (source.deployment_target or "").strip().lower()
    out: set[str] = set()
    for component in source.components:
        code = component.technology_code
        if not code:
            continue
        blueprint = session.scalar(
            select(Blueprint).where(
                Blueprint.technology_code == code,
                Blueprint.deployment_target == target,
                Blueprint.resource_kind != "",
            )
        )
        kind = blueprint.resource_kind if blueprint else None
        if (kind in live_kinds) if kind else bool(live_kinds):
            out.add(code)
    return out


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
        elif normalise_tier(target.environment_tier) not in NONPROD_TIERS:
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


def _validate_tier_has_a_network(data: dict, errors: dict[str, str]) -> None:
    """Refuse a component whose tier has no network, BEFORE anybody approves it.

    Some blueprints are built one VCN per environment tier, and a tier with no
    VCN mapped cannot be built at all. The orchestrator already refuses it — but
    at PLAN time, which is after the request has been priced, sent to Jira and
    approved. The requester then sees a retry loop with the reason buried in a
    status detail.

    That is the same expensive shape as the resource name that overran its limit
    and killed REQ-2026-0128 after approval: the portal knew the answer at form
    time and said nothing until it cost something.

    None means the technology takes no per-tier network and is unrestricted. An
    empty list means it needs one and none exists, which no tier can satisfy.
    """
    tier = normalise_tier(data.get("environment_tier"))
    for component in data.get("components") or []:
        code = (component.get("technology_code") or "").strip()
        if not code:
            continue
        mapped = blueprint_capabilities.network_tiers(code)
        if mapped is None:
            continue
        if not mapped:
            errors["components"] = (
                f"{code} is built in a network of its own per environment tier, "
                f"and no tier has one yet. Ask the network team to provision one "
                f"before requesting it.")
            return
        if tier and tier not in mapped:
            errors["environment_tier"] = (
                f"{code} cannot be built in the {tier} tier: no network has been "
                f"provisioned for it. These are built one VCN per tier, so it "
                f"will not be placed in another tier's network. Available: "
                f"{', '.join(sorted(mapped))} — or ask the network team to "
                f"provision a {tier} network.")
            return


def _validate_create_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    project = (data.get("project_code") or "").strip()
    if not project:
        errors["project_code"] = "Select the project this environment belongs to."
    else:
        row = session.scalar(select(Project).where(Project.code == project))
        if row is None:
            errors["project_code"] = f"Unknown project '{project}'."
        elif not row.active:
            # Disabling is a deliberate act by an administrator, so it refuses
            # here rather than merely hiding the project from the dropdown — the
            # form is a convenience, not the boundary.
            errors["project_code"] = (
                f"Project '{project}' is disabled and cannot take new requests. "
                "Ask a platform administrator to re-enable it."
            )

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

    _validate_tier_has_a_network(data, errors)

    tier = normalise_tier(data.get("environment_tier"))
    if not tier:
        errors["environment_tier"] = (
            "Select the environment tier ("
            + ", ".join(sorted(ENV_TIERS)) + ")."
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


def _validate_dr_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Disaster Recovery provisions a DR REPLICA of a provisioned environment
    (F-CAT): the create-field rules for the replica (its own name + DR target),
    the tier must be 'dr' (prod-class), and a valid provisioned source to protect."""
    _validate_create_fields(data, session, errors)
    # DR was deferred on 16 Aug 2026 — disaster recovery generally implies a
    # different REGION, which the per-tier network map has no dimension for. So
    # there is no DR tier to sit at, and saying "must be at the DR tier" would
    # send a requester looking for one that does not exist.
    errors["environment_tier"] = (
        "Disaster Recovery is not available: there is no DR tier. It was deferred "
        "because DR normally means a second region, which this platform does not "
        "yet model.")
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        errors["source_reference"] = "Select the environment to protect (the DR primary)."
        return
    source = session.scalar(select(Request).where(Request.reference == source_ref))
    if source is None:
        errors["source_reference"] = f"Unknown request '{source_ref}'."
    elif source.status != "provisioned":
        errors["source_reference"] = (
            f"{source_ref} is not provisioned (status: {source.status}); only a provisioned "
            "environment can have a DR replica.")


# A DNS label: letters, digits and hyphens, not starting or ending with a hyphen.
# Each dot-separated part is validated, so "app.egate" is fine but "-app" is not.
DNS_TYPES = {"A", "CNAME"}
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _validate_platform_service_fields(data: dict, session: Session,
                                      errors: dict[str, str]) -> None:
    """Asking the infrastructure team for a CAPABILITY, not for a component.

    "Backup & Recovery", "Centralised Logging", "Monitoring & Alerting" — these
    have no package, no archive and no cloud resource, so there is nothing to
    size, nothing to price and no build path to fail at. They used to sit in the
    component list beside NGINX, where a requester could select one, have it
    approved, and receive a work item instead of infrastructure; REQ-2026-0183
    spent a real machine discovering that `dnf install backup` finds nothing.

    What this DOES carry is the justification, the approval and the audit trail,
    which is the whole reason to raise it here rather than by email.
    """
    from db.models import TechnologyDelivery

    # The capability rides in the existing components relationship — one entry,
    # no size. Deliberately not a new column: there is no migration mechanism
    # here, so a column added to `request` would silently not exist on a database
    # that already has one, and every platform-service request would carry
    # nothing at all.
    components = data.get("components") or []
    code = ""
    if components:
        code = (components[0].get("technology_code") or "").strip()

    if not code:
        errors["components"] = (
            "Choose the capability you need — backup, centralised logging, "
            "monitoring, an API gateway, a service mesh or Kubernetes.")
        return
    if len(components) > 1:
        errors["components"] = (
            "Ask for one capability at a time. Each is scoped and delivered "
            "separately, and bundling them hides what was actually agreed.")
        return

    row = session.get(TechnologyDelivery, code)
    if row is None or row.delivery_model != "capability":
        # Named something that is not a capability. Says which door to use,
        # because a refusal that only says no gets worked around.
        errors["components"] = (
            f"'{code}' is not a platform service. If it is software or a cloud "
            f"resource, raise a normal create request for it instead — that "
            f"path sizes it, prices it and can build it.")
        return

    if not (data.get("business_justification") or "").strip():
        # The only substance this request type has. Without it the team receives
        # a component name and no idea what it is meant to achieve.
        errors["business_justification"] = (
            f"Say what {code} needs to do — what it should protect, cover or "
            f"watch, and how well. There is nothing else in this request for the "
            f"infrastructure team to work from.")


def _validate_dns_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """A DNS request (GAP-ANALYSIS step 5) names a PROVISIONED environment. The
    record points at that environment's own address unless an explicit value is
    given, so the name follows the thing it names."""
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        errors["source_reference"] = "Select the provisioned environment this name is for."
    else:
        source = session.scalar(select(Request).where(Request.reference == source_ref))
        if source is None:
            errors["source_reference"] = f"Unknown request '{source_ref}'."
        elif source.status != "provisioned":
            errors["source_reference"] = (
                f"{source_ref} is not provisioned (status: {source.status}); only a "
                "provisioned environment can be given a DNS name.")

    name = (data.get("dns_name") or "").strip().lower()
    if not name:
        errors["dns_name"] = "Enter the host name to create, e.g. 'egate-uat'."
    elif len(name) > 120:
        errors["dns_name"] = "That host name is too long (120 characters maximum)."
    elif not all(_DNS_LABEL.match(part) for part in name.split(".") if True):
        errors["dns_name"] = (
            "Use letters, digits and hyphens only, separated by dots — no leading or "
            "trailing hyphen (e.g. 'egate-uat' or 'api.egate').")

    rtype = (data.get("dns_type") or "A").strip().upper()
    if rtype not in DNS_TYPES:
        errors["dns_type"] = f"Choose a record type: {', '.join(sorted(DNS_TYPES))}."

    # An explicit value is optional (blank = point at the environment), but a
    # CNAME has nothing sensible to infer, so it must be given.
    value = (data.get("dns_value") or "").strip()
    if rtype == "CNAME" and not value:
        errors["dns_value"] = "A CNAME needs the target host name it points to."
    if value and len(value) > 255:
        errors["dns_value"] = "That target is too long (255 characters maximum)."


def _validate_reduce_fields(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Reduce capacity (F-CAT) scales chosen components of a PROVISIONED environment
    DOWN. The source must be provisioned; each chosen technology must be in its
    stack; and each new size must be strictly smaller than its current size."""
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        errors["source_reference"] = "Select the provisioned environment to reduce."
        return
    source = session.scalar(select(Request).where(Request.reference == source_ref))
    if source is None:
        errors["source_reference"] = f"Unknown request '{source_ref}'."
        return
    if source.status != "provisioned":
        errors["source_reference"] = (
            f"{source_ref} is not provisioned (status: {source.status}); only a provisioned "
            "environment can be reduced.")
        return

    current = {c.technology_code: (c.size or "").strip().lower()
               for c in source.components if c.technology_code}
    selected = [c for c in (data.get("components") or []) if (c.get("technology_code") or "").strip()]
    if not selected:
        errors["components"] = "Choose at least one component to scale down."
        return
    for comp in selected:
        tech = (comp.get("technology_code") or "").strip()
        new = (comp.get("size") or "").strip().lower()
        if tech not in current:
            errors["components"] = f"'{tech}' is not part of {source_ref}; choose from its stack."
            break
        if new not in SIZES:
            errors["components"] = f"Choose a valid smaller size for '{tech}'."
            break
        if SIZE_ORDER.get(new, 0) >= SIZE_ORDER.get(current[tech], 0):
            errors["components"] = (
                f"'{tech}' new size ({new}) must be smaller than its current size "
                f"({current[tech]}) — reduce capacity only scales down.")
            break


def _validate_shortlived_fields(data: dict, session: Session, errors: dict[str, str],
                                *, require_expiry: bool) -> None:
    """Sandbox / Temporary provision a short-lived NON-PROD environment (F-CAT).
    Same create-field rules, but the tier must be non-prod (never prod/DR); a
    temporary environment must additionally carry a future expiry date."""
    _validate_create_fields(data, session, errors)
    tier = normalise_tier(data.get("environment_tier"))
    if tier and tier not in NONPROD_TIERS:
        errors["environment_tier"] = (
            "A sandbox/temporary environment must be non-production."
        )
    if require_expiry:
        raw = data.get("expires_on")
        expiry: date | None = None
        if isinstance(raw, date):
            expiry = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                expiry = date.fromisoformat(raw.strip())
            except ValueError:
                errors["expires_on"] = "Enter a valid expiry date (YYYY-MM-DD)."
        if "expires_on" not in errors:
            if expiry is None:
                errors["expires_on"] = "Choose when this temporary environment should expire."
            elif expiry <= datetime.now(timezone.utc).date():
                errors["expires_on"] = "The expiry date must be in the future."


# The reference the orchestrator composes names from, shortened the way it
# shortens it: REQ-2026-0128 -> 26-0128. Seven characters, and it is what the
# environment name has to share its budget with.
_SHORT_REFERENCE_LENGTH = 7


def _validate_environment_name_fits(data: dict, errors: dict[str, str]) -> None:
    """Refuse an environment name that would be TRIMMED in the built resource.

    Resource names are composed as <environment>-<reference>-<suffix> and every
    module caps them (31 for a VM, 24 for OKE). The orchestrator now compresses
    an over-long name rather than failing, but it does that by trimming the
    environment name — so `visa-preprod-eu` becomes `visa-preprod` in OCI, and
    someone looking for their machine by name does not find it.

    Compressing the REFERENCE is lossless (the identifying digits survive), so
    that happens quietly. Trimming the ENVIRONMENT loses information, so it is
    refused here with the length that would work.

    Before any of this existed the name simply overran and Terraform refused it
    at PLAN time — after the requester had a Jira ticket approved for something
    that could never be built (REQ-2026-0128).
    """
    name = (data.get("environment_name") or "").strip()
    if not name:
        return  # its absence is reported by the type's own validator
    from api import blueprint_capabilities
    from api.component_options import _fetch_blueprints

    worst: tuple[int, str, str] | None = None
    for component in (data.get("components") or []):
        code = (component.get("technology_code") or "").strip()
        if not code:
            continue
        budget = blueprint_capabilities.name_budget(
            code, _SHORT_REFERENCE_LENGTH, _fetch_blueprints)
        if budget is None:
            continue  # this technology's blueprint states no limit
        allowed, suffix = budget
        if len(name) > allowed and (worst is None or allowed < worst[0]):
            worst = (allowed, code, suffix)

    if worst is not None:
        allowed, code, suffix = worst
        errors["environment_name"] = (
            f"'{name}' is {len(name)} characters, and {code} can only carry "
            f"{allowed} — its machine is named "
            f"<environment>-<request>-{suffix}, which the cloud limits. Longer "
            f"names get shortened, so the resource would not be findable by the "
            f"name you chose. Use {allowed} characters or fewer."
        )


def _validate_not_already_in_flight(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Refuse a second request to do the same thing to the same resource.

    Submitting a decommission twice produced two accepted requests and two Jira
    tickets for one teardown. Nothing downstream caught it: each request was
    individually valid, the source was provisioned for both, and the components
    belonged to it in both. The clash only exists BETWEEN requests, so this is
    the only layer that can see it.

    Scoped by technology for the types whose unit of work is part of a stack:
    decommissioning nginx while a separate request decommissions redis from the
    same environment is fine, and blocking it would be a nuisance. Refresh and
    restore act on the whole environment, so any overlap is a clash.
    """
    request_type = (data.get("request_type") or "").strip()
    if request_type not in SOURCE_ACTING_TYPES:
        return
    source_ref = (data.get("source_reference") or "").strip()
    if not source_ref:
        return  # a missing source is the type validator's error to report, not ours

    # Exclude the request being submitted: it is already a draft row in the
    # database, and a rule that blocks a request on account of itself is worse
    # than no rule.
    mine = (data.get("reference") or "").strip()
    others = session.scalars(
        select(Request).where(
            Request.source_reference == source_ref,
            Request.request_type == request_type,
            Request.status.in_(IN_FLIGHT_STATUSES),
            Request.reference != mine,
        ).order_by(Request.reference)
    ).all()
    if not others:
        return

    wanted = {(c.get("technology_code") or "").strip()
              for c in (data.get("components") or []) if c.get("technology_code")}
    for other in others:
        theirs = {c.technology_code for c in other.components if c.technology_code}
        overlap = sorted(wanted & theirs) if request_type in COMPONENT_SCOPED_TYPES else None
        if request_type in COMPONENT_SCOPED_TYPES and not overlap:
            continue  # a different part of the same stack — not a clash

        ticket = getattr(getattr(other, "approval", None), "jira_key", "") or ""
        awaiting = f" (Jira {ticket})" if ticket else ""
        what = f" for {', '.join(overlap)}" if overlap else ""
        errors["source_reference"] = (
            f"{other.reference} is already requesting {request_type} of "
            f"{source_ref}{what} and is awaiting approval{awaiting}. Wait for it "
            f"to finish, or cancel it, rather than raising a second one for the "
            f"same resource."
        )
        return


def _tech_targets(tech: Technology) -> list[str]:
    """The deployment targets a technology is available on (parsed from its CSV)."""
    return [t.strip() for t in (tech.targets or "").split(",") if t.strip()]


def _validate_components(data: dict, session: Session, errors: dict[str, str]) -> None:
    """Require at least one component, each a valid technology + size, and each
    available on the chosen deployment target (F-CAT)."""
    components = data.get("components") or []
    target = (data.get("deployment_target") or "").strip()
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
            elif target in DEPLOYMENT_TARGETS and target not in _tech_targets(tech):
                errors[f"component_{index}_technology"] = (
                    f"{tech.name} is not available on {target} — choose a technology offered there."
                )
        if size not in SIZES:
            errors[f"component_{index}_size"] = "Choose a size: small, medium or large."

        # The component detail fields (version / vCPU / memory / disk). Every
        # value must be one the SERVER offers for this technology on this target
        # — the form's dropdowns are a convenience, not the rule. A request
        # hand-crafted against the API asking for a shape nobody offers is
        # refused here, which is the only place it can be refused honestly.
        if technology:
            for field, message in component_options.validate(
                session, technology, target, component
            ).items():
                errors[f"component_{index}_{field}"] = message
