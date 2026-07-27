"""Cost estimation (increment 1.5).

Prices a request's components against the seeded rate cards, per deployment
target (on-prem / Azure / OCI). Discount-aware (F-FIN-04). Mock pricing lives
in the rate_card table — no external calls. All figures are authoritative
server-side (P2); the browser only displays them.

Returns one-time, monthly, and annual totals plus a per-component line-item
breakdown, in AED.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.sizing import resolve_components
from db.models import RateCard

HOURS_PER_MONTH = 730  # standard cloud billing month
CURRENCY = "AED"

# Which rate_card.kind holds each target's resource rates.
TARGET_KIND = {"onprem": "onprem", "azure": "cloud_azure", "oci": "cloud_oci"}
DEPLOYMENT_TARGETS = set(TARGET_KIND)

# Which licence (if any) a technology carries. Mock config for now; a proper
# technology->licence link can replace this later without touching pricing.
TECHNOLOGY_LICENCE = {
    "postgres16": "postgres-licence",
    "win2019": "windows-licence",
}


def _rates(session: Session, kind: str) -> dict[str, float]:
    """Return {item: discounted rate} for one rate-card kind."""
    rows = session.scalars(select(RateCard).where(RateCard.kind == kind)).all()
    return {
        row.item: float(row.rate) * (1 - float(row.discount_pct) / 100) for row in rows
    }


def _component_monthly(target: str, resource: dict, vcpu: int, memory_gb: int, storage_gb: int) -> float:
    """Monthly resource cost for one component, per target's rate structure."""
    if target == "onprem":
        return (
            vcpu * resource.get("vcpu", 0)
            + memory_gb * resource.get("memory-gb", 0)
            + storage_gb * resource.get("storage-gb", 0)
        )
    # Cloud: compute billed per hour, storage per GB/month.
    return (
        vcpu * resource.get("vcpu-hour", 0) * HOURS_PER_MONTH
        + memory_gb * resource.get("memory-gb-hour", 0) * HOURS_PER_MONTH
        + storage_gb * resource.get("storage-gb-month", 0)
    )


def estimate_cost(components: list[dict], deployment_target: str, session: Session) -> dict:
    """Return a cost breakdown + one-time/monthly/annual totals for a target.

    If the target is unknown, totals are zero and each line is marked so.
    """
    target = (deployment_target or "").strip()
    sizing = resolve_components(components, session)

    known_target = target in DEPLOYMENT_TARGETS
    resource = _rates(session, TARGET_KIND[target]) if known_target else {}
    licences = _rates(session, "licence")
    setup_fee = resource.get("setup", 0.0) if target == "onprem" else 0.0

    lines: list[dict] = []
    one_time_total = 0.0
    monthly_total = 0.0

    for comp in sizing["components"]:
        monthly = 0.0
        licence_monthly = 0.0
        one_time = 0.0
        if comp["resolved"] and known_target:
            monthly = _component_monthly(
                target, resource, comp["vcpu"], comp["memory_gb"], comp["storage_gb"]
            )
            licence_item = TECHNOLOGY_LICENCE.get(comp["technology_code"])
            if licence_item:
                licence_monthly = licences.get(licence_item, 0.0)
            one_time = setup_fee

        component_monthly = monthly + licence_monthly
        one_time_total += one_time
        monthly_total += component_monthly
        lines.append(
            {
                "technology_name": comp["technology_name"],
                "size": comp["size"],
                "resolved": comp["resolved"] and known_target,
                "resource_monthly": round(monthly, 2),
                "licence_monthly": round(licence_monthly, 2),
                "one_time": round(one_time, 2),
                "monthly": round(component_monthly, 2),
            }
        )

    return {
        "currency": CURRENCY,
        "deployment_target": target or None,
        "known_target": known_target,
        "lines": lines,
        "totals": {
            "one_time": round(one_time_total, 2),
            "monthly": round(monthly_total, 2),
            "annual": round(monthly_total * 12, 2),
        },
    }
