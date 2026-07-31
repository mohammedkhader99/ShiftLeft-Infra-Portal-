"""Anomaly detection (F-FIN-09 cost + F-RPT-10 request).

Scans the estate and surfaces things worth a second look — spend that's out of
line, and request patterns that could signal misuse. Deterministic rules and
statistics over data we already have (requests, estimates, actuals, budgets); no
model, no external call.

Read-only and advisory: it **only flags signals** for a human to review. It
never blocks, acts on, or changes anything (consistent with the recommend-only
posture, ARCHITECTURE.md §7 / P3).
"""

import os
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ActualCost, Budget, Request

# In-flight + running spend (matches Showback's committed scope).
COMMITTED_STATUSES = ("provisioned", "submitted", "planned", "in-progress")
PIPELINE_STATUSES = ("submitted", "planned", "in-progress")
SENSITIVE = {"restricted", "confidential"}
PUBLIC_CLOUD = {"azure", "oci"}
HIGH_TIERS = {"prod", "dr"}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _monthly(req: Request) -> float:
    return float(req.estimate.monthly) if req.estimate else 0.0


def _anomaly(kind, type_, severity, subject, signal, reference=None, **detail) -> dict:
    return {
        "kind": kind,
        "type": type_,
        "severity": severity,
        "subject": subject,
        "reference": reference,
        "signal": signal,
        "detail": detail,
    }


# --- Cost anomalies (F-FIN-09) -----------------------------------------------

def _cost_outliers(session: Session) -> list[dict]:
    """Requests whose monthly cost sits far above the estate baseline."""
    reqs = [
        r for r in session.scalars(select(Request).where(Request.status.in_(COMMITTED_STATUSES)))
        if _monthly(r) > 0
    ]
    costs = [_monthly(r) for r in reqs]
    if len(costs) < 4:  # too few to establish a baseline
        return []
    mean = statistics.mean(costs)
    sd = statistics.pstdev(costs)
    if sd <= 0:
        return []
    sigma = _f("ANOMALY_COST_SIGMA", 2.0)
    floor = _f("ANOMALY_COST_FLOOR", 1000.0)
    threshold = mean + sigma * sd
    out = []
    for r in reqs:
        cost = _monthly(r)
        if cost >= threshold and cost >= floor:
            severity = "high" if cost >= mean + 3 * sd else "medium"
            out.append(_anomaly(
                "cost", "outlier", severity, r.reference,
                f"Monthly cost {cost:,.0f} AED is well above the estate average "
                f"({mean:,.0f} AED); review the sizing.",
                reference=r.reference, monthly=round(cost, 2),
                estate_mean=round(mean, 2), threshold=round(threshold, 2),
            ))
    return out


def _cost_overruns(session: Session) -> list[dict]:
    """Requests billed far above their approved estimate (reuses F-FIN-01)."""
    pct_threshold = _f("ANOMALY_OVERRUN_PCT", 25.0)
    out = []
    for actual in session.scalars(select(ActualCost)):
        req = session.scalar(select(Request).where(Request.reference == actual.reference))
        if req is None or req.estimate is None:
            continue
        estimate = float(req.estimate.monthly)
        billed = float(actual.billed_monthly)
        if estimate <= 0 or billed <= estimate:
            continue
        pct = (billed - estimate) / estimate * 100
        if pct >= pct_threshold:
            severity = "high" if pct >= 2 * pct_threshold else "medium"
            out.append(_anomaly(
                "cost", "overrun", severity, req.reference,
                f"Billed {billed:,.0f} AED/mo vs {estimate:,.0f} estimated "
                f"(+{pct:.0f}%).",
                reference=req.reference, billed=round(billed, 2),
                estimate=round(estimate, 2), variance_pct=round(pct, 1),
            ))
    return out


def _budget_breaches(session: Session) -> list[dict]:
    """Cost centres at or near their budget (reuses F-FIN-02)."""
    warn_pct = _f("BUDGET_WARN_PCT", 90.0)
    committed: dict[str, float] = {}
    for r in session.scalars(select(Request).where(Request.status.in_(COMMITTED_STATUSES))):
        if r.cost_centre_code:
            committed[r.cost_centre_code] = committed.get(r.cost_centre_code, 0.0) + _monthly(r)
    out = []
    for budget in session.scalars(select(Budget)):
        limit = float(budget.monthly_limit)
        if limit <= 0:
            continue
        current = committed.get(budget.cost_centre_code, 0.0)
        used = current / limit * 100
        if current >= limit:
            out.append(_anomaly(
                "cost", "budget", "high", budget.cost_centre_code,
                f"Committed spend {current:,.0f} AED is over the "
                f"{limit:,.0f} AED budget ({used:.0f}%).",
                current=round(current, 2), limit=round(limit, 2), used_pct=round(used, 1)))
        elif used >= warn_pct:
            out.append(_anomaly(
                "cost", "budget", "medium", budget.cost_centre_code,
                f"Committed spend {current:,.0f} AED is at {used:.0f}% of the "
                f"{limit:,.0f} AED budget.",
                current=round(current, 2), limit=round(limit, 2), used_pct=round(used, 1)))
    return out


# --- Request anomalies (F-RPT-10) --------------------------------------------

def _bursts(session: Session) -> list[dict]:
    """A requester raising an unusual number of requests in a short window."""
    hours = _i("ANOMALY_BURST_HOURS", 24)
    threshold = _i("ANOMALY_BURST_COUNT", 5)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    counts: dict[str, int] = {}
    for r in session.scalars(select(Request).where(Request.created_at >= cutoff)):
        if r.requester:
            counts[r.requester] = counts.get(r.requester, 0) + 1
    out = []
    for requester, n in counts.items():
        if n >= threshold:
            out.append(_anomaly(
                "request", "burst", "medium", requester,
                f"{requester} raised {n} requests in {hours}h — an unusual burst.",
                count=n, window_hours=hours))
    return out


def _sensitive_exposure(session: Session) -> list[dict]:
    """Restricted/confidential data heading to public cloud or a public endpoint."""
    out = []
    for r in session.scalars(
        select(Request).where(Request.status.in_(COMMITTED_STATUSES),
                              Request.request_type != "decommission")
    ):
        classification = (r.data_classification or "").strip().lower()
        if classification not in SENSITIVE:
            continue
        adv = r.advanced_options or {}
        reasons = []
        if (r.deployment_target or "").strip().lower() in PUBLIC_CLOUD:
            reasons.append(f"public cloud ({r.deployment_target})")
        if str(adv.get("network_type") or "").lower() == "public":
            reasons.append("public network")
        if adv.get("public_endpoint") is True:
            reasons.append("public endpoint")
        if reasons:
            out.append(_anomaly(
                "request", "exposure", "high", r.reference,
                f"{classification.title()} data with {', '.join(reasons)} — review the exposure.",
                reference=r.reference, classification=classification, reasons=reasons))
    return out


def _high_impact(session: Session) -> list[dict]:
    """In-flight requests worth a review before approval: xlarge or prod/DR."""
    out = []
    for r in session.scalars(select(Request).where(Request.status.in_(PIPELINE_STATUSES))):
        tier = (r.environment_tier or "").strip().lower()
        xlarge = [c.technology_code for c in r.components if (c.size or "").lower() == "xlarge"]
        flags = []
        if xlarge:
            flags.append("xlarge component(s)")
        if tier in HIGH_TIERS:
            flags.append(f"{tier} environment")
        if flags:
            out.append(_anomaly(
                "request", "high-impact", "low", r.reference,
                f"Awaiting approval with {', '.join(flags)} — worth a review.",
                reference=r.reference, tier=tier or None, xlarge=xlarge))
    return out


def detect_anomalies(session: Session) -> dict:
    """Run every detector and return the flagged anomalies, most-severe first."""
    anomalies: list[dict] = []
    for detector in (_cost_outliers, _cost_overruns, _budget_breaches,
                     _bursts, _sensitive_exposure, _high_impact):
        anomalies.extend(detector(session))
    anomalies.sort(key=lambda a: _SEVERITY_RANK.get(a["severity"], 9))
    by_severity = {s: sum(1 for a in anomalies if a["severity"] == s) for s in ("high", "medium", "low")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(anomalies),
        "by_severity": by_severity,
        "anomalies": anomalies,
    }
