"""Optimisation digest (F-FIN-12).

Per-owner rollup of concrete cost-saving opportunities across provisioned
environments, each with an estimated monthly saving computed by re-pricing
through the authoritative engine (api.pricing.estimate_cost).

The rules are deliberately **non-prod-focused** — prod/DR configuration is
intentional and left alone. Read-only and advisory (P3): it recommends only, it
never changes a request or provisioning.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.pricing import estimate_cost
from db.models import Request

CURRENCY = "AED"
NONPROD_TIERS = {"dev", "test", "sit", "uat", "preprod"}
DEV_TEST = {"dev", "test"}
SIZE_LADDER = ["small", "medium", "large", "xlarge"]
_OWNER_ATTRS = ("environment_owner", "application_owner", "technical_owner", "business_owner")


def _owner(req: Request) -> str:
    for attr in _OWNER_ATTRS:
        value = (getattr(req, attr, None) or "").strip()
        if value:
            return value
    return req.requester


def _monthly(components: list[dict], target: str | None, session: Session, advanced: dict) -> float:
    return float(estimate_cost(components, target, session, advanced or {})["totals"]["monthly"])


def _recommendations(session: Session, req: Request) -> list[dict]:
    """Savings recommendations for one non-prod environment, with re-priced savings."""
    tier = (req.environment_tier or "").strip().lower()
    target = req.deployment_target
    if tier not in NONPROD_TIERS or not target:
        return []  # prod/DR (or untargeted) — leave it

    components = [{"technology_code": c.technology_code, "size": c.size} for c in req.components]
    advanced = dict(req.advanced_options or {})
    recs: list[dict] = []

    # 1) Rightsize each large/xlarge component one step down.
    for comp in components:
        size = (comp.get("size") or "").strip().lower()
        if size not in ("large", "xlarge"):
            continue
        smaller = SIZE_LADDER[SIZE_LADDER.index(size) - 1]
        now = _monthly([comp], target, session, {})
        down = _monthly([{"technology_code": comp.get("technology_code"), "size": smaller}], target, session, {})
        saving = round(now - down, 2)
        if saving > 0:
            recs.append({
                "type": "rightsize",
                "detail": f"{comp.get('technology_code')} is {size} on a {tier} environment — {smaller} would likely suffice.",
                "monthly_saving": saving,
            })

    # Advanced-option downgrades: re-price the whole request with the option relaxed.
    full = _monthly(components, target, session, advanced)

    def _saving_relaxed(**changes) -> float:
        mod = dict(advanced)
        mod.update(changes)
        return round(full - _monthly(components, target, session, mod), 2)

    if advanced.get("high_availability"):
        saving = _saving_relaxed(high_availability=False)
        if saving > 0:
            recs.append({"type": "disable-ha",
                         "detail": f"High availability is on for a {tier} environment — usually not needed.",
                         "monthly_saving": saving})

    if advanced.get("monitoring_level") == "enhanced" and tier in DEV_TEST:
        saving = _saving_relaxed(monitoring_level="basic")
        if saving > 0:
            recs.append({"type": "downgrade-monitoring",
                         "detail": f"Enhanced monitoring on a {tier} environment — basic is usually enough.",
                         "monthly_saving": saving})

    if advanced.get("support_tier") == "premium":
        saving = _saving_relaxed(support_tier="business")
        if saving > 0:
            recs.append({"type": "downgrade-support",
                         "detail": f"Premium support on a {tier} environment — business tier is typical.",
                         "monthly_saving": saving})

    return recs


def optimisation_digest(session: Session, owner: str | None = None) -> dict:
    """Per-owner savings opportunities across provisioned environments. Optionally
    scoped to a single `owner`. Owners are ranked by total potential saving."""
    by_owner: dict[str, dict] = {}
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        recs = _recommendations(session, req)
        if not recs:
            continue
        who = _owner(req)
        if owner and who != owner:
            continue
        env_saving = round(sum(r["monthly_saving"] for r in recs), 2)
        entry = by_owner.setdefault(who, {"owner": who, "environments": [], "saving": 0.0})
        entry["environments"].append({
            "reference": req.reference,
            "environment": req.environment_name or req.target_environment,
            "tier": (req.environment_tier or "").strip().lower() or None,
            "recommendations": recs,
            "saving": env_saving,
        })
        entry["saving"] = round(entry["saving"] + env_saving, 2)

    owners = sorted(by_owner.values(), key=lambda o: o["saving"], reverse=True)
    for entry in owners:
        entry["environments"].sort(key=lambda e: e["saving"], reverse=True)
    return {
        "currency": CURRENCY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_saving": round(sum(o["saving"] for o in owners), 2),
        "environment_count": sum(len(o["environments"]) for o in owners),
        "owner_count": len(owners),
        "owners": owners,
    }
