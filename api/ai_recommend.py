"""AI cloud & sizing recommendation (E4 — AI Assistant, recommend-only).

Given a plain-English workload description, suggest a stack (catalogue
technologies + a size for each) and then price that stack across every cloud it
can actually run on, so the requester can see a like-for-like cost comparison and
pick the best-value target.

Two things make this safe and trustworthy:

* **The catalogue is authoritative.** Every technology the model (or the offline
  matcher) proposes is re-checked against the live catalogue and dropped if it
  isn't offered — exactly like the drafter's `_constrain`.
* **The prices are the portal's, never the model's.** The model only picks the
  stack; the cross-cloud comparison is computed here by `estimate_cost`, the same
  authoritative pricing the request form and cost API use. The model never quotes
  a number.

It **recommends only** — it never submits a request, sets a price, or provisions
anything. The requester reviews it, and can pre-fill the form with one click.

Mirrors the drafter/explainer/triage pattern: mock-first (deterministic keyword
matcher, always works offline) with a live Claude adapter behind AI_MODE=live.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

# Reuse the drafter's shared AI plumbing and keyword matchers so the two features
# behave identically (same model config, same catalogue loading, same aliases).
from api.ai_drafter import (
    AiUnavailable,
    _load_catalog,
    _match_size,
    _match_technologies,
    ai_mode,
    ai_model,
    anthropic_client,
)
from api.pricing import estimate_cost
from api.validation import DEPLOYMENT_TARGETS, SIZES
from db.models import Technology

import json
from common.doctrine import with_doctrine

# On-prem is a valid, priced target, but this feature is about choosing a *cloud*,
# so the comparison ranks the cloud targets. On-prem still appears so the user can
# see the managed-cloud premium (or saving) against running it themselves.
_CLOUD_TARGETS = ("azure", "oci", "aws", "gcp")


# --- Catalogue targets (which clouds each technology can run on) --------------

def _tech_targets(session: Session) -> dict[str, set[str]]:
    """code -> the set of deployment targets it's offered on (non-EOL only).

    A generic technology (e.g. postgres16) is offered everywhere; a managed cloud
    service (e.g. aws-rds) only on its own cloud. Mirrors the target-aware
    catalogue that submit validation enforces.
    """
    techs = session.scalars(
        select(Technology).where(Technology.lifecycle_state != "eol")
    ).all()
    out: dict[str, set[str]] = {}
    for t in techs:
        targets = {p.strip() for p in (t.targets or "").split(",") if p.strip()}
        out[t.code] = targets & DEPLOYMENT_TARGETS
    return out


def _eligible_targets(codes: list[str], tech_targets: dict[str, set[str]]) -> list[str]:
    """Targets where EVERY chosen technology is available — the whole stack must
    run on one cloud, so it's the intersection. Empty stack → all targets."""
    if not codes:
        return sorted(DEPLOYMENT_TARGETS)
    sets = [tech_targets.get(c, set()) for c in codes]
    common: set[str] = set.intersection(*sets) if sets else set()
    return sorted(common)


def _price_across(
    components: list[dict], targets: list[str], session: Session
) -> list[dict]:
    """Price the identical stack on each target with the authoritative estimator,
    cheapest first. Each row is what the request form would show for that target."""
    rows: list[dict] = []
    for target in targets:
        est = estimate_cost(components, target, session)
        if not est.get("known_target"):
            continue  # unpriced target — leave it out of the comparison
        rows.append({
            "target": target,
            "currency": est["currency"],
            "monthly": est["totals"]["monthly"],
            "one_time": est["totals"]["one_time"],
            "annual": est["totals"]["annual"],
            "pricing_source": est["pricing_source"],
        })
    rows.sort(key=lambda r: r["monthly"])
    return rows


# --- Mock recommender (deterministic keyword matching) -----------------------

def _recommend_mock(description: str, catalog: dict) -> dict:
    """Infer the stack from keywords, exactly like the drafter's offline path."""
    text = description.lower()
    codes = _match_technologies(text, catalog)
    size = _match_size(text)
    notes: list[str] = []
    if codes and size is None:
        notes.append("Assumed size 'medium' where none was stated.")
    components = [
        {"technology_code": code, "size": size or "medium"} for code in codes
    ]
    return {
        "components": components,
        "rationale": None,  # the deterministic path lets the price table speak
        "notes": notes,
    }


# --- Live recommender (Claude, catalogue-constrained) ------------------------

_SYSTEM = (
    "You recommend an infrastructure stack for an internal provisioning portal. "
    "From the user's plain-English workload description, choose the technologies "
    "(using ONLY codes from the supplied catalogue) and a size for each. Do NOT "
    "choose a deployment target and do NOT quote any prices — the portal computes "
    "the multi-cloud price comparison itself from the stack you choose. Write a "
    "one- to two-sentence rationale explaining the stack and sizing. You are only "
    "RECOMMENDING for a human to review — you never submit, price, or provision."
)


def _live_schema(catalog: dict) -> dict:
    tech_codes = [t["code"] for t in catalog["technologies"]] or [""]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["components", "rationale"],
        "properties": {
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
            "rationale": {"type": "string"},
        },
    }


def _recommend_live(description: str, catalog: dict) -> dict:
    """Ask Claude for a catalogue-constrained stack + rationale. Raises
    AiUnavailable (never crashes the request) on any SDK/key/API problem."""
    client = anthropic_client()
    catalog_json = json.dumps(catalog, indent=2)
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=2000,
            system=with_doctrine(_SYSTEM),
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _live_schema(catalog)},
            },
            messages=[{
                "role": "user",
                "content": f"Catalogue:\n{catalog_json}\n\nWorkload:\n{description.strip()}",
            }],
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK/transport/API error cleanly
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc

    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
    )
    if not text:
        raise AiUnavailable("The AI service returned an empty recommendation.")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiUnavailable("The AI service returned an unreadable recommendation.") from exc
    return {
        "components": parsed.get("components") or [],
        "rationale": (parsed.get("rationale") or "").strip() or None,
        "notes": ["Recommended by Claude — the portal computed the price comparison."],
    }


# --- Catalogue-constraining safety net (runs on mock AND live output) --------

def _constrain_components(raw: list[dict], catalog: dict) -> tuple[list[dict], list[str]]:
    """Drop anything not in the live catalogue (authoritative guard), so a
    recommended stack can never carry a technology the submit path would reject."""
    tech_codes = {t["code"] for t in catalog["technologies"]}
    components: list[dict] = []
    warnings: list[str] = []
    for comp in raw or []:
        code = (comp.get("technology_code") or "").strip()
        if code not in tech_codes:
            if code:
                warnings.append(f"Dropped '{code}' — not in the approved catalogue.")
            continue
        size = (comp.get("size") or "").strip().lower()
        components.append({
            "technology_code": code,
            "size": size if size in SIZES else "medium",
        })
    return components, warnings


def recommend(description: str, session: Session, classification: str | None = None) -> dict:
    """Recommend a cloud + sizing from a plain-English workload description.

    Returns {mode, components, rationale, eligible_targets, comparison,
    recommended_target, notes, warnings}. Everything is a suggestion for a human
    to review — nothing is saved, priced authoritatively-for-charge, or provisioned
    here. Every component is guaranteed to be in the approved catalogue, and every
    price is computed by the portal's own estimator, not by the model.
    """
    catalog = _load_catalog(session)
    tech_targets = _tech_targets(session)

    if ai_mode() == "live":
        raw = _recommend_live(description, catalog)
        mode = "live"
    else:
        raw = _recommend_mock(description, catalog)
        mode = "mock"

    components, warnings = _constrain_components(raw["components"], catalog)
    notes = list(raw.get("notes") or [])

    codes = [c["technology_code"] for c in components]
    eligible = _eligible_targets(codes, tech_targets)
    comparison = _price_across(components, eligible, session) if components else []

    # Best value = cheapest monthly among the clouds the stack can run on. On-prem
    # is priced too, but the recommendation is a *cloud*, so prefer a cloud target
    # when one is present; fall back to the overall cheapest otherwise.
    recommended_target = None
    if comparison:
        cloud_rows = [r for r in comparison if r["target"] in _CLOUD_TARGETS]
        recommended_target = (cloud_rows or comparison)[0]["target"]

    if not components:
        warnings.append("No catalogue technology matched — describe the workload's stack (e.g. 'a Postgres database and a Java API').")
    elif not eligible:
        warnings.append("The chosen technologies aren't all offered on any single cloud — split them or pick alternatives.")

    return {
        "mode": mode,
        "components": components,
        "rationale": raw.get("rationale"),
        "eligible_targets": eligible,
        "comparison": comparison,
        "recommended_target": recommended_target,
        "notes": notes,
        "warnings": warnings,
    }
