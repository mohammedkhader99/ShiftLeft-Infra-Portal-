"""Scheduled auto-shutdown for non-prod environments (F-FIN-06).

Models a business-hours schedule and **quantifies the monthly saving** from
powering non-prod environments down out-of-hours (compute stops; storage keeps
billing). Prod/DR is never shut down.

The saving view is read-only and always available. Actually pausing is opt-in
(`SHUTDOWN_ENABLED`, default off): at each off-hours ⇄ business-hours boundary the
background sweep drives the control plane's actuation (the same signed stop/start
a manual click uses) for non-prod **compute** — so the saving is enacted, not just
recorded. In mock mode this changes no real cloud; live + `OCI_ACTUATE_ENABLED`
really powers the VM. Prod and storage are never touched.
"""

import os
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.pricing import estimate_cost
from db.models import Request

CURRENCY = "AED"
NONPROD_TIERS = {"dev", "test", "sit", "uat", "preprod"}
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
HOURS_PER_WEEK = 168.0


def enabled() -> bool:
    """Whether the auto-pause sweep runs. Off by default — the saving view still works."""
    return os.getenv("SHUTDOWN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _parse_days(spec: str) -> set[int]:
    """Parse 'mon-fri' or 'mon,wed,fri' into weekday numbers (Mon=0 .. Sun=6)."""
    spec = (spec or "").strip().lower()
    if not spec:
        return set(range(7))
    days: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if "-" in token:
            a, b = (p.strip()[:3] for p in token.split("-", 1))
            if a in _WEEKDAYS and b in _WEEKDAYS:
                i = _WEEKDAYS[a]
                while True:
                    days.add(i)
                    if i == _WEEKDAYS[b]:
                        break
                    i = (i + 1) % 7
        elif token[:3] in _WEEKDAYS:
            days.add(_WEEKDAYS[token[:3]])
    return days or set(range(7))


def _parse_hm(spec: str, default: dtime) -> dtime:
    try:
        hh, mm = spec.strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        return default


def _days() -> set[int]:
    return _parse_days(os.getenv("SHUTDOWN_DAYS", "mon-fri"))


def _start() -> dtime:
    return _parse_hm(os.getenv("SHUTDOWN_START", "08:00"), dtime(8, 0))


def _end() -> dtime:
    return _parse_hm(os.getenv("SHUTDOWN_END", "20:00"), dtime(20, 0))


def schedule() -> dict:
    return {
        "days": os.getenv("SHUTDOWN_DAYS", "mon-fri"),
        "start": os.getenv("SHUTDOWN_START", "08:00"),
        "end": os.getenv("SHUTDOWN_END", "20:00"),
        "tz": os.getenv("SHUTDOWN_TZ", "UTC"),
    }


def is_off_hours(now: datetime | None = None) -> bool:
    """True if non-prod should be shut down right now (outside business hours)."""
    tz_name = os.getenv("SHUTDOWN_TZ", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    in_hours = local.weekday() in _days() and _start() <= local.time() <= _end()
    return not in_hours


def off_hours_fraction() -> float:
    """Fraction of a week spent off-hours — the share of compute cost that can be saved."""
    start, end = _start(), _end()
    hours_per_day = max(0.0, (end.hour + end.minute / 60) - (start.hour + start.minute / 60))
    business_hours = len(_days()) * hours_per_day
    return max(0.0, min(1.0, 1.0 - business_hours / HOURS_PER_WEEK))


def _compute_cost(session: Session, req: Request) -> float:
    components = [{"technology_code": c.technology_code, "size": c.size} for c in req.components]
    breakdown = estimate_cost(components, req.deployment_target, session, req.advanced_options or {})
    return float(breakdown["by_category"]["compute"])


def shutdown_savings(session: Session) -> dict:
    """Per non-prod provisioned environment: potential monthly saving = its compute
    cost x the off-hours fraction (storage keeps billing). Plus the estate total."""
    frac = off_hours_fraction()
    environments: list[dict] = []
    total = 0.0
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        tier = (req.environment_tier or "").strip().lower()
        if tier not in NONPROD_TIERS:
            continue
        compute = _compute_cost(session, req)
        saving = round(compute * frac, 2)
        if saving <= 0:
            continue
        total += saving
        environments.append({
            "reference": req.reference,
            "environment": req.environment_name or req.target_environment,
            "tier": tier,
            "compute_monthly": round(compute, 2),
            "monthly_saving": saving,
        })
    environments.sort(key=lambda e: e["monthly_saving"], reverse=True)
    return {"environments": environments, "total_saving": round(total, 2)}


def status(session: Session) -> dict:
    """Schedule + current state + quantified saving. `paused` per env is derived:
    the environment is powered down now iff the sweep is enabled and it's off-hours."""
    off = is_off_hours()
    on = enabled()
    savings = shutdown_savings(session)
    for env in savings["environments"]:
        env["paused"] = on and off
    return {
        "enabled": on,
        "schedule": schedule(),
        "off_hours_now": off,
        "off_hours_fraction": round(off_hours_fraction(), 3),
        "currency": CURRENCY,
        "total_saving": savings["total_saving"],
        "environment_count": len(savings["environments"]),
        "environments": savings["environments"],
        "note": ("Potential saving = each non-prod environment's compute cost x the off-hours "
                 "fraction (storage keeps billing). Enable SHUTDOWN_ENABLED to stop non-prod "
                 "compute out-of-hours and start it back in-hours (mock changes no real cloud)."),
    }
