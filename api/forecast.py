"""Spend forecasting (F-RPT-05).

Projects the estate's monthly spend over the next N months from what's already
known — a deterministic pipeline-plus-estate projection, not a statistical model:

  today's run-rate (provisioned environments' monthly cost)
    + pipeline (in-flight requests that start billing when they provision)
    - expiring (non-prod environments whose TTL lapses within the horizon)

Read-only — it changes nothing, and the numbers reconcile with Showback's
`committed` scope (provisioned + submitted/planned/in-progress).
"""

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import ProvisionedResource, Request

CURRENCY = "AED"
# In-flight requests that will start billing when provisioned (matches Showback's
# committed scope minus provisioned). Decommissions reduce spend, so they're
# excluded from pipeline additions rather than wrongly inflating the forecast.
PIPELINE_STATUSES = ("submitted", "planned", "in-progress")
MAX_MONTHS = 36


def _month_index(target: date, base: date) -> int:
    """Whole months from `base` to `target` (may be negative)."""
    return (target.year - base.year) * 12 + (target.month - base.month)


def _month_label(base: date, offset: int) -> str:
    total = base.month - 1 + offset
    return f"{base.year + total // 12:04d}-{total % 12 + 1:02d}"


def _min_ttl_expiry(session: Session, reference: str) -> datetime | None:
    """Earliest TTL expiry among a request's active resources (mirrors
    api.main._min_active_ttl, kept here to avoid importing the app module)."""
    return session.scalar(
        select(func.min(ProvisionedResource.ttl_expiry)).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.ttl_expiry.is_not(None),
        )
    )


def _monthly(req: Request) -> float:
    return float(req.estimate.monthly) if req.estimate else 0.0


def spend_forecast(session: Session, months: int = 6) -> dict:
    """Return a month-by-month projected monthly (and annual) spend plus the
    headline totals and top drivers. Month 0 is today's run-rate."""
    months = max(1, min(int(months or 6), MAX_MONTHS))
    today = datetime.now(timezone.utc).date()

    # Base run-rate + non-prod environments expiring within the horizon.
    base = 0.0
    expiring: list[dict] = []
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        monthly = _monthly(req)
        base += monthly
        if monthly <= 0:
            continue
        expiry = _min_ttl_expiry(session, req.reference)
        if expiry is None:
            continue
        edate = expiry.date() if isinstance(expiry, datetime) else expiry
        mi = _month_index(edate, today)
        if mi <= months:  # this-month/overdue counts from month 1; beyond horizon skipped
            expiring.append({
                "reference": req.reference,
                "environment": req.environment_name or req.target_environment,
                "monthly": round(monthly, 2),
                "month": min(max(mi, 1), months),
            })

    # Pipeline: in-flight requests that will start billing (exclude decommissions).
    pipeline: list[dict] = []
    for req in session.scalars(
        select(Request).where(
            Request.status.in_(PIPELINE_STATUSES), Request.request_type != "decommission"
        )
    ):
        monthly = _monthly(req)
        if monthly <= 0:
            continue
        month = 1
        if req.required_delivery_date:
            month = min(max(_month_index(req.required_delivery_date, today), 1), months)
        pipeline.append({
            "reference": req.reference,
            "environment": req.environment_name or req.target_environment,
            "monthly": round(monthly, 2),
            "month": month,
        })

    # Month-by-month projection (month 0 = today's run-rate).
    series: list[dict] = []
    for m in range(0, months + 1):
        added = sum(p["monthly"] for p in pipeline if p["month"] <= m)
        removed = sum(e["monthly"] for e in expiring if e["month"] <= m)
        projected = base + added - removed
        series.append({
            "month": m,
            "label": _month_label(today, m),
            "projected_monthly": round(projected, 2),
            "projected_annual": round(projected * 12, 2),
            "added": round(added, 2),
            "removed": round(removed, 2),
        })

    return {
        "currency": CURRENCY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "horizon_months": months,
        "current_monthly": round(base, 2),
        "pipeline_monthly": round(sum(p["monthly"] for p in pipeline), 2),
        "expiring_monthly": round(sum(e["monthly"] for e in expiring), 2),
        "projected_monthly": series[-1]["projected_monthly"],
        "months": series,
        "drivers": {
            "pipeline": sorted(pipeline, key=lambda p: p["monthly"], reverse=True)[:5],
            "expiring": sorted(expiring, key=lambda e: e["monthly"], reverse=True)[:5],
        },
    }
