"""Cost estimation (increment 1.5; Azure live pricing added in 2.1).

Prices a request's components per deployment target (on-prem / Azure / OCI).
Discount-aware (F-FIN-04). On-prem and OCI price from the seeded rate cards;
Azure compute prices live from the public Retail Prices API when
AZURE_PRICING_MODE=live, falling back to the cached rate cards if Azure is
unreachable (§13). All figures are authoritative server-side (P2).

Returns one-time, monthly, and annual totals plus a per-component line-item
breakdown, in AED.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters import azure_pricing, oci_pricing
from api.adapters.azure_pricing import AzureUnavailable
from api.adapters.oci_pricing import OCIUnavailable
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
    # Catalog expansion (6.2): commercial DB licences.
    "oracle-db": "oracle-licence",
    "mssql": "mssql-licence",
}


def _rates(session: Session, kind: str) -> dict[str, float]:
    """Return {item: discounted rate} for one rate-card kind."""
    rows = session.scalars(select(RateCard).where(RateCard.kind == kind)).all()
    return {
        row.item: float(row.rate) * (1 - float(row.discount_pct) / 100) for row in rows
    }


def _discount(session: Session, kind: str) -> float:
    """Negotiated discount fraction (0-1) for a rate-card kind (F-FIN-04)."""
    row = session.scalar(select(RateCard).where(RateCard.kind == kind))
    return float(row.discount_pct) / 100 if row else 0.0


def _cloud_compute_cached(resource: dict, vcpu: int, memory_gb: int) -> float:
    """Decomposed cached cloud compute (per-hour rates x 730)."""
    return (
        vcpu * resource.get("vcpu-hour", 0)
        + memory_gb * resource.get("memory-gb-hour", 0)
    ) * HOURS_PER_MONTH


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

    # Azure live pricing (2.1): compute from the real API, everything else cached.
    azure_live = target == "azure" and azure_pricing.is_live()
    azure_discount = _discount(session, "cloud_azure") if target == "azure" else 0.0
    pricing_source = "azure-live" if azure_live else "mock"

    # OCI live pricing (2.2): decomposed rates map straight onto the cloud
    # formula, so swap in the live (discounted) rate dict for `resource`.
    if target == "oci" and oci_pricing.is_live():
        try:
            oci_discount = _discount(session, "cloud_oci")
            resource = {
                item: rate * (1 - oci_discount)
                for item, rate in oci_pricing.rates().items()
            }
            pricing_source = "oci-live"
        except OCIUnavailable:
            pricing_source = "oci-cached"  # keep the cached rate cards

    lines: list[dict] = []
    one_time_total = 0.0
    monthly_total = 0.0

    for comp in sizing["components"]:
        monthly = 0.0
        licence_monthly = 0.0
        one_time = 0.0
        if comp["resolved"] and known_target:
            if azure_live:
                storage_monthly = comp["storage_gb"] * resource.get("storage-gb-month", 0)
                try:
                    compute = azure_pricing.vm_monthly(comp["size"]) * (1 - azure_discount)
                except AzureUnavailable:
                    # Fall back to the cached rate cards, and flag it (§13).
                    compute = _cloud_compute_cached(resource, comp["vcpu"], comp["memory_gb"])
                    pricing_source = "azure-cached"
                monthly = compute + storage_monthly
            else:
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
        "pricing_source": pricing_source,
        "lines": lines,
        "totals": {
            "one_time": round(one_time_total, 2),
            "monthly": round(monthly_total, 2),
            "annual": round(monthly_total * 12, 2),
        },
    }
