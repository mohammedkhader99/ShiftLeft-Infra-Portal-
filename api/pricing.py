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

from api.adapters import aws_pricing, azure_pricing, gcp_pricing, oci_pricing
from api.adapters.aws_pricing import AWSUnavailable
from api.adapters.azure_pricing import AzureUnavailable
from api.adapters.gcp_pricing import GCPUnavailable
from api.adapters.oci_pricing import OCIUnavailable
from api.sizing import resolve_components
from db.models import RateCard

HOURS_PER_MONTH = 730  # standard cloud billing month
CURRENCY = "AED"

# Which rate_card.kind holds each target's resource rates.
TARGET_KIND = {"onprem": "onprem", "azure": "cloud_azure", "oci": "cloud_oci",
               "aws": "cloud_aws", "gcp": "cloud_gcp"}
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

# Advanced-option cost modifiers (6.5). Transparent, documented mock/demo
# constants; an unset option contributes nothing, so base totals are unchanged.
HA_COMPUTE_MULTIPLIER = 2.0                                # high_availability doubles compute
BACKUP_FACTOR = {"7": 0.10, "30": 0.30, "90": 0.60}       # x monthly storage cost, by retention days
MONITORING_MONTHLY = {"basic": 150.0, "enhanced": 500.0}  # flat monthly, by level
SUPPORT_PCT = {"business": 0.10, "premium": 0.20}         # x infra subtotal, by tier


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


def _component_parts(target: str, resource: dict, vcpu: int, memory_gb: int, storage_gb: int) -> tuple[float, float]:
    """(compute_monthly, storage_monthly) for one component, per target's rates.

    Splitting compute from storage lets the breakdown report a per-category
    cost (6.4); their sum equals the previous single resource figure, so totals
    are unchanged.
    """
    if target == "onprem":
        compute = vcpu * resource.get("vcpu", 0) + memory_gb * resource.get("memory-gb", 0)
        storage = storage_gb * resource.get("storage-gb", 0)
    else:
        # Cloud: compute billed per hour, storage per GB/month.
        compute = (
            vcpu * resource.get("vcpu-hour", 0) + memory_gb * resource.get("memory-gb-hour", 0)
        ) * HOURS_PER_MONTH
        storage = storage_gb * resource.get("storage-gb-month", 0)
    return compute, storage


def estimate_cost(
    components: list[dict],
    deployment_target: str,
    session: Session,
    advanced: dict | None = None,
) -> dict:
    """Return a cost breakdown + one-time/monthly/annual totals for a target.

    If the target is unknown, totals are zero and each line is marked so.
    `advanced` (6.5) may add cost: high_availability doubles compute, and
    backup_retention / monitoring_level / support_tier add their own lines.
    """
    target = (deployment_target or "").strip()
    advanced = advanced or {}
    ha = bool(advanced.get("high_availability"))
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

    # AWS live pricing (multi-cloud breadth): same decomposed formula — swap in the
    # live (discounted) rate dict. The live fetch is an extension point, so this
    # falls back to the cached seeded rate cards until it's wired.
    if target == "aws" and aws_pricing.is_live():
        try:
            aws_discount = _discount(session, "cloud_aws")
            resource = {
                item: rate * (1 - aws_discount)
                for item, rate in aws_pricing.rates().items()
            }
            pricing_source = "aws-live"
        except AWSUnavailable:
            pricing_source = "aws-cached"  # keep the cached rate cards

    # GCP live pricing (multi-cloud breadth): same decomposed formula, same
    # cache fallback as AWS/OCI. The live fetch is an extension point.
    if target == "gcp" and gcp_pricing.is_live():
        try:
            gcp_discount = _discount(session, "cloud_gcp")
            resource = {
                item: rate * (1 - gcp_discount)
                for item, rate in gcp_pricing.rates().items()
            }
            pricing_source = "gcp-live"
        except GCPUnavailable:
            pricing_source = "gcp-cached"  # keep the cached rate cards

    lines: list[dict] = []
    one_time_total = 0.0
    monthly_total = 0.0
    compute_total = 0.0
    storage_total = 0.0
    licence_total = 0.0

    for comp in sizing["components"]:
        compute_monthly = 0.0
        storage_monthly = 0.0
        licence_monthly = 0.0
        one_time = 0.0
        if comp["resolved"] and known_target:
            if azure_live:
                storage_monthly = comp["storage_gb"] * resource.get("storage-gb-month", 0)
                try:
                    compute_monthly = azure_pricing.vm_monthly(comp["size"]) * (1 - azure_discount)
                except AzureUnavailable:
                    # Fall back to the cached rate cards, and flag it (§13).
                    compute_monthly = _cloud_compute_cached(resource, comp["vcpu"], comp["memory_gb"])
                    pricing_source = "azure-cached"
            else:
                compute_monthly, storage_monthly = _component_parts(
                    target, resource, comp["vcpu"], comp["memory_gb"], comp["storage_gb"]
                )
            licence_item = TECHNOLOGY_LICENCE.get(comp["technology_code"])
            if licence_item:
                licence_monthly = licences.get(licence_item, 0.0)
            one_time = setup_fee

        if ha:
            compute_monthly *= HA_COMPUTE_MULTIPLIER  # redundant nodes (6.5)
        resource_monthly = compute_monthly + storage_monthly
        component_monthly = resource_monthly + licence_monthly
        one_time_total += one_time
        monthly_total += component_monthly
        compute_total += compute_monthly
        storage_total += storage_monthly
        licence_total += licence_monthly
        lines.append(
            {
                "technology_name": comp["technology_name"],
                "size": comp["size"],
                "resolved": comp["resolved"] and known_target,
                "compute_monthly": round(compute_monthly, 2),
                "storage_monthly": round(storage_monthly, 2),
                "resource_monthly": round(resource_monthly, 2),
                "licence_monthly": round(licence_monthly, 2),
                "one_time": round(one_time, 2),
                "monthly": round(component_monthly, 2),
            }
        )

    # Advanced-option add-ons (6.5), driven by the selected options. Each is 0
    # when its option is unset, so a plain request keeps its base total.
    backup_total = round(storage_total * BACKUP_FACTOR.get(str(advanced.get("backup_retention")), 0.0), 2)
    monitoring_total = MONITORING_MONTHLY.get(advanced.get("monitoring_level"), 0.0)
    support_base = compute_total + storage_total + licence_total + backup_total + monitoring_total
    support_total = round(support_base * SUPPORT_PCT.get(advanced.get("support_tier"), 0.0), 2)
    monthly_total += backup_total + monitoring_total + support_total

    return {
        "currency": CURRENCY,
        "deployment_target": target or None,
        "known_target": known_target,
        "pricing_source": pricing_source,
        "lines": lines,
        # Per-category subtotals. Compute/storage/licence come from the components
        # (6.4); backup/monitoring/support are advanced-option add-ons (6.5). The
        # six sum to the monthly total. Network stays usage-based (not estimated).
        "by_category": {
            "compute": round(compute_total, 2),
            "storage": round(storage_total, 2),
            "licence": round(licence_total, 2),
            "backup": backup_total,
            "monitoring": round(monitoring_total, 2),
            "support": support_total,
        },
        "totals": {
            "one_time": round(one_time_total, 2),
            "monthly": round(monthly_total, 2),
            "annual": round(monthly_total * 12, 2),
        },
    }
