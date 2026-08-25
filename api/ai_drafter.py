"""AI request drafting (F-RPT-06).

Turns a plain-English description ("a medium Postgres database for the eGate UAT
environment, on-prem, internal data") into a *draft* provisioning request that
pre-fills the guided-request form.

Authority boundary (ARCHITECTURE.md §7 + the CLAUDE.md hard rule): the AI
**recommends a draft only**. It never submits, approves, prices, or provisions.
The requester reviews and edits the draft, then submits through the normal path,
where validate_submission re-checks everything server-side (P2). Every catalog
value the drafter proposes is re-verified against the live catalog in
`_constrain` here, so the model can never introduce a technology, project, or
cost centre that doesn't exist — the AI stays inside the approved catalogue.

Two modes (AI_MODE), mirroring the codebase's mock/live pattern (pricing, Jira):
  - mock  (default): a deterministic keyword→catalog mapper. No network, no key —
           so it demos and tests offline.
  - live:  the Claude API (Anthropic SDK) with structured outputs constrained to
           the catalog. Needs ANTHROPIC_API_KEY in the API's environment,
           supplied via .env / a vault — never in code or git.
"""

import json
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.validation import (
    CLASSIFICATIONS,
    CRITICALITIES,
    DEPLOYMENT_TARGETS,
    ENV_TIERS,
    NAME_PATTERN,
    PRIORITIES,
    REQUEST_TYPES,
    SIZES,
)
from db.models import CostCentre, Project, Technology
from common.doctrine import with_doctrine


class AiUnavailable(RuntimeError):
    """Live drafting couldn't run (no key, SDK not installed, or the API failed).

    The endpoint turns this into a clear, plain-language message. Mock mode never
    raises it, so the offline path always works.
    """


def ai_mode() -> str:
    """'live' calls the Claude API; anything else (the default) uses the offline
    keyword drafter. Read via the runtime settings store so it can be toggled from
    the Admin console (DB override -> .env -> default)."""
    from api import settings  # local import avoids any import-order coupling
    return (settings.env("AI_MODE", "mock") or "mock").strip().lower()


def ai_model() -> str:
    """The Claude model used in live mode (configurable from the Admin console)."""
    from api import settings
    return (settings.env("AI_MODEL", "claude-opus-5") or "").strip() or "claude-opus-5"


def anthropic_client():
    """A configured Anthropic client, or AiUnavailable with a clear message if the
    SDK isn't installed or the key isn't set. Shared by the live AI features
    (drafting, cost explanation); anthropic is lazy-imported so mock mode and the
    test suite never need it."""
    try:
        import anthropic
    except ImportError as exc:
        raise AiUnavailable(
            "The 'anthropic' package isn't installed on the API. Rebuild the image "
            "with it, or set AI_MODE=mock."
        ) from exc
    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        raise AiUnavailable(
            "ANTHROPIC_API_KEY isn't set. Add it to the API environment "
            "(via .env or a vault), or set AI_MODE=mock."
        )
    return anthropic.Anthropic()


# --- The approved catalogue the draft must stay inside -----------------------

def _load_catalog(session: Session) -> dict:
    """Load the valid choices from the DB + the fixed option sets. End-of-life
    technologies are excluded — they can't be requested (mirrors validation)."""
    techs = session.scalars(select(Technology).order_by(Technology.name)).all()
    return {
        "technologies": [
            {"code": t.code, "name": t.name}
            for t in techs
            if t.lifecycle_state != "eol"
        ],
        "projects": [
            {"code": p.code, "name": p.name}
            for p in session.scalars(select(Project).order_by(Project.name)).all()
        ],
        "cost_centres": [
            {"code": c.code, "name": c.name}
            for c in session.scalars(select(CostCentre).order_by(CostCentre.name)).all()
        ],
        "sizes": sorted(SIZES),
        "environment_tiers": sorted(ENV_TIERS),
        "data_classifications": sorted(CLASSIFICATIONS),
        "deployment_targets": sorted(DEPLOYMENT_TARGETS),
        "priorities": sorted(PRIORITIES),
        "business_criticalities": sorted(CRITICALITIES),
    }


# --- Mock drafter (deterministic keyword mapping) ----------------------------

# Common ways people name each technology → its catalog code. Only codes that
# actually exist in the catalog are ever emitted (filtered in _match_technologies).
_TECH_ALIASES: dict[str, list[str]] = {
    "postgres16": ["postgres", "postgresql", "psql"],
    "oracle-db": ["oracle database", "oracle db", "oracle-db"],
    "mssql": ["sql server", "mssql", "sqlserver", "ms sql"],
    "mongodb": ["mongo", "mongodb", "documentdb"],
    "redis7": ["redis", "cache"],
    "kafka": ["kafka", "event stream", "message stream"],
    "rabbitmq": ["rabbit", "rabbitmq", "message queue", "amqp"],
    "elasticsearch": ["elasticsearch", "elastic search", "elk"],
    "opensearch": ["opensearch"],
    "java21": ["java", "jvm", "spring boot"],
    "dotnet8": ["dotnet", ".net", "asp.net"],
    "nodejs20": ["node.js", "nodejs", "node ", "express"],
    "python312": ["python", "django", "flask", "fastapi"],
    "nginx": ["nginx"],
    "apache": ["apache http", "httpd", "apache web"],
    "k8s": ["kubernetes", "k8s"],
    "openshift": ["openshift", "ocp"],
    "vault": ["hashicorp vault", "secrets manager", "secret store"],
    "keycloak": ["keycloak", "single sign-on", "sso", "identity provider"],
    "rhel9": ["rhel", "red hat", "redhat"],
    "win2019": ["windows server", "windows"],
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _match_technologies(text: str, catalog: dict) -> list[str]:
    """Catalog codes whose code or an alias appears in the text, in catalog order."""
    matched: list[str] = []
    for tech in catalog["technologies"]:
        code = tech["code"]
        phrases = _TECH_ALIASES.get(code, []) + [code]
        if any(phrase in text for phrase in phrases):
            matched.append(code)
    return matched


def _match_size(text: str) -> str | None:
    if re.search(r"\b(x-?large|extra[- ]large|xl)\b", text):
        return "xlarge"
    if re.search(r"\blarge\b", text):
        return "large"
    if re.search(r"\bsmall\b", text):
        return "small"
    if re.search(r"\bmedium\b", text):
        return "medium"
    return None


def _match_target(text: str) -> str | None:
    if re.search(r"\boci\b|oracle cloud", text):
        return "oci"
    if "azure" in text:
        return "azure"
    if re.search(r"on[- ]?prem", text) or "data centre" in text or "data center" in text:
        return "onprem"
    return None


# Tier phrasings → one of ENV_TIERS, checked in this order so the more specific
# pre-production wins before production.
#
# A requester still writes "SIT" or "pre-prod" in prose whatever the portal calls
# its tiers, so those phrasings are kept and land on the tier the owner folded
# them into (16 Aug 2026): SIT is an integration-testing stage like Pre-Test, and
# pre-production is a final gate like UAT.
#
# "Disaster recovery" deliberately matches NOTHING. DR was deferred, so there is
# no tier to draft into, and inventing one here would produce a draft that fails
# validation with a message about a tier the portal does not have.
_TIER_PATTERNS: list[tuple[str, str]] = [
    (r"pre[- ]?prod(uction)?", "UAT"),
    (r"\bprod(uction)?\b", "Production"),
    (r"\buat\b|user acceptance", "UAT"),
    (r"\bsit\b|system integration", "Pre-Test"),
    (r"\bqmg\b", "QMG"),
    (r"pre[- ]?test", "Pre-Test"),
    (r"\btest\b|\bqa\b", "Test"),
    (r"\bdev(elopment)?\b", "Development"),
]


def _match_tier(text: str) -> str | None:
    for pattern, tier in _TIER_PATTERNS:
        if re.search(pattern, text):
            return tier
    return None


def _match_classification(text: str) -> str | None:
    if re.search(r"\brestricted\b|top secret|highly sensitive", text):
        return "restricted"
    if re.search(r"\bconfidential\b|sensitive|\bpii\b|personal data", text):
        return "confidential"
    if re.search(r"\binternal\b", text):
        return "internal"
    if re.search(r"\bpublic\b", text):
        return "public"
    return None


def _match_priority(text: str) -> str | None:
    for level in ("critical", "high", "medium", "low"):
        if re.search(rf"\b{level}[- ]?priority\b|priority[:= ]+{level}\b", text):
            return level
    if re.search(r"\burgent\b|\basap\b|\bemergency\b", text):
        return "high"
    return None


def _match_criticality(text: str) -> str | None:
    m = re.search(r"\btier[- ]?([1-4])\b", text)
    if m:
        return f"tier{m.group(1)}"
    if "mission critical" in text:
        return "tier1"
    if "business critical" in text:
        return "tier2"
    return None


def _match_lookup(text: str, items: list[dict]) -> str | None:
    """A project / cost-centre code, matched by its code token or full name."""
    toks = _tokens(text)
    for item in items:
        if item["code"].lower() in toks:
            return item["code"]
    for item in items:
        if item["name"].lower() in text:
            return item["code"]
    return None


def _sanitize_name(raw: str | None) -> str | None:
    """Coerce a candidate to the environment-name standard, or None if it can't be."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (raw or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    return slug if NAME_PATTERN.match(slug) else None


def _match_env_name(text: str, project_code: str | None, tier: str | None) -> str | None:
    """Prefer an explicit '<name>-<tier>' mention; else derive from project+tier."""
    tiers = "|".join(sorted(ENV_TIERS))
    explicit = re.search(rf"\b([a-z][a-z0-9-]{{1,36}}-(?:{tiers}))\b", text.lower())
    if explicit:
        name = _sanitize_name(explicit.group(1))
        if name:
            return name
    if project_code and tier:
        return _sanitize_name(f"{project_code}-{tier}")
    return None


def _draft_mock(description: str, catalog: dict) -> tuple[dict, list[str]]:
    """Deterministic keyword→catalog draft. Returns (raw_draft, notes)."""
    text = description.lower()
    notes: list[str] = []

    techs = _match_technologies(text, catalog)
    size = _match_size(text)
    if techs and size is None:
        size = "medium"
        notes.append("Assumed size 'medium' (none stated) — adjust per component.")
    components = [{"technology_code": code, "size": size} for code in techs]

    tier = _match_tier(text)
    project = _match_lookup(text, catalog["projects"])

    draft = {
        "request_type": "create",
        "project_code": project,
        "cost_centre_code": _match_lookup(text, catalog["cost_centres"]),
        "deployment_target": _match_target(text),
        "environment_name": _match_env_name(text, project, tier),
        "environment_tier": tier,
        "data_classification": _match_classification(text),
        "priority": _match_priority(text),
        "business_criticality": _match_criticality(text),
        "business_justification": description.strip(),
        "components": components,
    }
    return draft, notes


# --- Live drafter (Claude API, structured outputs) ---------------------------

_SYSTEM = (
    "You draft infrastructure provisioning requests for an internal portal. Read "
    "the user's plain-English description and fill in a request DRAFT using ONLY "
    "values from the supplied catalog and the allowed option lists. Never invent a "
    "technology, project, cost centre, size, tier, or classification that is not "
    "offered — if something isn't stated or can't be matched to the catalog, leave "
    "that field as an empty string (and omit any component you cannot match). "
    "Write a one- to two-sentence business justification from the description. You "
    "are only RECOMMENDING a draft for a human to review, edit, and submit — you do "
    "not submit, approve, price, or provision anything."
)


def _live_schema(catalog: dict) -> dict:
    """A JSON schema that constrains the model's output to catalog values. Every
    optional field allows "" (meaning 'not determined'), which _constrain drops."""
    def enum(values: list[str]) -> dict:
        return {"type": "string", "enum": [*values, ""]}

    tech_codes = [t["code"] for t in catalog["technologies"]] or [""]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "request_type", "project_code", "cost_centre_code", "deployment_target",
            "environment_name", "environment_tier", "data_classification",
            "priority", "business_criticality", "business_justification", "components",
        ],
        "properties": {
            "request_type": {"type": "string", "enum": sorted(REQUEST_TYPES)},
            "project_code": enum([p["code"] for p in catalog["projects"]]),
            "cost_centre_code": enum([c["code"] for c in catalog["cost_centres"]]),
            "deployment_target": enum(sorted(DEPLOYMENT_TARGETS)),
            "environment_name": {"type": "string"},
            "environment_tier": enum(sorted(ENV_TIERS)),
            "data_classification": enum(sorted(CLASSIFICATIONS)),
            "priority": enum(sorted(PRIORITIES)),
            "business_criticality": enum(sorted(CRITICALITIES)),
            "business_justification": {"type": "string"},
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["technology_code", "size"],
                    "properties": {
                        "technology_code": {"type": "string", "enum": tech_codes},
                        "size": {"type": "string", "enum": sorted(SIZES)},
                    },
                },
            },
        },
    }


def _draft_live(description: str, catalog: dict) -> tuple[dict, list[str]]:
    """Call the Claude API for a catalog-constrained draft. Returns (raw_draft, notes).

    Raises AiUnavailable (never crashes the request) if the SDK isn't installed,
    the key is missing, or the API call fails.
    """
    client = anthropic_client()
    catalog_json = json.dumps(catalog, indent=2)
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=4000,
            system=with_doctrine(_SYSTEM),
            output_config={
                "effort": "low",  # structured extraction — keep it fast and cheap
                "format": {"type": "json_schema", "schema": _live_schema(catalog)},
            },
            messages=[{
                "role": "user",
                "content": f"Catalog:\n{catalog_json}\n\nDescription:\n{description.strip()}",
            }],
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK/transport/API error cleanly
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc

    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
    )
    if not text:
        raise AiUnavailable("The AI service returned an empty draft.")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiUnavailable("The AI service returned an unreadable draft.") from exc
    return parsed, ["Drafted by Claude — review every field before submitting."]


# --- Catalog-constraining safety net (runs on mock AND live output) ----------

def _constrain(draft: dict, catalog: dict, mode: str, notes: list[str]) -> dict:
    """Re-check every proposed value against the live catalog. Anything not
    offered is dropped (with a warning), so a draft can never carry a value the
    submit path would reject. This is the authoritative guard — the model's
    output is never trusted directly."""
    tech_codes = {t["code"] for t in catalog["technologies"]}
    project_codes = {p["code"] for p in catalog["projects"]}
    cc_codes = {c["code"] for c in catalog["cost_centres"]}
    warnings: list[str] = []

    def keep(value, allowed: set[str]) -> str | None:
        """Keep a drafted value only if the portal actually accepts it.

        Lower-cases first because most of these vocabularies are lowercase, then
        falls back to a case-insensitive match — environment tiers are capitalised
        (Development, UAT, Pre-Test), and lowercasing alone silently dropped every
        tier the drafter inferred, leaving the field blank on every AI draft.
        """
        if not isinstance(value, str):
            return value if value in allowed else None
        lowered = value.strip().lower()
        if lowered in allowed:
            return lowered
        return next((a for a in allowed if a.lower() == lowered), None)

    request_type = (draft.get("request_type") or "create").strip().lower()
    if request_type not in REQUEST_TYPES:
        request_type = "create"

    components: list[dict] = []
    for comp in draft.get("components") or []:
        code = (comp.get("technology_code") or "").strip()
        if code not in tech_codes:
            if code:
                warnings.append(f"Dropped '{code}' — not in the approved catalogue.")
            continue
        size = (comp.get("size") or "").strip().lower()
        components.append({
            "technology_code": code,
            "size": size if size in SIZES else None,
        })

    project = keep(draft.get("project_code"), {c.lower() for c in project_codes})
    # keep() lowercased; recover the catalogue's canonical casing.
    project = next((c for c in project_codes if c.lower() == project), None) if project else None
    if (draft.get("project_code") or "").strip() and project is None:
        warnings.append(f"Dropped project '{draft['project_code']}' — not a known project.")

    cost_centre = keep(draft.get("cost_centre_code"), {c.lower() for c in cc_codes})
    cost_centre = next((c for c in cc_codes if c.lower() == cost_centre), None) if cost_centre else None
    if (draft.get("cost_centre_code") or "").strip() and cost_centre is None:
        warnings.append(f"Dropped cost centre '{draft['cost_centre_code']}' — not a known cost centre.")

    clean = {
        "request_type": request_type,
        "project_code": project,
        "cost_centre_code": cost_centre,
        "deployment_target": keep(draft.get("deployment_target"), DEPLOYMENT_TARGETS),
        "environment_name": _sanitize_name(draft.get("environment_name")),
        "environment_tier": keep(draft.get("environment_tier"), ENV_TIERS),
        "data_classification": keep(draft.get("data_classification"), CLASSIFICATIONS),
        "priority": keep(draft.get("priority"), PRIORITIES),
        "business_criticality": keep(draft.get("business_criticality"), CRITICALITIES),
        "business_justification": (draft.get("business_justification") or "").strip() or None,
        "components": components,
    }

    # Gentle nudges for the fields a create still needs before it can be submitted.
    if not clean["components"]:
        warnings.append("No catalogue technology matched — add at least one component.")
    if not clean["deployment_target"]:
        warnings.append("No deployment target detected — choose on-prem, Azure or OCI.")
    if not clean["cost_centre_code"]:
        warnings.append("No cost centre detected — select one so it can be charged back.")
    if clean["request_type"] == "create" and not clean["project_code"]:
        warnings.append("No project detected — select the owning project.")

    return {"mode": mode, "draft": clean, "notes": notes, "warnings": warnings}


def draft_request(description: str, session: Session) -> dict:
    """Draft a provisioning request from a plain-English description.

    Returns {mode, draft, notes, warnings}. `draft` is a suggestion only — it is
    NOT saved or submitted here; the caller returns it for the requester to
    review. Every value in it is guaranteed to be in the approved catalogue.
    """
    catalog = _load_catalog(session)
    if ai_mode() == "live":
        raw, notes = _draft_live(description, catalog)
        mode = "live"
    else:
        raw, notes = _draft_mock(description, catalog)
        mode = "mock"
    return _constrain(raw, catalog, mode, notes)
