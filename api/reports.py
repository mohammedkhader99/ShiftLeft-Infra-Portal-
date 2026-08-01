"""Report generation for scheduled subscriptions (F-RPT-11).

Thin wrappers over the existing report functions (forecast, anomalies) plus a
compact estate summary — no new reporting logic. A subscription's run is stored
(viewable in the portal) and optionally delivered to a webhook.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import anomalies as anomaly_detect
from api import forecast as forecast_mod
from db.models import Request

REPORT_KINDS = ("forecast", "anomalies", "estate")


def _estate_summary(session: Session) -> dict:
    """A compact estate snapshot: counts by status + committed monthly spend."""
    by_status: dict[str, int] = {}
    for status, count in session.execute(
        select(Request.status, func.count()).group_by(Request.status)
    ).all():
        by_status[status] = count
    provisioned = session.scalars(select(Request).where(Request.status == "provisioned")).all()
    monthly = sum(float(r.estimate.monthly) for r in provisioned if r.estimate)
    return {
        "report": "estate",
        "environments_provisioned": len(provisioned),
        "committed_monthly": round(monthly, 2),
        "currency": "AED",
        "by_status": by_status,
    }


def generate_report(session: Session, kind: str) -> dict:
    """Generate the report of `kind` (one of REPORT_KINDS)."""
    if kind == "forecast":
        return {"report": "forecast", **forecast_mod.spend_forecast(session)}
    if kind == "anomalies":
        return {"report": "anomalies", **anomaly_detect.detect_anomalies(session)}
    if kind == "estate":
        return _estate_summary(session)
    raise ValueError(f"unknown report kind: {kind}")
