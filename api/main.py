import json
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import urllib.parse

import anyio
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi import Request as HTTPRequest
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, computed_field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import ai_drafter
from api import ai_explainer
from api import ai_triage
from api import anomalies as anomaly_detect
from api import apikeys
from api import chatbot
from api import eventstream
from api import forecast
from api import optimisation as optim
from api import shutdown as autoshutdown
from api import sustainability as sustainability_mod
from api import vault
from api import webhooks
from api.tracing import current_trace_id, install_tracing, new_trace_id, set_trace_id
from api.attachment import build_request_pdf
from api.costsheet import build_cost_sheet_xlsx
from api.evidence import build_evidence_pdf
from api.audit import append_audit, verify_chain
from api.auth import get_requester, get_requester_name
from api.jira import (
    JiraError,
    add_comment,
    build_ticket_body,
    create_issue,
    get_approval_author,
    get_approvers,
    get_status,
    inprogress_status,
    jira_mode,
    resolve_fields,
    resolved_status,
    sync_subsidiaries,
    transition_issue,
)
from api.plan_preview import build_plan_preview
from api.policy import PolicyUnavailable, get_policy_evaluator
from api.pricing import estimate_cost
from api import roles as roles_mod
from api.sizing import resolve_components
from api.validation import validate_submission
from common.security import install_rate_limit, install_security_headers
from common.signing import sign
from db.models import (
    AccessGrant,
    ActualCost,
    ApiKey,
    Approval,
    AuditLog,
    Backup,
    Budget,
    CostCentre,
    Quota,
    Environment,
    Estimate,
    Project,
    ProvisionedResource,
    Request,
    RequestComponent,
    SizingAnchor,
    Subsidiary,
    Technology,
    WebhookDelivery,
    WebhookSubscription,
)
from db.session import SessionLocal

load_dotenv()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the background workers on startup, stop them on shutdown.

    The functions are defined further down. The Jira poller runs only when
    AUTO_PROVISION is on; the subsidiary sync runs only against live Jira. So
    nothing runs by default (mock mode / dev / tests).
    """
    _start_poller()
    _start_subsync()
    try:
        yield
    finally:
        _stop_poller()
        _stop_subsync()


app = FastAPI(title="Infra Portal API", lifespan=_lifespan)

# Application hardening (F-SEC-09): strict headers (JSON-only, so lock everything
# down) + a per-client rate limit. The API sits behind the BFF; these are defence
# in depth. SSE + health are exempt from the rate limit.
install_security_headers(app, csp="default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
install_rate_limit(app, exempt_prefixes=("/health", "/api/events/stream"))
# Distributed tracing (F-OPS-04): installed last, so it is the OUTERMOST middleware
# — it sets the request's trace id before anything else runs (so every audit entry
# is stamped) and echoes X-Trace-Id on the way out.
install_tracing(app)

# No login yet (real identity arrives in increment E1); stamp a fixed requester.
MOCK_REQUESTER = "mohammed.khader@emaratechg.ae"
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:9091")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-mock-secret")
# Versioned contract to the orchestrator (2.5).
CONTRACT_VERSION = "1.0"
ORCH_MAX_ATTEMPTS = 3
# Real provisioning (terraform plan/apply) can take a while, so the handoff
# waits longer than a normal API call.
ORCH_TIMEOUT = 300.0
# Sandbox resources get a short time-to-live (F-FIN-07 foundation).
PROVISION_TTL_DAYS = int(os.getenv("PROVISION_TTL_DAYS", "7"))

# Environment TTL & renewal (E3.1, F-FIN-07). Only known non-prod tiers auto-
# expire; prod/dr and unknown-tier requests (e.g. add/resize on an existing
# environment) never expire, so production is never wrongly reclaimed.
TTL_NONPROD_TIERS = {"dev", "test", "sit", "uat", "preprod"}


def _ttl_days_nonprod() -> int:
    try:
        return max(1, int(os.getenv("TTL_DAYS_NONPROD", "30")))
    except (ValueError, TypeError):
        return 30


def _ttl_warn_days() -> int:
    try:
        return max(0, int(os.getenv("TTL_WARN_DAYS", "7")))
    except (ValueError, TypeError):
        return 7


def _ttl_enforce() -> bool:
    """Whether an expired non-prod environment is auto-decommissioned. Off by
    default: expiry only warns/flags until an operator turns enforcement on."""
    return os.getenv("TTL_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _ttl_for(req: Request) -> datetime | None:
    """Expiry for a newly provisioned environment (F-FIN-07): a lifetime for a
    known non-prod tier, None (never expires) for prod/dr or unknown tier."""
    tier = (req.environment_tier or "").strip().lower()
    if tier in TTL_NONPROD_TIERS:
        return datetime.now(timezone.utc) + timedelta(days=_ttl_days_nonprod())
    return None


def _variance_alert_pct() -> float:
    try:
        return max(0.0, float(os.getenv("VARIANCE_ALERT_PCT", "15")))
    except (ValueError, TypeError):
        return 15.0


def _variance_status(estimate_monthly, actual_monthly) -> dict | None:
    """Actual-vs-estimate variance (F-FIN-01), computed server-side. None unless
    both an estimate and a recorded actual exist. status: over / under / on-track
    against VARIANCE_ALERT_PCT."""
    if estimate_monthly is None or actual_monthly is None:
        return None
    est, act = float(estimate_monthly), float(actual_monthly)
    variance = round(act - est, 2)
    pct = round(variance / est * 100.0, 1) if est else 0.0
    threshold = _variance_alert_pct()
    if pct > threshold:
        status = "over"
    elif pct < -threshold:
        status = "under"
    else:
        status = "on-track"
    return {"estimate": round(est, 2), "actual": round(act, 2),
            "variance": variance, "variance_pct": pct, "status": status}


def _health_for(session: Session, req: Request, actual=None) -> dict | None:
    """Environment health score (F-LCM-08): 0-100 + grade A-E, from governance and
    ops signals. Provisioned environments only (else None). Computed server-side.
    Signals that don't apply (no recorded actual, no scan yet) simply don't deduct.
    `actual` may be passed in to avoid a per-row query when scoring a list."""
    if req.status != "provisioned":
        return None
    score = 100
    factors: list[dict] = []

    def ding(points: int, signal: str, detail: str) -> None:
        nonlocal score
        score -= points
        factors.append({"signal": signal, "impact": -points, "detail": detail})

    ttl = _ttl_status(_min_active_ttl(session, req.reference))
    if ttl and ttl["status"] == "expired":
        ding(25, "ttl-expired", "environment is past its TTL")
    elif ttl and ttl["status"] == "expiring":
        ding(5, "ttl-expiring", f"expires in {ttl['days_left']} day(s)")

    if _is_orphan(req):
        ding(20, "orphaned", "no resolvable owner")

    scan = session.scalar(
        select(AuditLog).where(AuditLog.reference == req.reference,
                               AuditLog.event == "scan.findings").order_by(AuditLog.id.desc()))
    counts = (scan.detail or {}).get("counts", {}) if scan else {}
    if counts.get("high"):
        ding(20, "iac-high", f"{counts['high']} high-severity IaC finding(s)")
    elif counts.get("medium"):
        ding(8, "iac-medium", f"{counts['medium']} medium-severity IaC finding(s)")

    if actual is None:
        actual = session.scalar(
            select(ActualCost.billed_monthly).where(ActualCost.reference == req.reference))
    variance = _variance_status(req.estimate.monthly if req.estimate else None, actual)
    if variance and variance["status"] == "over":
        ding(10, "cost-over", f"billed {variance['variance_pct']}% over estimate")

    adv = req.advanced_options or {}
    if str(adv.get("backup_retention") or "none") == "none":
        ding(10, "no-backup", "no backup retention configured")
    if str(adv.get("monitoring_level") or "none") in ("none", "basic"):
        ding(5, "weak-monitoring", "monitoring is none or basic")
    if not (req.application_owner or req.technical_owner):
        ding(5, "no-owner-named", "no application/technical owner named")

    score = max(0, score)
    grade = ("A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60
             else "D" if score >= 40 else "E")
    return {"score": score, "grade": grade, "factors": factors}


def _ttl_status(expiry: datetime | None) -> dict | None:
    """TTL state for the portal (F-FIN-07), computed server-side: ok / expiring /
    expired plus days remaining. None when the environment has no expiry."""
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expiry <= now:
        state = "expired"
    elif expiry <= now + timedelta(days=_ttl_warn_days()):
        state = "expiring"
    else:
        state = "ok"
    return {"expiry": expiry.isoformat(),
            "days_left": (expiry - now).days,
            "status": state}


def _post_to_orchestrator(body: bytes, signature: str, path: str = "/provision"):
    """POST the signed handoff to an orchestrator path, retrying transient failures.

    Returns (response, error). error is None on success; otherwise a string.
    Safe to retry because the handoff is idempotent (keyed on the request).
    """
    error = None
    for attempt in range(ORCH_MAX_ATTEMPTS):
        try:
            _headers = {"X-Signature": signature, "Content-Type": "application/json"}
            _tid = current_trace_id()
            if _tid:  # propagate the trace across the hop (F-OPS-04)
                _headers["X-Trace-Id"] = _tid
            response = httpx.post(
                f"{ORCHESTRATOR_URL}{path}",
                content=body,
                headers=_headers,
                timeout=ORCH_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — transient network failure
            error = f"unreachable: {exc}"
        else:
            if response.status_code < 500:
                return response, None
            error = f"orchestrator {response.status_code}"
        if attempt < ORCH_MAX_ATTEMPTS - 1:
            time.sleep(0.2 * (2 ** attempt))  # exponential backoff
    return None, error


def is_mock_mode() -> bool:
    return os.getenv("USE_MOCK", "true").strip().lower() == "true"


def get_session() -> Iterator[Session]:
    """Hand out a database session for the life of one request (read-only use here)."""
    with SessionLocal() as session:
        yield session


def _authed_requester(
    session: Session = Depends(get_session),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_requester: str | None = Header(default=None),
) -> str:
    """The authenticated requester (F-INT-01). A valid X-API-Key authenticates as
    the identity the key was issued for; otherwise the normal Entra/mock auth,
    byte-for-byte unchanged. An unknown or revoked key is refused."""
    if x_api_key:
        identity = apikeys.resolve_api_key(session, x_api_key)
        if identity is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key.")
        return identity
    return get_requester(authorization=authorization, x_requester=x_requester)


def require_action(action: str):
    """FastAPI dependency: authorise the signed-in user for a guarded action.

    Resolves the user's roles server-side (F-IAM-01) and refuses with 403 if none
    of them permit the action. Returns the requester so endpoints can also use it.
    """
    def _dep(requester: str = Depends(_authed_requester)) -> str:
        user_roles = roles_mod.resolve_roles(requester)
        if not roles_mod.can(user_roles, action):
            have = ", ".join(sorted(user_roles)) or "none"
            raise HTTPException(
                status_code=403,
                detail=(f"Your role ({have}) is not permitted to {action.replace('_', ' ')}. "
                        "Ask a platform administrator."),
            )
        return requester
    return _dep


@app.get("/api/me")
def whoami(requester: str = Depends(_authed_requester)) -> dict:
    """The signed-in user's identity and resolved roles (for the portal to
    show the role and hide actions it can't take — the API stays the gate)."""
    return {"email": requester, "roles": sorted(roles_mod.resolve_roles(requester))}


# --- API keys / programmatic access (E1, F-INT-01) ---------------------------

class ApiKeyIn(BaseModel):
    label: str = "api key"

    @field_validator("label")
    @classmethod
    def _clean(cls, v: str) -> str:
        return (v or "").strip()[:120] or "api key"


@app.post("/api/api-keys")
def create_api_key(body: ApiKeyIn, requester: str = Depends(_authed_requester),
                   session: Session = Depends(get_session)) -> dict:
    """Issue an API key bound to the caller (F-INT-01). The plaintext key is
    returned ONCE — only its hash is stored — and it acts as the caller, so it
    can never exceed the caller's roles."""
    key = apikeys.generate_key()
    row = ApiKey(key_hash=apikeys.hash_key(key), identity=requester, label=body.label)
    session.add(row)
    append_audit(session, "apikey.created", actor=requester, detail={"label": row.label})
    session.commit()
    return {"id": row.id, "label": row.label, "identity": requester, "key": key,
            "note": "Store this key now — it will not be shown again."}


@app.get("/api/api-keys")
def list_api_keys(requester: str = Depends(_authed_requester),
                  session: Session = Depends(get_session)) -> dict:
    """The caller's own API keys — never the secret (F-INT-01)."""
    rows = session.scalars(
        select(ApiKey).where(ApiKey.identity == requester).order_by(ApiKey.id.desc()))
    return {"keys": [{"id": r.id, "label": r.label, "identity": r.identity, "active": r.active,
                      "created_at": r.created_at.isoformat() if r.created_at else None,
                      "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None}
                     for r in rows]}


@app.delete("/api/api-keys/{key_id}")
def revoke_api_key(key_id: int, requester: str = Depends(_authed_requester),
                   session: Session = Depends(get_session)) -> dict:
    """Revoke one of the caller's API keys (F-INT-01)."""
    row = session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.identity == requester))
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    row.active = False
    append_audit(session, "apikey.revoked", actor=requester,
                 detail={"id": key_id, "label": row.label})
    session.commit()
    return {"revoked": True, "id": key_id}


@app.get("/api/config")
def system_config(_auth: str = Depends(require_action("execute"))) -> dict:
    """The effective governance & FinOps posture (F-OPS-09), read server-side.

    Powers the admin console's posture panel — a platform admin can see how every
    control is currently configured. Read-only; changing it stays a deploy-time
    concern (env / vault), never a portal write.
    """
    return {
        "modes": {
            "auth": os.getenv("AUTH_MODE", "mock").strip().lower(),
            "jira": jira_mode(),
            "auto_provision": auto_provision_enabled(),
            "provision_mode": provision_mode(),
            "use_mock": is_mock_mode(),
        },
        "governance": {
            "sod_enforced": _sod_enforced(),
            "four_eyes_enforced": _four_eyes_enforced(),
            "approval_quorum": _approval_quorum(),
            "approval_sla_hours": float(os.getenv("APPROVAL_SLA_HOURS", "24")),
            "audit_hmac": bool(os.getenv("AUDIT_HMAC_KEY", "").strip()),
        },
        "change_window": {
            "enabled": _change_window_enabled(),
            "open_now": change_window_status()["open"],
            "days": os.getenv("CHANGE_WINDOW_DAYS", "mon-fri"),
            "start": os.getenv("CHANGE_WINDOW_START", "08:00"),
            "end": os.getenv("CHANGE_WINDOW_END", "18:00"),
            "tz": os.getenv("CHANGE_WINDOW_TZ", "UTC"),
        },
        "finops": {
            "ttl_days_nonprod": _ttl_days_nonprod(),
            "ttl_warn_days": _ttl_warn_days(),
            "ttl_enforce": _ttl_enforce(),
            "budget_enforce": _budget_enforce(),
            "budget_warn_pct": _budget_warn_pct(),
            "variance_alert_pct": _variance_alert_pct(),
            "departed_owners_count": len(_departed_owners()),
        },
    }


@app.get("/api/stats")
def stats(session: Session = Depends(get_session),
          _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Whole-estate aggregate metrics for the overview dashboard (F-RPT-01).

    Role-gated to oversight roles (admin / auditor / finops). Computed
    server-side (authoritative); the portal only draws it.
    """
    def breakdown(column) -> list[dict]:
        rows = session.execute(select(column, func.count()).group_by(column)).all()
        out = [{"key": k if k is not None else "—", "count": c} for k, c in rows]
        return sorted(out, key=lambda r: r["count"], reverse=True)

    total = session.scalar(select(func.count()).select_from(Request)) or 0
    by_status = breakdown(Request.status)
    by_type = breakdown(Request.request_type)
    by_target = breakdown(Request.deployment_target)
    by_technology = breakdown(RequestComponent.technology_code)
    by_requester = breakdown(func.coalesce(Request.requester_name, Request.requester))
    by_subsidiary = breakdown(Request.subsidiary)

    status_count = {r["key"]: r["count"] for r in by_status}

    def sum_of(*names) -> int:
        return sum(status_count.get(n, 0) for n in names)

    active_cost = session.scalar(
        select(func.coalesce(func.sum(Estimate.monthly), 0))
        .select_from(Estimate).join(Request, Estimate.request_id == Request.id)
        .where(Request.status == "provisioned")
    ) or 0

    # Trend: requests created per ISO week, bucketed in Python for DB portability.
    weeks: Counter = Counter()
    for created in session.scalars(select(Request.created_at)):
        if created is not None:
            iso = created.isocalendar()
            weeks[f"{iso[0]}-W{iso[1]:02d}"] += 1

    # Approvals past their SLA and still waiting (F-GOV-01).
    breaching_sla = sum(
        1
        for req in session.scalars(select(Request).where(Request.status == "submitted"))
        if (_compute_sla(req.status, req.submitted_at, req.created_at) or {}).get("status") == "breached"
    )

    return {
        "kpis": {
            "total": total,
            "active": sum_of("provisioned"),
            "in_flight": sum_of("submitted", "planned", "in-progress"),
            "failed": sum_of("apply-failed", "decommission-failed", "rejected"),
            "decommissioned": sum_of("decommissioned"),
            "breaching_sla": breaching_sla,
        },
        "active_monthly_cost": {"amount": float(active_cost), "currency": "AED"},
        "by_status": by_status,
        "by_type": by_type,
        "by_technology": by_technology,
        "by_target": by_target,
        "by_requester": by_requester,
        "by_subsidiary": by_subsidiary,
        "trend": [{"week": w, "count": weeks[w]} for w in sorted(weeks)][-8:],
    }


# --- Showback & chargeback (E3.2, F-FIN-03) ----------------------------------

# The dimensions cost can be attributed to, and the SQL expression for each.
SHOWBACK_GROUPERS = {
    "cost_centre": Request.cost_centre_code,
    "project": Request.project_code,
    "environment": func.coalesce(Request.environment_name, Request.target_environment),
    "owner": func.coalesce(Request.application_owner, Request.requester_name, Request.requester),
}
# Which requests count as spend. 'active' = running (provisioned) cost, matching
# the estate KPI; 'committed' also counts in-flight requests. Drafts, rejected
# and decommissioned never count.
SHOWBACK_SCOPES = {
    "active": ["provisioned"],
    "committed": ["provisioned", "submitted", "planned", "in-progress"],
}


@app.get("/api/showback")
def showback(group_by: str = "cost_centre", scope: str = "active",
             session: Session = Depends(get_session),
             _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Monthly/annual cost broken down by who consumes it (F-FIN-03).

    Sums the estimate captured at submission (the authoritative cost, 1.6),
    grouped by cost centre / project / environment / owner. Read-only and
    role-gated to the oversight roles, like the estate dashboard. Computed
    server-side (P2); the portal only draws it.
    """
    if group_by not in SHOWBACK_GROUPERS:
        raise HTTPException(
            status_code=422,
            detail=f"group_by must be one of {sorted(SHOWBACK_GROUPERS)}.")
    if scope not in SHOWBACK_SCOPES:
        raise HTTPException(
            status_code=422, detail=f"scope must be one of {sorted(SHOWBACK_SCOPES)}.")

    dim = SHOWBACK_GROUPERS[group_by]
    rows = session.execute(
        select(dim, func.count(),
               func.coalesce(func.sum(Estimate.monthly), 0),
               func.coalesce(func.sum(Estimate.annual), 0))
        .select_from(Estimate).join(Request, Estimate.request_id == Request.id)
        .where(Request.status.in_(SHOWBACK_SCOPES[scope]))
        .group_by(dim)
    ).all()
    out = [
        {"key": k if k is not None else "—", "count": c,
         "monthly": round(float(m), 2), "annual": round(float(a), 2)}
        for k, c, m, a in rows
    ]
    out.sort(key=lambda r: r["monthly"], reverse=True)
    total = {
        "count": sum(r["count"] for r in out),
        "monthly": round(sum(r["monthly"] for r in out), 2),
        "annual": round(sum(r["annual"] for r in out), 2),
    }
    return {"group_by": group_by, "scope": scope, "currency": "AED",
            "total": total, "rows": out}


@app.get("/api/forecast")
def spend_forecast(months: int = 6, session: Session = Depends(get_session),
                   _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Projected monthly spend over the next `months` (F-RPT-05).

    A deterministic pipeline-plus-estate projection: today's provisioned run-rate,
    plus in-flight requests as they provision, minus non-prod environments as
    their TTL expires. Read-only; reconciles with Showback's committed scope.
    """
    return forecast.spend_forecast(session, months)


@app.get("/api/anomalies")
def anomalies(session: Session = Depends(get_session),
              _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Cost + request anomalies for review (F-FIN-09 / F-RPT-10).

    Deterministic detectors over the estate — outlier spend, cost overruns,
    budget breaches, requester bursts, sensitive-data exposure, high-impact
    in-flight requests. Read-only and advisory: it flags signals only, never
    blocks or acts.
    """
    return anomaly_detect.detect_anomalies(session)


@app.get("/api/optimisation")
def optimisation_digest(owner: str | None = None, session: Session = Depends(get_session),
                        _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Per-owner cost-optimisation digest (F-FIN-12).

    Concrete, non-prod-focused savings opportunities across provisioned
    environments — rightsizing, disabling HA, downgrading over-spec monitoring/
    support — each with a re-priced monthly saving. Read-only and advisory:
    it recommends only, it never changes a request. Optional `?owner=` filter.
    """
    return optim.optimisation_digest(session, owner)


@app.get("/api/sustainability")
def sustainability(session: Session = Depends(get_session),
                   _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Indicative energy + carbon footprint per environment (F-FIN-13).

    Estimated from each environment's sizing times documented power/PUE/grid
    coefficients (configurable via SUSTAIN_* env). A directional green-IT signal,
    not a metered value; read-only.
    """
    return sustainability_mod.estate_footprint(session)


@app.get("/api/shutdown")
def shutdown_status(session: Session = Depends(get_session),
                    _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Scheduled auto-shutdown status + quantified saving (F-FIN-06).

    The business-hours schedule, whether we're off-hours now, the off-hours
    fraction, and the potential monthly saving per non-prod environment + estate
    total. Pausing is opt-in (SHUTDOWN_ENABLED) and portal-side; this view is
    read-only.
    """
    return autoshutdown.status(session)


class ShutdownPolicyIn(BaseModel):
    enabled: bool = False
    days: str = "mon-fri"
    start: str = "08:00"
    end: str = "20:00"
    tz: str = "UTC"

    @field_validator("start", "end")
    @classmethod
    def _valid_hm(cls, v: str) -> str:
        try:
            hh, mm = (v or "").strip().split(":")
            hh, mm = int(hh), int(mm)
            assert 0 <= hh <= 23 and 0 <= mm <= 59
            return f"{hh:02d}:{mm:02d}"
        except Exception:
            raise ValueError("time must be HH:MM (00:00–23:59)")

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo((v or "").strip())
            return v.strip()
        except Exception:
            raise ValueError("tz must be a valid IANA timezone, e.g. 'Asia/Dubai'")

    @field_validator("days")
    @classmethod
    def _clean_days(cls, v: str) -> str:
        return (v or "").strip().lower() or "mon-fri"


@app.put("/api/shutdown")
def set_shutdown_policy(body: ShutdownPolicyIn, session: Session = Depends(get_session),
                        actor: str = Depends(require_action("execute"))) -> dict:
    """Set the global auto-shutdown schedule from the portal (F-FIN-06),
    platform_admin. Persisted in the DB (overrides the .env defaults) and audited.
    Note: the enabled toggle runs the SWEEP; a real cloud stop still also requires
    OCI_ACTUATE_ENABLED — that gate stays out of the UI (nothing real by surprise)."""
    policy = autoshutdown.set_policy(session, body.model_dump(), actor=actor)
    append_audit(session, "shutdown.policy.updated", actor=actor, detail=policy)
    session.commit()
    return autoshutdown.status(session)


# --- Budget guardrails (E3.3, F-FIN-02) --------------------------------------

def _budget_enforce() -> bool:
    """Whether an over-budget submit is a hard block. Off by default: over budget
    warns until an operator turns enforcement on."""
    return os.getenv("BUDGET_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _budget_warn_pct() -> float:
    try:
        return max(0.0, min(100.0, float(os.getenv("BUDGET_WARN_PCT", "90"))))
    except (ValueError, TypeError):
        return 90.0


def _committed_spend(session: Session, cost_centre: str | None,
                     exclude_ref: str | None = None) -> float:
    """Committed monthly spend for a cost centre (provisioned + in-flight),
    optionally excluding one request (the one being submitted)."""
    if not cost_centre:
        return 0.0
    stmt = (select(func.coalesce(func.sum(Estimate.monthly), 0))
            .select_from(Estimate).join(Request, Estimate.request_id == Request.id)
            .where(Request.cost_centre_code == cost_centre,
                   Request.status.in_(SHOWBACK_SCOPES["committed"])))
    if exclude_ref:
        stmt = stmt.where(Request.reference != exclude_ref)
    return float(session.scalar(stmt) or 0)


def _budget_status(session: Session, cost_centre: str | None, projected: float) -> dict | None:
    """Budget standing for a cost centre at a projected monthly spend (F-FIN-02).
    None when the cost centre has no budget defined (ungated)."""
    if not cost_centre:
        return None
    budget = session.scalar(select(Budget).where(Budget.cost_centre_code == cost_centre))
    if budget is None:
        return None
    limit = float(budget.monthly_limit)
    if projected > limit:
        status = "over"
    elif projected >= _budget_warn_pct() / 100.0 * limit:
        status = "near"
    else:
        status = "ok"
    return {"cost_centre": cost_centre, "limit": round(limit, 2),
            "projected": round(projected, 2), "remaining": round(limit - projected, 2),
            "status": status, "currency": budget.currency}


def _budget_message(b: dict) -> str:
    cc, cur = b["cost_centre"], b["currency"]
    if b["status"] == "over":
        return (f"Cost centre {cc} would exceed its monthly budget: projected "
                f"{cur} {b['projected']:,.0f} vs limit {cur} {b['limit']:,.0f} "
                f"({cur} {abs(b['remaining']):,.0f} over).")
    return (f"Cost centre {cc} is near its monthly budget: projected "
            f"{cur} {b['projected']:,.0f} of {cur} {b['limit']:,.0f} "
            f"({cur} {b['remaining']:,.0f} remaining).")


class BudgetIn(BaseModel):
    cost_centre_code: str
    monthly_limit: float
    currency: str = "AED"

    @field_validator("monthly_limit")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("monthly_limit must be positive.")
        return v


@app.get("/api/budgets")
def list_budgets(session: Session = Depends(get_session),
                 _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Every cost-centre budget with its current committed spend + remaining (F-FIN-02)."""
    out = []
    for b in session.scalars(select(Budget).order_by(Budget.cost_centre_code)):
        current = _committed_spend(session, b.cost_centre_code)
        st = _budget_status(session, b.cost_centre_code, current)
        out.append({"cost_centre": b.cost_centre_code, "limit": float(b.monthly_limit),
                    "currency": b.currency, "current": round(current, 2),
                    "remaining": round(float(b.monthly_limit) - current, 2),
                    "status": st["status"] if st else "ok"})
    return {"currency": "AED", "budgets": out}


@app.post("/api/budgets")
def set_budget(body: BudgetIn, session: Session = Depends(get_session),
               _auth: str = Depends(require_action("execute"))) -> dict:
    """Set or update a cost centre's monthly budget (F-FIN-02). platform_admin."""
    budget = session.scalar(select(Budget).where(Budget.cost_centre_code == body.cost_centre_code))
    if budget is None:
        budget = Budget(cost_centre_code=body.cost_centre_code)
        session.add(budget)
    budget.monthly_limit = body.monthly_limit
    budget.currency = body.currency
    append_audit(session, "budget.set", actor=_auth,
                 detail={"cost_centre": body.cost_centre_code,
                         "monthly_limit": body.monthly_limit, "currency": body.currency})
    session.commit()
    return {"cost_centre": body.cost_centre_code, "monthly_limit": body.monthly_limit,
            "currency": body.currency}


@app.delete("/api/budgets/{cost_centre_code}")
def delete_budget(cost_centre_code: str, session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("execute"))) -> dict:
    """Remove a cost centre's budget (F-FIN-02). platform_admin."""
    budget = session.scalar(select(Budget).where(Budget.cost_centre_code == cost_centre_code))
    if budget is None:
        raise HTTPException(status_code=404, detail=f"No budget defined for {cost_centre_code}.")
    session.delete(budget)
    append_audit(session, "budget.deleted", actor=_auth, detail={"cost_centre": cost_centre_code})
    session.commit()
    return {"deleted": True, "cost_centre": cost_centre_code}


# --- Quota management (E3.9, F-FIN-08) ---------------------------------------

def _quota_enforce() -> bool:
    """Whether an over-quota create is a hard block. Off by default: over quota
    warns until an operator turns enforcement on."""
    return os.getenv("QUOTA_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _environment_count(session: Session, project_code: str | None,
                       exclude_ref: str | None = None) -> int:
    """A project's active environments: committed (provisioned + in-flight)
    'create' requests, optionally excluding one."""
    if not project_code:
        return 0
    stmt = (select(func.count()).select_from(Request)
            .where(Request.project_code == project_code,
                   Request.request_type == "create",
                   Request.status.in_(SHOWBACK_SCOPES["committed"])))
    if exclude_ref:
        stmt = stmt.where(Request.reference != exclude_ref)
    return int(session.scalar(stmt) or 0)


def _quota_status(session: Session, project_code: str | None, projected: int) -> dict | None:
    """Quota standing for a project at a projected environment count (F-FIN-08).
    None when the project has no quota defined (ungated)."""
    if not project_code:
        return None
    quota = session.scalar(select(Quota).where(Quota.project_code == project_code))
    if quota is None:
        return None
    limit = int(quota.max_environments)
    if projected > limit:
        status = "over"
    elif projected == limit:
        status = "near"
    else:
        status = "ok"
    return {"project": project_code, "limit": limit, "projected": projected,
            "remaining": limit - projected, "status": status}


def _quota_message(q: dict) -> str:
    p = q["project"]
    if q["status"] == "over":
        return (f"Project {p} would exceed its environment quota: {q['projected']} "
                f"vs a limit of {q['limit']}.")
    return f"Project {p} is now at its environment quota: {q['projected']} of {q['limit']}."


class QuotaIn(BaseModel):
    project_code: str
    max_environments: int

    @field_validator("max_environments")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_environments must be positive.")
        return v


@app.get("/api/quotas")
def list_quotas(session: Session = Depends(get_session),
                _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Every project quota with its current environment count + remaining (F-FIN-08)."""
    out = []
    for q in session.scalars(select(Quota).order_by(Quota.project_code)):
        current = _environment_count(session, q.project_code)
        limit = int(q.max_environments)
        out.append({"project": q.project_code, "limit": limit, "current": current,
                    "remaining": limit - current,
                    "status": "over" if current > limit else "near" if current == limit else "ok"})
    return {"quotas": out}


@app.post("/api/quotas")
def set_quota(body: QuotaIn, session: Session = Depends(get_session),
              _auth: str = Depends(require_action("execute"))) -> dict:
    """Set or update a project's environment quota (F-FIN-08). platform_admin."""
    quota = session.scalar(select(Quota).where(Quota.project_code == body.project_code))
    if quota is None:
        quota = Quota(project_code=body.project_code)
        session.add(quota)
    quota.max_environments = body.max_environments
    append_audit(session, "quota.set", actor=_auth,
                 detail={"project": body.project_code, "max_environments": body.max_environments})
    session.commit()
    return {"project": body.project_code, "max_environments": body.max_environments}


@app.delete("/api/quotas/{project_code}")
def delete_quota(project_code: str, session: Session = Depends(get_session),
                 _auth: str = Depends(require_action("execute"))) -> dict:
    """Remove a project's quota (F-FIN-08). platform_admin."""
    quota = session.scalar(select(Quota).where(Quota.project_code == project_code))
    if quota is None:
        raise HTTPException(status_code=404, detail=f"No quota defined for {project_code}.")
    session.delete(quota)
    append_audit(session, "quota.deleted", actor=_auth, detail={"project": project_code})
    session.commit()
    return {"deleted": True, "project": project_code}


# --- Actual-vs-estimate variance (E3.5, F-FIN-01) ----------------------------

class ActualIn(BaseModel):
    billed_monthly: float
    period: str | None = None
    source: str = "manual"

    @field_validator("billed_monthly")
    @classmethod
    def _nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError("billed_monthly must be zero or positive.")
        return v


@app.post("/api/requests/{reference}/actual")
def record_actual(reference: str, body: ActualIn, session: Session = Depends(get_session),
                  actor: str = Depends(require_action("view_overview"))):
    """Record the actual billed monthly cost for a request (F-FIN-01).

    Computes variance against the approved estimate and, when the drift exceeds
    VARIANCE_ALERT_PCT, raises a one-time alert (Jira comment + variance.alert).
    Oversight-gated (finops/admin/auditor). Re-recording the same figure doesn't
    re-alert; a changed figure re-arms the alert.
    """
    req = _load_request(reference, session)
    if req.estimate is None:
        raise HTTPException(status_code=400,
                            detail="No approved estimate to compare the actual against.")
    actual = session.scalar(select(ActualCost).where(ActualCost.reference == reference))
    changed = actual is None or float(actual.billed_monthly) != float(body.billed_monthly)
    if actual is None:
        actual = ActualCost(reference=reference)
        session.add(actual)
    actual.billed_monthly = body.billed_monthly
    actual.period = body.period
    actual.source = body.source
    if changed:
        actual.alerted = False
    append_audit(session, "actual.recorded", reference=reference, actor=actor,
                 detail={"billed_monthly": body.billed_monthly, "period": body.period,
                         "source": body.source})

    v = _variance_status(req.estimate.monthly, body.billed_monthly)
    if v and v["status"] != "on-track" and not actual.alerted:
        actual.alerted = True
        req.status_detail = (f"Cost variance {v['variance_pct']:+.0f}% vs estimate "
                             f"(billed {v['actual']:,.0f} vs {v['estimate']:,.0f} AED/mo).")
        if req.approval is not None:
            try:
                add_comment(req.approval.jira_key,
                            f"📊 Cost variance alert: {reference} is billing "
                            f"{v['actual']:,.0f} vs an estimate of {v['estimate']:,.0f} AED/mo "
                            f"({v['variance_pct']:+.0f}%).")
            except JiraError:
                pass
        append_audit(session, "variance.alert", reference=reference,
                     jira_key=req.approval.jira_key if req.approval else None, actor=actor,
                     detail=v)
    session.commit()
    return {"reference": reference, **(v or {})}


@app.get("/api/variance")
def variance_report(session: Session = Depends(get_session),
                    _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Actual-vs-estimate variance across the estate (F-FIN-01): one row per
    request that has both an estimate and a recorded actual, biggest drift first,
    plus estate totals. Oversight-gated."""
    rows = []
    tot_est = tot_act = 0.0
    for req, est, act in session.execute(
        select(Request, Estimate.monthly, ActualCost.billed_monthly)
        .join(Estimate, Estimate.request_id == Request.id)
        .join(ActualCost, ActualCost.reference == Request.reference)
    ).all():
        v = _variance_status(est, act)
        if v is None:
            continue
        rows.append({"reference": req.reference, "cost_centre": req.cost_centre_code,
                     "environment": req.environment_name or req.target_environment, **v})
        tot_est += v["estimate"]
        tot_act += v["actual"]
    rows.sort(key=lambda r: abs(r["variance_pct"]), reverse=True)
    total_variance = round(tot_act - tot_est, 2)
    return {"currency": "AED",
            "total": {"estimate": round(tot_est, 2), "actual": round(tot_act, 2),
                      "variance": total_variance,
                      "variance_pct": round(total_variance / tot_est * 100.0, 1) if tot_est else 0.0},
            "rows": rows}


# --- Ownership transfer & orphan detection (E3.7, F-LCM-10) -------------------

OWNER_ROLES = {"environment_owner", "application_owner", "business_owner", "technical_owner"}


class TransferOwnerIn(BaseModel):
    new_owner: str
    role: str = "environment_owner"

    @field_validator("new_owner")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("new_owner is required.")
        return v.strip()

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        if v not in OWNER_ROLES:
            raise ValueError(f"role must be one of {sorted(OWNER_ROLES)}.")
        return v


@app.post("/api/requests/{reference}/transfer-owner")
def transfer_owner(reference: str, body: TransferOwnerIn, session: Session = Depends(get_session),
                   actor: str = Depends(require_action("execute"))):
    """Reassign an environment's owner (F-LCM-10). platform_admin.

    Records the change (from -> to) in the tamper-evident trail and clears any
    orphan flag, so a reassigned environment stops being flagged.
    """
    req = _load_request(reference, session)
    old = getattr(req, body.role, None)
    setattr(req, body.role, body.new_owner)
    req.orphaned_at = None  # a fresh owner clears the orphan flag
    if req.status_detail and "orphan" in req.status_detail.lower():
        req.status_detail = None
    append_audit(session, "ownership.transferred", reference=reference, actor=actor,
                 detail={"role": body.role, "from": old, "to": body.new_owner})
    session.commit()
    return {"reference": reference, "role": body.role, "from": old, "to": body.new_owner,
            "owner": _resolve_owner(req), "orphaned": _is_orphan(req)}


class OwnerGroupIn(BaseModel):
    group: str | None = None  # empty/None clears — back to individual ownership


@app.put("/api/requests/{reference}/owner-group")
def set_owner_group(reference: str, body: OwnerGroupIn, session: Session = Depends(get_session),
                    actor: str = Depends(_authed_requester)) -> dict:
    """Set or clear an environment's owning directory group (F-IAM-09) — the owner
    or a platform_admin. A group owner survives an individual leaving, and its
    members can manage the environment. Audited."""
    req = _load_request(reference, session)
    if not _owner_or_admin(req, actor):
        raise HTTPException(status_code=403, detail=("Only the environment owner or a platform "
                            "administrator can set its owning group."))
    grp = (body.group or "").strip() or None
    old = req.owner_group
    req.owner_group = grp
    if grp:  # a group now owns it — clear any orphan flag
        req.orphaned_at = None
        if req.status_detail and "orphan" in req.status_detail.lower():
            req.status_detail = None
    append_audit(session, "ownership.group_set", reference=reference, actor=actor,
                 detail={"from": old, "to": grp})
    session.commit()
    return {"reference": reference, "owner_group": grp, "orphaned": _is_orphan(req)}


@app.get("/api/orphans")
def orphans(session: Session = Depends(get_session),
            _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Provisioned environments with no resolvable owner (F-LCM-10), for
    reassignment. Oversight-gated."""
    out = []
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        if not _is_orphan(req):
            continue
        owner = _resolve_owner(req)
        out.append({"reference": req.reference,
                    "environment": req.environment_name or req.target_environment,
                    "owner": owner,
                    "reason": "no owner assigned" if not owner else f"owner '{owner}' has left"})
    return {"count": len(out), "orphans": out}


@app.get("/api/health-scores")
def health_scores(session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Environment health across the estate (F-LCM-08): a score per provisioned
    environment, worst first, plus the average. Oversight-gated."""
    rows = []
    total = 0
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        h = _health_for(session, req)
        if h is None:
            continue
        rows.append({"reference": req.reference,
                     "environment": req.environment_name or req.target_environment,
                     "owner": _resolve_owner(req), **h})
        total += h["score"]
    rows.sort(key=lambda r: r["score"])  # worst first
    return {"count": len(rows),
            "average": round(total / len(rows), 1) if rows else None,
            "scores": rows}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mock": is_mock_mode()}


# --- Lookups (increment 1.2) -------------------------------------------------
# Typed response shapes so the JSON is clean and stable for the portal to render.


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class CostCentreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class TechnologyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    lifecycle_state: str


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    environment_class: str


class LookupsResponse(BaseModel):
    projects: list[ProjectOut]
    cost_centres: list[CostCentreOut]
    subsidiaries: list[CostCentreOut]
    technologies: list[TechnologyOut]
    environments: list[EnvironmentOut]


@app.get("/api/lookups", response_model=LookupsResponse)
def lookups(session: Session = Depends(get_session)) -> LookupsResponse:
    """Read-only reference data for the guided-request form's dropdowns."""
    return LookupsResponse(
        projects=session.scalars(select(Project).order_by(Project.name)).all(),
        cost_centres=session.scalars(select(CostCentre).order_by(CostCentre.name)).all(),
        # Subsidiaries are synced from Jira (customfield_36200) in live mode; show
        # only the active ones (Jira-removed ones are deactivated, not deleted).
        subsidiaries=session.scalars(
            select(Subsidiary).where(Subsidiary.active.is_(True)).order_by(Subsidiary.name)
        ).all(),
        technologies=session.scalars(select(Technology).order_by(Technology.name)).all(),
        environments=session.scalars(select(Environment).order_by(Environment.name)).all(),
    )


@app.post("/api/lookups/subsidiaries/sync")
def sync_subsidiaries_now(requester: str = Depends(_authed_requester)) -> dict:
    """Trigger an immediate subsidiary sync from Jira (customfield_36200).

    In live mode a background thread already syncs on an interval; this is the
    "sync now" for a platform admin (and how we verify the integration). Reads
    Jira's field options and reconciles the Subsidiary table (add/rename/
    reactivate/deactivate) — never deletes, and a failed/empty fetch is a no-op.
    """
    if roles_mod.PLATFORM_ADMIN not in roles_mod.resolve_roles(requester):
        raise HTTPException(
            status_code=403,
            detail="Only a platform administrator can sync subsidiaries.",
        )
    return _sync_subsidiaries_once()


# --- AI request drafting (F-RPT-06) ------------------------------------------

class AiDraftIn(BaseModel):
    description: str


@app.post("/api/ai/draft")
def ai_draft(
    body: AiDraftIn,
    session: Session = Depends(get_session),
    requester: str = Depends(require_action("create_request")),
) -> dict:
    """Draft a provisioning request from a plain-English description (F-RPT-06).

    The AI **recommends a draft only** (ARCHITECTURE.md §7): it fills the form
    fields, constrained to the approved catalogue, but never submits, approves,
    prices, or provisions. The requester reviews, edits, and submits through the
    normal path, where validate_submission re-checks everything (P2). Every draft
    is audited so the AI's involvement is on the record.
    """
    description = (body.description or "").strip()
    if len(description) < 8:
        raise HTTPException(
            status_code=422,
            detail="Describe what you need in a sentence or two (at least 8 characters).",
        )
    try:
        result = ai_drafter.draft_request(description, session)
    except ai_drafter.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    append_audit(
        session,
        "ai.drafted",
        actor=requester,
        detail={
            "mode": result["mode"],
            "description": description[:500],
            "warnings": result["warnings"],
        },
    )
    session.commit()
    return result


# --- Requests: drafts + submission (increment 1.3) ---------------------------

# The editable scalar fields a draft carries (components handled separately;
# reference/status/requester are managed).
REQUEST_FIELDS = (
    "request_type",
    "project_code",
    "cost_centre_code",
    "subsidiary",
    "deployment_target",
    "environment_name",
    "target_environment",
    "environment_tier",
    "source_reference",
    "refresh_from_reference",
    "restore_backup_id",
    "data_classification",
    # Governance metadata (increment 6.1).
    "business_justification",
    "priority",
    "business_criticality",
    "required_delivery_date",
    "application_owner",
    "business_owner",
    "technical_owner",
    "environment_owner",
    "owner_group",
    # Advanced options (6.5).
    "advanced_options",
)


class ComponentIn(BaseModel):
    technology_code: str | None = None
    size: str | None = None


class ComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    technology_code: str | None = None
    size: str | None = None


class DraftIn(BaseModel):
    """Everything optional — a draft may be saved half-finished (F-UX-01)."""

    reference: str | None = None
    request_type: str | None = None
    project_code: str | None = None
    cost_centre_code: str | None = None
    subsidiary: str | None = None
    deployment_target: str | None = None
    environment_name: str | None = None
    target_environment: str | None = None
    environment_tier: str | None = None
    source_reference: str | None = None
    refresh_from_reference: str | None = None
    restore_backup_id: int | None = None
    data_classification: str | None = None
    # Governance metadata (increment 6.1). All optional at draft time.
    business_justification: str | None = None
    priority: str | None = None
    business_criticality: str | None = None
    required_delivery_date: date | None = None
    application_owner: str | None = None
    business_owner: str | None = None
    technical_owner: str | None = None
    environment_owner: str | None = None
    owner_group: str | None = None
    advanced_options: dict | None = None
    components: list[ComponentIn] | None = None

    @field_validator("required_delivery_date", mode="before")
    @classmethod
    def _blank_date_to_none(cls, v):
        """Treat an empty string (an unfilled date field) as no date."""
        return None if v in ("", None) else v


class EstimateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    currency: str
    one_time: float
    monthly: float
    annual: float


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    jira_key: str
    status: str
    ticket_url: str | None = None
    ticket_body: str | None = None


def _compute_sla(status: str | None, submitted_at, created_at) -> dict | None:
    """Approval SLA status for a request awaiting approval (F-GOV-01), computed
    server-side (authoritative). None unless the request is still 'submitted'.
    APPROVAL_SLA_HOURS<=0 means an immediate breach (handy for demos/tests)."""
    if status != "submitted":
        return None
    start = submitted_at or created_at
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    sla_hours = float(os.getenv("APPROVAL_SLA_HOURS", "24"))
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() / 3600.0
    if sla_hours <= 0 or elapsed >= sla_hours:
        state = "breached"
    elif elapsed >= 0.8 * sla_hours:
        state = "due-soon"
    else:
        state = "on-time"
    return {
        "sla_hours": sla_hours,
        "elapsed_hours": round(elapsed, 2),
        "due_at": (start + timedelta(hours=sla_hours)).isoformat(),
        "status": state,
    }


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reference: str
    status: str
    status_detail: str | None = None
    requester: str
    requester_name: str | None = None
    request_type: str | None = None
    project_code: str | None = None
    cost_centre_code: str | None = None
    subsidiary: str | None = None
    deployment_target: str | None = None
    environment_name: str | None = None
    target_environment: str | None = None
    environment_tier: str | None = None
    source_reference: str | None = None
    refresh_from_reference: str | None = None
    restore_backup_id: int | None = None
    data_classification: str | None = None
    # Governance metadata (increment 6.1).
    business_justification: str | None = None
    priority: str | None = None
    business_criticality: str | None = None
    required_delivery_date: date | None = None
    application_owner: str | None = None
    business_owner: str | None = None
    technical_owner: str | None = None
    environment_owner: str | None = None
    owner_group: str | None = None
    advanced_options: dict | None = None
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    components: list[ComponentOut] = []
    estimate: EstimateOut | None = None
    approval: ApprovalOut | None = None
    # Policy waiver (F-GOV-02): the documented exception, if one was granted.
    waiver: dict | None = None
    # Advisory policy warnings from the last submit (F-GOV-03). Transient — set on
    # the submit response, not stored; empty on a plain read.
    policy_warnings: list[str] = []
    # Environment TTL (F-FIN-07): {expiry, days_left, status} for a provisioned
    # non-prod environment, else None. Attached by the list/get endpoints.
    ttl: dict | None = None
    # Cost variance (F-FIN-01): {estimate, actual, variance_pct, status} when an
    # actual has been recorded, else None. Attached by the list/get endpoints.
    variance: dict | None = None
    # Ownership (F-LCM-10): the resolved environment owner + whether it's orphaned.
    owner: str | None = None
    orphaned: bool = False
    # Environment health (F-LCM-08): {score, grade, factors} for a provisioned env.
    health: dict | None = None
    # Drift (F-LCM-09): {detected, checked_at} from the last drift check, else None.
    drift: dict | None = None
    # Cloud state sync: {status, synced_at} from the last reconciliation, else None.
    state: dict | None = None
    # Operational power state (cloud-sync increment 2): 'running' | 'stopped' |
    # 'partial' for a provisioned environment's resources, else None.
    power: str | None = None
    # Per-request auto-shutdown (F-FIN-06 B): {override, effective} for a
    # provisioned environment, else None.
    shutdown: dict | None = None
    # Backup restore-points (F-LCM-06) for a provisioned environment, newest first.
    backups: list | None = None
    # Active JIT access grants (F-IAM-07) for a provisioned environment — metadata
    # only, never the credential.
    access_grants: list | None = None

    @computed_field
    @property
    def approval_sla(self) -> dict | None:
        return _compute_sla(self.status, self.submitted_at, self.created_at)


def _load_request(reference: str, session: Session) -> Request:
    req = session.scalar(select(Request).where(Request.reference == reference))
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{reference}' not found.")
    return req


@app.post("/api/requests/draft", response_model=RequestOut)
def save_draft(
    body: DraftIn,
    session: Session = Depends(get_session),
    requester: str = Depends(require_action("create_request")),
    requester_name: str | None = Depends(get_requester_name),
) -> RequestOut:
    """Create or update a draft. Lenient: partial data is allowed.

    The requester identity comes from get_requester: a validated Microsoft token
    in live mode (2.3b, §8), or the X-Requester header / default in mock mode.
    """
    if body.reference:
        req = _load_request(body.reference, session)
    else:
        # Readable sequential reference (REQ-2026-0001). Not tied to the row id,
        # so it can be assigned before insert (reference is NOT NULL).
        next_seq = (session.scalar(select(func.max(Request.id))) or 0) + 1
        req = Request(
            status="draft",
            requester=requester,
            requester_name=requester_name,
            reference=f"REQ-{datetime.now(timezone.utc).year}-{next_seq:04d}",
        )
        session.add(req)

    # Only update fields the caller actually sent, so a partial save can't wipe
    # data set by an earlier save. (The portal sends the whole form each time.)
    provided = body.model_dump(exclude_unset=True)
    for field in REQUEST_FIELDS:
        if field in provided:
            setattr(req, field, provided[field])

    # If components were sent, replace the request's component list with them.
    if body.components is not None:
        req.components = [
            RequestComponent(technology_code=c.technology_code, size=c.size)
            for c in body.components
        ]

    req.status = "draft"
    session.commit()
    return RequestOut.model_validate(req)


@app.get("/api/requests", response_model=list[RequestOut])
def list_requests(
    requester: str | None = None,
    requested_by: str | None = None,
    subsidiary: str | None = None,
    status: str | None = None,
    request_type: str | None = None,
    technology: str | None = None,
    deployment_target: str | None = None,
    created_week: str | None = None,
    session: Session = Depends(get_session),
) -> list[RequestOut]:
    """List requests newest-first with optional filters.

    `status` accepts a comma-separated set (e.g. 'submitted,planned'). Powers My
    requests, the decommission source picker (status='provisioned'), and the
    overview dashboard's drill-down (filter by status/type/technology/target/week).
    Read-only.
    """
    stmt = select(Request).order_by(Request.id.desc())
    if requester:
        stmt = stmt.where(Request.requester == requester)
    if requested_by:  # matches the displayed name-or-email (the by-requester chart)
        stmt = stmt.where(
            func.coalesce(Request.requester_name, Request.requester) == requested_by
        )
    if subsidiary:
        stmt = stmt.where(Request.subsidiary == subsidiary)
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        stmt = stmt.where(Request.status.in_(wanted))
    if request_type:
        stmt = stmt.where(Request.request_type == request_type)
    if deployment_target:
        stmt = stmt.where(Request.deployment_target == deployment_target)
    if technology:
        stmt = stmt.where(Request.components.any(RequestComponent.technology_code == technology))

    results = list(session.scalars(stmt))
    if created_week:  # ISO week bucket, matched in Python for DB portability
        def _week(dt) -> str:
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        results = [r for r in results if r.created_at and _week(r.created_at) == created_week]

    # Attach each environment's TTL (F-FIN-07) + cost variance (F-FIN-01), batched.
    refs = [r.reference for r in results]
    ttl_map: dict[str, datetime] = {}
    actual_map: dict[str, float] = {}
    power_map: dict[str, list[str]] = {}
    if refs:
        for ref, exp in session.execute(
            select(ProvisionedResource.reference, func.min(ProvisionedResource.ttl_expiry))
            .where(ProvisionedResource.reference.in_(refs),
                   ProvisionedResource.lifecycle_state == "active",
                   ProvisionedResource.ttl_expiry.is_not(None))
            .group_by(ProvisionedResource.reference)
        ).all():
            if exp is not None:
                ttl_map[ref] = exp
        for ref, billed in session.execute(
            select(ActualCost.reference, ActualCost.billed_monthly)
            .where(ActualCost.reference.in_(refs))
        ).all():
            actual_map[ref] = billed
        for ref, ps in session.execute(
            select(ProvisionedResource.reference, ProvisionedResource.power_state)
            .where(ProvisionedResource.reference.in_(refs),
                   ProvisionedResource.lifecycle_state == "active",
                   ProvisionedResource.kind == "oci-instance")
        ).all():
            power_map.setdefault(ref, []).append(ps)
    outs = []
    for r in results:
        out = RequestOut.model_validate(r)
        out.ttl = _ttl_status(ttl_map.get(r.reference))
        out.variance = _variance_status(r.estimate.monthly if r.estimate else None,
                                        actual_map.get(r.reference))
        out.owner = _resolve_owner(r)
        out.orphaned = r.status == "provisioned" and _is_orphan(r)
        out.health = _health_for(session, r, actual=actual_map.get(r.reference))
        out.drift = _drift_out(r)
        out.state = _state_out(r)
        out.power = _derive_power(power_map.get(r.reference, []))
        out.shutdown = _shutdown_out(session, r)
        out.backups = _backups_out(session, r)
        out.access_grants = _access_out(session, r)
        outs.append(out)
    return outs


def _drift_out(req: Request) -> dict | None:
    """The last drift-check result for the portal (F-LCM-09), else None."""
    if req.drift_checked_at is None:
        return None
    return {"detected": bool(req.drift_detected), "checked_at": req.drift_checked_at.isoformat()}


def _state_out(req: Request) -> dict | None:
    """The last cloud-state reconciliation result for the portal, else None."""
    if req.state_synced_at is None:
        return None
    return {"status": req.state_status or "unknown", "synced_at": req.state_synced_at.isoformat()}


def _derive_power(states: list[str]) -> str | None:
    """The environment's power state from its active resources' power states:
    'running' if all running, 'stopped' if all stopped, 'partial' if mixed, None
    if it has no active resources."""
    states = [s or "running" for s in states]
    if not states:
        return None
    if all(s == "running" for s in states):
        return "running"
    if all(s == "stopped" for s in states):
        return "stopped"
    return "partial"


def _power_for(session: Session, reference: str) -> str | None:
    """Power state for a single request (used by the detail endpoint). Only
    compute instances have a power state — buckets can't be stopped/started."""
    return _derive_power(session.scalars(
        select(ProvisionedResource.power_state).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.kind == "oci-instance",
        )
    ).all())


def _backup_row(b: Backup) -> dict:
    return {"id": b.id, "label": b.label, "created_by": b.created_by,
            "created_at": b.created_at.isoformat() if b.created_at else None}


def _backups_out(session: Session, req: Request) -> list | None:
    """A provisioned environment's backup restore-points (F-LCM-06), newest first."""
    if req.status != "provisioned":
        return None
    rows = session.scalars(
        select(Backup).where(Backup.reference == req.reference).order_by(Backup.id.desc())
    ).all()
    return [_backup_row(b) for b in rows]


def _shutdown_out(session: Session, req: Request) -> dict | None:
    """Per-request auto-shutdown for the portal: the override + the resolved
    effective schedule (override merged over global). Provisioned envs only."""
    if req.status != "provisioned":
        return None
    return {"override": req.shutdown_override,
            "effective": autoshutdown.effective_policy(session, req)}


def _environment_resource_kind(session: Session, req: Request) -> str:
    """The cloud resource this environment provisions, from its components:
    'oci-instance' if any component is a compute technology, else 'oci-bucket'."""
    codes = [c.technology_code for c in req.components if c.technology_code]
    if not codes:
        return "oci-bucket"
    kinds = session.scalars(
        select(Technology.resource_kind).where(Technology.code.in_(codes))
    ).all()
    return "oci-instance" if "oci-instance" in kinds else "oci-bucket"


@app.get("/api/requests/{reference}", response_model=RequestOut)
def get_request(reference: str, session: Session = Depends(get_session)) -> RequestOut:
    """Load a draft (or submitted request) so it can be resumed/viewed."""
    req = _load_request(reference, session)
    out = RequestOut.model_validate(req)
    out.ttl = _ttl_status(_min_active_ttl(session, reference))
    actual = session.scalar(select(ActualCost.billed_monthly).where(ActualCost.reference == reference))
    out.variance = _variance_status(req.estimate.monthly if req.estimate else None, actual)
    out.owner = _resolve_owner(req)
    out.orphaned = req.status == "provisioned" and _is_orphan(req)
    out.health = _health_for(session, req, actual=actual)
    out.drift = _drift_out(req)
    out.state = _state_out(req)
    out.power = _power_for(session, reference)
    out.shutdown = _shutdown_out(session, req)
    out.backups = _backups_out(session, req)
    out.access_grants = _access_out(session, req)
    return out


def _min_active_ttl(session: Session, reference: str) -> datetime | None:
    """Earliest expiry among a request's active provisioned resources (F-FIN-07)."""
    return session.scalar(
        select(func.min(ProvisionedResource.ttl_expiry)).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.ttl_expiry.is_not(None),
        )
    )


def _parse_waiver_expiry(expires: str) -> datetime:
    """Parse a waiver expiry (ISO date or datetime) to an aware UTC datetime.
    A date-only value (YYYY-MM-DD) expires at the end of that day. Raises
    ValueError on an unparseable value (F-GOV-02)."""
    exp = datetime.fromisoformat(expires)
    if len(expires.strip()) == 10:  # date-only -> valid through end of that day
        exp = exp.replace(hour=23, minute=59, second=59)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp


def _waiver_active(req: Request, now: datetime | None = None) -> bool:
    """Whether the request carries a valid (non-expired) policy waiver (F-GOV-02).
    A waiver with no expiry is open-ended; an unparseable expiry counts as
    inactive (fail-closed, so a malformed exception never waves anything through)."""
    waiver = getattr(req, "waiver", None)
    if not waiver:
        return False
    expires = waiver.get("expires_at")
    if not expires:
        return True
    try:
        exp = _parse_waiver_expiry(expires)
    except (ValueError, TypeError):
        return False
    return (now or datetime.now(timezone.utc)) < exp


class WaiverIn(BaseModel):
    """A documented policy exception (F-GOV-02). `expires_at` is an ISO date or
    datetime; omit it for an open-ended waiver."""

    reason: str
    expires_at: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason_meaningful(cls, v: str) -> str:
        if not v or len(v.strip()) < 10:
            raise ValueError("A waiver reason of at least 10 characters is required.")
        return v.strip()

    @field_validator("expires_at", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return None if v in ("", None) else v


@app.post("/api/requests/{reference}/waiver", response_model=RequestOut)
def grant_waiver(
    reference: str,
    body: WaiverIn,
    session: Session = Depends(get_session),
    actor: str = Depends(require_action("grant_waiver")),
) -> RequestOut:
    """Grant a documented, expiring exception to the policy gate (F-GOV-02).

    An authorised approver records a waiver so a request the OPA policy gate would
    block can be submitted. Governance guards: the granter must **not** be the
    requester (no self-waiver), and the exception is written to the tamper-evident
    audit trail. Only *policy* violations are waivable — field validation still
    applies at submit, and the waiver is consumed (logged as `policy.waived`) only
    if the policy actually blocks.
    """
    req = _load_request(reference, session)

    # No self-waiver: you can't wave through your own request's policy violation.
    if actor and actor == req.requester:
        append_audit(session, "waiver.blocked", reference=req.reference, actor=actor,
                     detail={"reason": "self-waiver", "requester": req.requester})
        session.commit()
        raise HTTPException(
            status_code=403,
            detail=(f"You raised {req.reference}, so you cannot grant its own policy "
                    "waiver. A different approver must grant it."),
        )

    if body.expires_at is not None:
        try:
            _parse_waiver_expiry(body.expires_at)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="expires_at must be an ISO date (YYYY-MM-DD) or datetime.",
            )

    req.waiver = {
        "reason": body.reason,
        "granted_by": actor,
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": body.expires_at,
    }
    append_audit(session, "waiver.granted", reference=req.reference, actor=actor,
                 detail={"reason": body.reason, "expires_at": body.expires_at})
    session.commit()
    return RequestOut.model_validate(req)


@app.post("/api/requests/{reference}/submit")
def submit_request(
    reference: str,
    session: Session = Depends(get_session),
    policy_eval=Depends(get_policy_evaluator),
    _auth: str = Depends(require_action("create_request")),
):
    """Validate, run the OPA policy gate, then mark the request submitted."""
    req = _load_request(reference, session)
    # Idempotent: if a ticket was already raised (e.g. a retry after the portal
    # timed out), return it rather than creating a duplicate.
    if req.approval is not None:
        return RequestOut.model_validate(req)

    # Decommission inherits context (target, project, cost centre, environment)
    # from the request it tears down, so pricing and the ticket have full context.
    if req.request_type == "decommission" and req.source_reference:
        source = session.scalar(
            select(Request).where(Request.reference == req.source_reference)
        )
        if source is not None:
            req.deployment_target = req.deployment_target or source.deployment_target
            req.project_code = req.project_code or source.project_code
            req.cost_centre_code = req.cost_centre_code or source.cost_centre_code
            req.subsidiary = req.subsidiary or source.subsidiary
            req.environment_name = req.environment_name or source.environment_name

    # Refresh + restore inherit context from the TARGET env they operate on
    # (F-LCM-03/06), so the ticket + guardrails have full context.
    if req.request_type in ("refresh", "restore") and req.source_reference:
        target = session.scalar(select(Request).where(Request.reference == req.source_reference))
        if target is not None:
            req.deployment_target = req.deployment_target or target.deployment_target
            req.project_code = req.project_code or target.project_code
            req.cost_centre_code = req.cost_centre_code or target.cost_centre_code
            req.subsidiary = req.subsidiary or target.subsidiary
            req.environment_name = req.environment_name or target.environment_name
            req.environment_tier = req.environment_tier or target.environment_tier
            req.data_classification = req.data_classification or target.data_classification

    components_data = [
        {"technology_code": c.technology_code, "size": c.size} for c in req.components
    ]
    data = {field: _jsonable(getattr(req, field)) for field in REQUEST_FIELDS}
    data["components"] = components_data
    errors = validate_submission(data, session)
    if errors:
        return JSONResponse(status_code=422, content={"errors": errors})

    # Policy-as-code gate (F-GOV-03): OPA decides, we obey. Fail-safe on outage.
    try:
        verdict = policy_eval(data)
    except PolicyUnavailable:
        return JSONResponse(
            status_code=503,
            content={"policy_error": "Policy service unavailable — request not submitted."},
        )
    if not verdict["allow"]:
        # A documented, non-expired waiver (F-GOV-02) lets the request through
        # despite the policy violations — recording the exception in the
        # tamper-evident trail. Without one (or an expired one) it stays blocked.
        if _waiver_active(req):
            append_audit(session, "policy.waived", reference=req.reference,
                         actor=(req.waiver or {}).get("granted_by"),
                         detail={"violations": verdict["violations"],
                                 "reason": (req.waiver or {}).get("reason"),
                                 "expires_at": (req.waiver or {}).get("expires_at")})
        else:
            return JSONResponse(
                status_code=422, content={"policy_violations": verdict["violations"]}
            )

    # Server-computed estimate (1.6) — needed now for the budget guardrail below.
    breakdown = estimate_cost(components_data, req.deployment_target, session, req.advanced_options)
    monthly = float(breakdown["totals"]["monthly"])

    # Budget guardrail (F-FIN-02): compare the cost centre's projected committed
    # spend against its budget. Over budget hard-blocks only when enforcement is
    # on; otherwise over/near is an advisory warning. Undefined budgets are ungated.
    budget_warnings: list[str] = []
    projected = _committed_spend(session, req.cost_centre_code, exclude_ref=req.reference) + monthly
    bstatus = _budget_status(session, req.cost_centre_code, projected)
    if bstatus is not None and bstatus["status"] == "over" and _budget_enforce():
        append_audit(session, "budget.blocked", reference=req.reference, detail=bstatus)
        session.commit()
        return JSONResponse(status_code=422, content={"budget_error": _budget_message(bstatus)})
    if bstatus is not None and bstatus["status"] in ("over", "near"):
        budget_warnings.append(_budget_message(bstatus))
        append_audit(session, "budget.warning", reference=req.reference, detail=bstatus)

    # Quota guardrail (F-FIN-08): cap environments per project — create requests
    # only (add/resize/decommission don't create a new environment).
    if req.request_type == "create":
        qcount = _environment_count(session, req.project_code, exclude_ref=req.reference)
        qstatus = _quota_status(session, req.project_code, qcount + 1)
        if qstatus is not None and qstatus["status"] == "over" and _quota_enforce():
            append_audit(session, "quota.blocked", reference=req.reference, detail=qstatus)
            session.commit()
            return JSONResponse(status_code=422, content={"quota_error": _quota_message(qstatus)})
        if qstatus is not None and qstatus["status"] in ("over", "near"):
            budget_warnings.append(_quota_message(qstatus))
            append_audit(session, "quota.warning", reference=req.reference, detail=qstatus)

    req.status = "submitted"
    # Denormalise the resource kind now that components are final, so the handoff
    # and orchestrator can branch (bucket vs stoppable compute) without re-deriving.
    req.resource_kind = _environment_resource_kind(session, req)
    req.submitted_at = req.submitted_at or datetime.now(timezone.utc)  # SLA clock (F-GOV-01)
    # Capture the server-computed estimate as a stored fact at submission (1.6).
    req.estimate = Estimate(
        deployment_target=breakdown["deployment_target"],
        currency=breakdown["currency"],
        one_time=breakdown["totals"]["one_time"],
        monthly=breakdown["totals"]["monthly"],
        annual=breakdown["totals"]["annual"],
        breakdown=breakdown,
    )

    # Raise the Jira approval with config + cost + plan preview together (1.8;
    # real Jira in 2.4). If live Jira creation fails, refuse the submit rather
    # than leaving a submitted request with no approval ticket.
    plan_preview = build_plan_preview(req, session)
    ticket_body = build_ticket_body(req, breakdown, plan_preview)
    # A one-page request + costing PDF (2.4c) and an Excel cost sheet (6.4) for
    # the approver — never fail a submit over an attachment.
    attachment = None
    extra_attachments: list[tuple[str, bytes]] = []
    try:
        sizing = resolve_components(components_data, session)
        pdf = build_request_pdf(req, breakdown, sizing)
        attachment = (f"request-{req.reference}.pdf", pdf)
        xlsx = build_cost_sheet_xlsx(req, breakdown, sizing)
        extra_attachments.append((f"cost-{req.reference}.xlsx", xlsx))
    except Exception:  # noqa: BLE001
        pass
    try:
        req.approval = create_issue(
            session, req, ticket_body, attachment=attachment, attachments=extra_attachments
        )
    except JiraError as exc:
        session.rollback()
        return JSONResponse(
            status_code=502,
            content={"error": f"Could not raise the Jira approval ticket: {exc}"},
        )

    # Advisory notes (F-GOV-03 policy + F-FIN-02 budget): don't block, but record
    # them in the tamper-evident trail (so they show in the evidence pack) and hand
    # them back to the portal to surface to the requester.
    policy_warnings = verdict.get("warnings") or []
    if policy_warnings:
        append_audit(session, "policy.warnings", reference=req.reference,
                     detail={"warnings": policy_warnings})
    warnings = policy_warnings + budget_warnings

    session.commit()
    out = RequestOut.model_validate(req)
    out.policy_warnings = warnings
    return out


# --- Automatic sizing (increment 1.4) ----------------------------------------


class SizingIn(BaseModel):
    components: list[ComponentIn] = []


@app.post("/api/sizing")
def sizing(body: SizingIn, session: Session = Depends(get_session)) -> dict:
    """Resolve CPU/RAM/storage per component and the environment totals."""
    components = [
        {"technology_code": c.technology_code, "size": c.size} for c in body.components
    ]
    return resolve_components(components, session)


# --- Cost estimation (increment 1.5) -----------------------------------------


class CostIn(BaseModel):
    deployment_target: str | None = None
    components: list[ComponentIn] = []
    advanced_options: dict | None = None


@app.post("/api/cost")
def cost(body: CostIn, session: Session = Depends(get_session)) -> dict:
    """Estimate one-time/monthly/annual cost for the components on a target."""
    components = [
        {"technology_code": c.technology_code, "size": c.size} for c in body.components
    ]
    return estimate_cost(components, body.deployment_target, session, body.advanced_options)


@app.post("/api/cost/explain")
def explain_cost(
    body: CostIn,
    session: Session = Depends(get_session),
    requester: str = Depends(_authed_requester),
) -> dict:
    """Plain-English explanation of a request's cost (F-RPT-07).

    The AI **explains and suggests only** (ARCHITECTURE.md §7): it narrates the
    authoritative server-computed breakdown and offers advisory tips, but never
    changes the request, re-prices, submits, or provisions. Audited (ai.explained).
    """
    payload = {
        "deployment_target": body.deployment_target,
        "components": [
            {"technology_code": c.technology_code, "size": c.size} for c in body.components
        ],
        "advanced_options": body.advanced_options,
    }
    try:
        result = ai_explainer.explain_cost(payload, session)
    except ai_drafter.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    append_audit(
        session,
        "ai.explained",
        actor=requester,
        detail={"mode": result["mode"], "monthly": result["monthly"],
                "target": body.deployment_target},
    )
    session.commit()
    return result


# --- Approval + signed orchestrator handoff (increment 1.9) -------------------


def _jsonable(value):
    """Make a scalar field JSON-safe (dates -> ISO strings) for the policy gate
    and the signed orchestrator handoff."""
    return value.isoformat() if isinstance(value, date) else value


def _policy_input(req: Request) -> dict:
    data = {field: _jsonable(getattr(req, field)) for field in REQUEST_FIELDS}
    data["components"] = [
        {"technology_code": c.technology_code, "size": c.size} for c in req.components
    ]
    return {k: v for k, v in data.items() if v is not None}


def _load_approval(jira_key: str, session: Session) -> Approval:
    appr = session.scalar(select(Approval).where(Approval.jira_key == jira_key))
    if appr is None:
        raise HTTPException(status_code=404, detail=f"Approval '{jira_key}' not found.")
    return appr


@app.get("/api/approvals/{jira_key}")
def get_approval(jira_key: str, session: Session = Depends(get_session)) -> dict:
    """Approval status — used by the orchestrator to re-verify authority.

    In live mode this reflects the REAL Jira status (independent re-verification,
    §4); in mock mode it reflects our stored status.
    """
    appr = _load_approval(jira_key, session)
    status = appr.status
    if jira_mode() == "live":
        try:
            status = get_status(jira_key)
            appr.status = status
            session.commit()
        except JiraError:
            pass  # fall back to the stored status if Jira is momentarily unreachable
    return {"jira_key": appr.jira_key, "status": status, "reference": appr.request.reference}


def _sod_enforced() -> bool:
    """Whether segregation of duties is enforced (E1.2). On by default."""
    return os.getenv("SOD_ENFORCED", "true").strip().lower() in ("1", "true", "yes", "on")


def _check_sod(session: Session, req: Request, actor: str, action: str) -> None:
    """Segregation of duties (F-IAM-03): the person who raised a request may not
    approve/apply/destroy it. The autonomous poller runs _advance_request
    directly (no human actor), so it is never affected by this guard.
    """
    if _sod_enforced() and actor and actor == req.requester:
        append_audit(session, "sod.blocked", reference=req.reference, actor=actor,
                     detail={"action": action, "requester": req.requester})
        session.commit()  # persist the block even though the action is refused
        raise HTTPException(
            status_code=403,
            detail=(f"Segregation of duties (F-IAM-03): you raised {req.reference}, so you "
                    f"cannot {action} it. A different approver must act."),
        )


def _four_eyes_enforced() -> bool:
    return os.getenv("FOUR_EYES_ENFORCED", "true").strip().lower() in ("1", "true", "yes", "on")


def _same_person(approver: dict, requester_email: str) -> bool:
    """Whether the Jira approver is the requester (F-GOV-08). Matches by email if
    Jira exposes it, else bridges the email -> Jira-username via the RBAC resolver."""
    email = (approver.get("email") or "").strip().lower()
    if email and email == requester_email.strip().lower():
        return True
    name = (approver.get("name") or "").strip().lower()
    if name:
        bridged = (roles_mod.jira_username(requester_email) or "").strip().lower()
        if bridged and name == bridged:
            return True
    return False


def _four_eyes_ok(session: Session, req: Request) -> bool:
    """Four-eyes gate (F-GOV-08): the Jira approver must differ from the requester.
    Returns True if it's OK to proceed. Live-only; fails open if the approver
    can't be determined (a Jira read error) rather than blocking provisioning."""
    if not _four_eyes_enforced() or jira_mode() != "live" or req.approval is None:
        return True
    approver = get_approval_author(req.approval.jira_key)
    if approver is None or not _same_person(approver, req.requester):
        return True
    # Self-approval in Jira — block. Notify + audit once; re-evaluated each cycle
    # (a genuine re-approval by a different person will pass and unblock).
    if req.four_eyes_notified_at is None:
        try:
            add_comment(
                req.approval.jira_key,
                "👥 Four-eyes control: this request was approved by its own requester. "
                "A different approver must approve it before it can be provisioned.",
            )
        except JiraError:
            pass  # best-effort; the audit + status_detail still record the block
        req.four_eyes_notified_at = datetime.now(timezone.utc)
        req.status_detail = ("Blocked (four-eyes): approved by the requester; "
                             "a different approver is required.")
        append_audit(session, "four_eyes.blocked", reference=req.reference,
                     jira_key=req.approval.jira_key, actor="poller",
                     detail={"approver": approver.get("name") or approver.get("email"),
                             "requester": req.requester})
        session.commit()
    return False


def _approval_quorum() -> int:
    """How many distinct approvers a request needs before provisioning (F-GOV-06).
    Default 1 = today's single-approver behaviour."""
    try:
        return max(1, int(os.getenv("APPROVAL_QUORUM", "1")))
    except (ValueError, TypeError):
        return 1


def _quorum_ok(session: Session, req: Request) -> bool:
    """Approval quorum gate (F-GOV-06): require N *distinct* Jira approvers, none
    of them the requester, before provisioning. Returns True if OK to proceed.

    Re-verification, not decision: authority stays in Jira; the portal only counts
    the approvers Jira records and holds until there are enough. quorum<=1 is the
    single-approver default (no Jira call). Live only. Fails CLOSED — when the
    quorum can't be read it holds and re-checks next poll, rather than provisioning
    on incomplete assurance. Held once (audited once), unblocks when met.
    """
    quorum = _approval_quorum()
    if quorum <= 1 or jira_mode() != "live" or req.approval is None:
        return True
    # Distinct approvers, excluding any approval by the requester (reusing the
    # four-eyes identity match, so it lines up with SoD/four-eyes).
    distinct: dict[str, dict] = {}
    for a in get_approvers(req.approval.jira_key):
        if _same_person(a, req.requester):
            continue
        key = (a.get("email") or "").strip().lower() or (a.get("name") or "").strip().lower()
        if key:
            distinct[key] = a
    have = len(distinct)
    if have >= quorum:
        if req.quorum_held_at is not None:  # recovered from a hold — record it
            if req.status_detail and "approvals" in req.status_detail:
                req.status_detail = None
            append_audit(session, "quorum.met", reference=req.reference,
                         jira_key=req.approval.jira_key, actor="poller",
                         detail={"have": have, "required": quorum})
            session.commit()
        return True
    # Not enough distinct approvers yet — hold, audit once, re-check each poll.
    if req.quorum_held_at is None:
        req.quorum_held_at = datetime.now(timezone.utc)
        append_audit(session, "quorum.blocked", reference=req.reference,
                     jira_key=req.approval.jira_key, actor="poller",
                     detail={"have": have, "required": quorum})
    req.status_detail = (f"Awaiting approvals: {have} of {quorum} approved; "
                         f"{quorum - have} more required.")
    session.commit()
    return False


@app.post("/api/approvals/{jira_key}/approve")
def approve(jira_key: str, session: Session = Depends(get_session),
            _auth: str = Depends(require_action("execute"))):
    """Proceed with the signed orchestrator handoff once the request is approved.

    Approval authority lives in Jira (ARCHITECTURE.md §4). In live mode this
    reads the REAL Jira status and only proceeds if the manager has approved;
    the mock button stands in for that approval locally.
    """
    appr = _load_approval(jira_key, session)
    req = appr.request

    # Idempotency (F-ORC-01): never provision the same request twice.
    if req.status == "provisioned":
        return {
            "approval": "approved",
            "provisioned": True,
            "idempotent": True,
            "message": f"{req.reference} is already provisioned.",
        }

    _check_sod(session, req, _auth, "approve")

    if jira_mode() == "live":
        # Do not decide approval — read it from Jira.
        try:
            status = get_status(jira_key)
        except JiraError as exc:
            return JSONResponse(status_code=502, content={"error": f"Could not read Jira: {exc}"})
        appr.status = status
        if status != "approved":
            append_audit(session, "approval.checked", reference=req.reference,
                         jira_key=jira_key, detail={"status": status})
            session.commit()
            return {
                "approval": status,
                "provisioned": False,
                "message": f"Ticket {jira_key} is not approved in Jira (status: {status}).",
            }

    # Four-eyes (F-GOV-08): refuse if the Jira approver is the requester.
    if not _four_eyes_ok(session, req):
        return JSONResponse(status_code=409,
                            content={"approval": "approved", "provisioned": False,
                                     "error": req.status_detail})

    # Approval quorum (F-GOV-06): refuse until enough distinct approvers signed off.
    if not _quorum_ok(session, req):
        return JSONResponse(status_code=409,
                            content={"approval": "approved", "provisioned": False,
                                     "error": req.status_detail})

    appr.status = "approved"
    append_audit(session, "approval.approved", reference=req.reference, jira_key=jira_key,
                 actor="jira")
    session.commit()

    # Decommission requests tear down the referenced resources instead of
    # provisioning new ones (2.9).
    if req.request_type == "decommission":
        return _decommission(session, req, actor="approver")

    # Refresh copies data from a higher env into the target (F-LCM-03).
    if req.request_type == "refresh":
        return _refresh(session, req, actor="approver")

    # Restore rolls the target back to one of its backups (F-LCM-06).
    if req.request_type == "restore":
        return _restore(session, req, actor="approver")

    # Signed, versioned handoff. The signature is authenticity; the orchestrator
    # re-checks authority (approval + policy) and re-validates cost itself.
    approved_monthly = float(req.estimate.monthly) if req.estimate else None
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": jira_key,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": jira_key,
        "reference": req.reference,
        "policy_input": _policy_input(req),
        "approved_monthly": approved_monthly,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    signature = sign(WEBHOOK_SECRET, body)
    append_audit(session, "orchestrator.handoff", reference=req.reference, jira_key=jira_key,
                 detail={"orchestrator": ORCHESTRATOR_URL, "contract": CONTRACT_VERSION})
    session.commit()

    response, error = _post_to_orchestrator(body, signature)
    if response is None:
        append_audit(session, "orchestrator.unreachable", reference=req.reference,
                     jira_key=jira_key, detail={"error": error})
        session.commit()
        return JSONResponse(
            status_code=503,
            content={"error": f"Orchestrator unreachable after retries — not provisioned ({error})."},
        )

    if response.status_code != 200:
        append_audit(session, "orchestrator.refused", reference=req.reference,
                     jira_key=jira_key, detail={"status": response.status_code,
                                                "body": response.text})
        session.commit()
        return JSONResponse(status_code=response.status_code,
                            content={"error": "Orchestrator refused the handoff.",
                                     "detail": response.text})

    result = response.json()
    if not result.get("provisioned"):
        # Plan-only (2.6a): a preview came back, nothing was created.
        req.status = "planned"
        append_audit(session, "plan.previewed", reference=req.reference, jira_key=jira_key,
                     detail={"plan_summary": result.get("plan_summary")})
        session.commit()
        return {
            "approval": "approved",
            "provisioned": False,
            "planned": True,
            "message": result.get("message"),
            "result": result,
        }

    req.status = "provisioned"
    append_audit(session, "provisioned", reference=req.reference, jira_key=jira_key,
                 detail=result)
    session.commit()
    return {"approval": "approved", "provisioned": True, "result": result}


@app.get("/api/requests/{reference}/audit")
def request_audit(reference: str, session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("view_audit"))) -> dict:
    """Return the audit trail for one request (append-only, hash-chained)."""
    rows = session.scalars(
        select(AuditLog).where(AuditLog.reference == reference).order_by(AuditLog.id)
    ).all()
    return {
        "reference": reference,
        "entries": [
            {
                "event": r.event,
                "actor": r.actor,
                "jira_key": r.jira_key,
                "detail": r.detail,
                "entry_hash": r.entry_hash,
                "prev_hash": r.prev_hash,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@app.post("/api/requests/{reference}/triage")
def triage_failure(
    reference: str,
    session: Session = Depends(get_session),
    requester: str = Depends(_authed_requester),
) -> dict:
    """AI failure triage for a request (F-RPT-08). Platform-admin only.

    Reads the request's real failure signals (status, status_detail, and its
    failure audit events) and returns a plain-English diagnosis + next steps. The
    AI **diagnoses and advises only** (ARCHITECTURE.md §7) — it never retries,
    applies, re-provisions, or changes the request. Audited (ai.triaged).
    """
    if roles_mod.PLATFORM_ADMIN not in roles_mod.resolve_roles(requester):
        raise HTTPException(
            status_code=403, detail="Only a platform administrator can triage failures."
        )
    req = _load_request(reference, session)
    try:
        result = ai_triage.triage_request(req, session)
    except ai_drafter.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    append_audit(
        session, "ai.triaged", reference=reference, actor=requester,
        detail={"mode": result["mode"], "status": req.status, "failing": result["failing"]},
    )
    session.commit()
    return result


@app.get("/api/audit/verify")
def audit_verify(session: Session = Depends(get_session),
                 _auth: str = Depends(require_action("view_audit"))) -> dict:
    """Verify the append-only audit chain is intact (F-SEC-01).

    Recomputes every entry's hash and checks the prev-hash linkage, so any edit,
    deletion, insertion or reorder is reported with the offending entry.
    """
    return verify_chain(session)


# --- Lifecycle event stream (F-INT-02) ---------------------------------------

@app.get("/api/events")
def events_feed(
    since: int = 0,
    limit: int = 100,
    tail: int | None = None,
    reference: str | None = None,
    types: str | None = None,
    all: bool = False,
    session: Session = Depends(get_session),
    _auth: str = Depends(require_action("view_overview")),
) -> dict:
    """Curated request/environment lifecycle events (F-INT-02), read-only.

    Consumers tail the monotonic `cursor` (`?since=`) for gap-free, at-least-once
    delivery; `?tail=N` returns the most recent N for an initial load. Filter with
    `?reference=`, `?types=a,b`, or `?all=true`. Sourced from the tamper-evident
    audit log — this exposes events already recorded and changes nothing.
    """
    type_list = types.split(",") if types else None
    events, cursor = eventstream.lifecycle_events(
        session, since=since, limit=limit, reference=reference,
        types=type_list, include_all=all, tail=tail,
    )
    return {"events": events, "cursor": cursor}


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str, session: Session = Depends(get_session),
              _auth: str = Depends(require_action("view_audit"))) -> dict:
    """Every audit step recorded under one trace id (F-OPS-04) — follow a single
    request across the portal, API, and orchestrator, in order."""
    rows = session.scalars(
        select(AuditLog).where(AuditLog.trace_id == trace_id).order_by(AuditLog.id)
    ).all()
    return {"trace_id": trace_id, "count": len(rows),
            "steps": [{"id": r.id, "event": r.event, "reference": r.reference, "actor": r.actor,
                       "jira_key": r.jira_key, "detail": r.detail,
                       "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}


def _fetch_events(cursor: int, type_list: list[str] | None, reference: str | None) -> tuple[list[dict], int]:
    """One poll for the SSE stream, in its own session (runs off the event loop)."""
    with SessionLocal() as session:
        return eventstream.lifecycle_events(
            session, since=cursor, limit=eventstream.MAX_LIMIT,
            reference=reference, types=type_list,
        )


@app.get("/api/events/stream")
async def events_stream(
    http_request: HTTPRequest,
    since: int = 0,
    reference: str | None = None,
    types: str | None = None,
    last_event_id: str | None = Header(default=None),
    _auth: str = Depends(require_action("view_overview")),
):
    """Real-time Server-Sent Events of the lifecycle feed (F-INT-02).

    Resumes from `Last-Event-ID` (or `?since=`); each frame's `id:` is the cursor.
    Heartbeats keep the connection alive; it exits cleanly on client disconnect.
    """
    type_list = types.split(",") if types else None
    start = int(last_event_id) if (last_event_id or "").isdigit() else since

    async def gen():
        cursor = start
        yield ": connected\n\n"
        while True:
            if await http_request.is_disconnected():
                break
            events, cursor = await anyio.to_thread.run_sync(
                _fetch_events, cursor, type_list, reference
            )
            for event in events:
                yield eventstream.sse_frame(event)
            if not events:
                yield ": ping\n\n"  # heartbeat
            await anyio.sleep(2.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- ChatOps approvals bot (F-INT-08) ----------------------------------------

class ChatOpsIn(BaseModel):
    command: str


@app.post("/api/chatops")
def chatops(
    body: ChatOpsIn,
    session: Session = Depends(get_session),
    requester: str = Depends(_authed_requester),
) -> dict:
    """Run a ChatOps command as the signed-in user (F-INT-08).

    Powers the in-portal Assistant console and any authenticated API caller. The
    bot holds no authority — it runs as the caller's identity/roles; approve and
    reject record the decision in Jira and are audited (see api/chatbot.py).
    """
    return chatbot.handle_command(body.command, requester, session)


@app.post("/api/chatops/slack")
async def chatops_slack(http_request: HTTPRequest, session: Session = Depends(get_session)) -> dict:
    """Slack slash-command adapter (F-INT-08), live only.

    Verifies the Slack signing secret, maps the Slack user to a portal identity
    (CHATOPS_SLACK_MAP), then runs the same command engine. Replies ephemerally.
    """
    raw = await http_request.body()
    if not chatbot.verify_slack(raw, http_request.headers):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured Slack signature.")
    form = urllib.parse.parse_qs(raw.decode("utf-8", "ignore"))
    text = (form.get("text", [""])[0]).strip()
    identity = chatbot.slack_identity(form.get("user_id", [""])[0], form.get("user_name", [""])[0])
    if identity is None:
        return {"response_type": "ephemeral",
                "text": "Your Slack account isn't linked to a portal identity. Ask an admin to map it."}
    result = chatbot.handle_command(text, identity, session)
    return {"response_type": "ephemeral", "text": result["response"]}


@app.get("/api/requests/{reference}/evidence.pdf")
def request_evidence(reference: str, session: Session = Depends(get_session),
                     _auth: str = Depends(require_action("view_audit"))) -> Response:
    """Download the governance evidence pack for a request (F-GOV-10): request +
    cost + approval + full audit trail + a re-verified tamper-evidence attestation."""
    req = _load_request(reference, session)
    components = [{"technology_code": c.technology_code, "size": c.size} for c in req.components]
    if req.estimate and req.estimate.breakdown:
        breakdown = req.estimate.breakdown
    else:
        breakdown = estimate_cost(components, req.deployment_target, session, req.advanced_options)
    sizing = resolve_components(components, session)
    rows = session.scalars(
        select(AuditLog).where(AuditLog.reference == reference).order_by(AuditLog.id)
    ).all()
    audit = [{"when": r.created_at.isoformat(), "event": r.event, "actor": r.actor,
              "detail": r.detail} for r in rows]
    pdf = build_evidence_pdf(req, breakdown, sizing, audit, verify_chain(session))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="evidence-{reference}.pdf"'},
    )


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/requests/{reference}/costsheet.xlsx")
def request_costsheet(reference: str, session: Session = Depends(get_session)) -> Response:
    """Download the request's cost sheet as an Excel file (6.4).

    Uses the estimate captured at submission when present (the authoritative
    snapshot), otherwise computes a fresh estimate — so drafts work too. Same
    open access as GET /api/requests/{reference}, which already exposes the cost.
    """
    req = _load_request(reference, session)
    components = [{"technology_code": c.technology_code, "size": c.size} for c in req.components]
    if req.estimate and req.estimate.breakdown:
        breakdown = req.estimate.breakdown
    else:
        breakdown = estimate_cost(components, req.deployment_target, session)
    sizing = resolve_components(components, session)
    xlsx = build_cost_sheet_xlsx(req, breakdown, sizing)
    return Response(
        content=xlsx,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="cost-{reference}.xlsx"'},
    )


# --- Real apply / destroy (increment 2.6b) -----------------------------------


def _short_reason(text: str, limit: int = 300) -> str:
    """Pull a concise, human-readable reason out of an orchestrator/terraform
    error blob, for the portal and a Jira comment. Falls back to a trim."""
    if not text:
        return "Provisioning failed."
    # The orchestrator wraps terraform failures as JSON {"detail": "..."}.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("detail"):
            text = parsed["detail"]
    except (ValueError, TypeError):
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Error:"):  # terraform's own headline
            return line[:limit]
    return " ".join(text.split())[:limit]


def _handoff_payload(req: Request, *, ttl_expiry: str | None = None,
                     action: str | None = None) -> tuple[bytes, str]:
    """Build and sign the orchestrator handoff for a request."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": req.approval.jira_key,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "policy_input": _policy_input(req),
        "approved_monthly": float(req.estimate.monthly) if req.estimate else None,
        # Which cloud resource to provision/act on: oci-bucket | oci-instance.
        "resource_kind": req.resource_kind or "oci-bucket",
    }
    if ttl_expiry:
        payload["ttl_expiry"] = ttl_expiry
    if action:  # operational actuation (stop/start), cloud-sync increment 2
        payload["action"] = action
    body = json.dumps(payload, sort_keys=True).encode()
    return body, sign(WEBHOOK_SECRET, body)


def _transition_jira(session: Session, req: Request, target: str, event: str) -> None:
    """Best-effort Jira status transition + audit. Never blocks provisioning."""
    if jira_mode() == "live":
        try:
            # The Resolve transition needs required fields (Solution, Closure Reason).
            fields = resolve_fields() if target == resolved_status() else None
            transition_issue(req.approval.jira_key, target, fields=fields)
            append_audit(session, event, reference=req.reference, jira_key=req.approval.jira_key,
                         detail={"jira_status": target})
        except JiraError as exc:
            append_audit(session, f"{event}.jira_failed", reference=req.reference,
                         jira_key=req.approval.jira_key, detail={"error": str(exc)})
    else:
        append_audit(session, event, reference=req.reference, jira_key=req.approval.jira_key,
                     detail={"jira_status": target})


def _provision_in_background(reference: str, jira_key: str, body: bytes, signature: str,
                            ttl_expiry_iso: str) -> None:
    """Run the real apply, then set Jira Resolved + status provisioned (or failed)."""
    with SessionLocal() as session:
        req = session.scalar(select(Request).where(Request.reference == reference))
        if req is None:
            return
        response, error = _post_to_orchestrator(body, signature, path="/apply")
        if response is None or response.status_code != 200:
            raw = error or (response.text if response else "")
            reason = _short_reason(raw)
            req.status = "apply-failed"
            req.status_detail = reason
            append_audit(session, "apply.failed", reference=reference, jira_key=jira_key,
                         detail={"error": raw})
            # Leave the approver a note on the ticket rather than a silent stall.
            add_comment(jira_key, "⚠️ Automated provisioning failed and no resources "
                                  f"were created.\n\n{reason}")
            session.commit()
            return

        result = response.json()
        res = result.get("resource", {})
        session.add(ProvisionedResource(
            reference=reference, kind=res.get("kind", "resource"), name=res.get("name", ""),
            region=res.get("region"), details=res.get("outputs", {}),
            ttl_expiry=datetime.fromisoformat(ttl_expiry_iso) if ttl_expiry_iso else None,
            lifecycle_state="active",
        ))
        _transition_jira(session, req, resolved_status(), "jira.resolved")
        req.status = "provisioned"
        req.status_detail = None
        append_audit(session, "provisioned", reference=reference, jira_key=jira_key, detail=result)
        session.commit()


@app.post("/api/requests/{reference}/apply")
def apply_request(reference: str, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("execute"))):
    """Start provisioning: set Jira In Progress, then apply in the background.

    Returns immediately with status 'in-progress' so the portal can show live
    progress; the background task creates the resource and sets Jira Resolved.
    """
    req = _load_request(reference, session)
    if req.approval is None:
        raise HTTPException(status_code=400, detail="Request has no approval to apply.")
    if req.status in ("in-progress", "provisioned"):
        return {"status": req.status, "message": f"{reference} is already {req.status}."}

    _check_sod(session, req, _auth, "apply")

    if req.status != "planned":
        return JSONResponse(
            status_code=409,
            content={"error": "Request must be approved and planned before it can be applied."},
        )

    ttl_dt = _ttl_for(req)  # non-prod gets a TTL; prod/dr exempt (F-FIN-07)
    ttl_iso = ttl_dt.isoformat() if ttl_dt else None
    body, signature = _handoff_payload(req, ttl_expiry=ttl_iso)
    # Move Jira to In Progress and mark the request in-progress up front.
    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    req.status = "in-progress"
    append_audit(session, "provisioning.started", reference=reference,
                 jira_key=req.approval.jira_key, actor="approver")
    session.commit()

    background_tasks.add_task(_provision_in_background, reference, req.approval.jira_key,
                             body, signature, ttl_iso)
    return {"status": "in-progress", "message": f"Provisioning {reference} started."}


@app.post("/api/requests/{reference}/destroy")
def destroy_request(reference: str, session: Session = Depends(get_session),
                    _auth: str = Depends(require_action("execute"))):
    """Destroy the resources created for a request (rollback / cleanup)."""
    req = _load_request(reference, session)
    if req.approval is None:
        raise HTTPException(status_code=400, detail="Request has no approval.")

    _check_sod(session, req, _auth, "destroy")

    body, signature = _handoff_payload(req)
    append_audit(session, "destroy.handoff", reference=reference, jira_key=req.approval.jira_key,
                 actor="approver")
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/destroy")
    if response is None:
        return JSONResponse(status_code=503, content={"error": f"Orchestrator unreachable ({error})."})
    if response.status_code != 200:
        append_audit(session, "destroy.failed", reference=reference,
                     jira_key=req.approval.jira_key, detail={"body": response.text})
        session.commit()
        return JSONResponse(status_code=response.status_code,
                            content={"error": "Destroy failed.", "detail": response.text})

    for res in session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.lifecycle_state == "active",
        )
    ):
        res.lifecycle_state = "decommissioned"
    req.status = "decommissioned"
    append_audit(session, "destroyed", reference=reference, jira_key=req.approval.jira_key,
                 detail=response.json())
    session.commit()
    return {"destroyed": True, "message": response.json().get("summary")}


@app.post("/api/requests/{reference}/renew")
def renew_request(reference: str, days: int | None = None,
                  session: Session = Depends(get_session),
                  actor: str = Depends(require_action("execute"))):
    """Extend a non-prod environment's TTL (F-FIN-07).

    Gated to platform_admin. Resets the expiry of the environment's active
    resources to now + `days` (default the non-prod lifetime), clears the
    expiring/expired flag so the poller can warn again next cycle, and records a
    tamper-evident `ttl.renewed` event.
    """
    req = _load_request(reference, session)
    resources = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.ttl_expiry.is_not(None),
        )
    ).all()
    if not resources:
        raise HTTPException(
            status_code=400,
            detail="Nothing to renew: this request has no active resource with a TTL.")
    extend = _ttl_days_nonprod() if not days else max(1, int(days))
    new_expiry = datetime.now(timezone.utc) + timedelta(days=extend)
    for res in resources:
        res.ttl_expiry = new_expiry
    req.ttl_notified = None  # let the poller warn again as the new expiry nears
    if req.status_detail and ("xpir" in req.status_detail):  # "Expiring"/"Expired"
        req.status_detail = None
    append_audit(session, "ttl.renewed", reference=reference,
                 jira_key=req.approval.jira_key if req.approval else None, actor=actor,
                 detail={"new_expiry": new_expiry.isoformat(), "days": extend})
    session.commit()
    return {"renewed": True, "reference": reference,
            "new_expiry": new_expiry.isoformat(), "days": extend}


@app.post("/api/requests/{reference}/drift-check")
def drift_check(reference: str, session: Session = Depends(get_session),
                actor: str = Depends(require_action("view_overview"))):
    """Check a provisioned environment for drift from its applied state (F-LCM-09).

    Posts a signed handoff to the orchestrator, which re-plans and reports any
    changes (read-only — creates nothing). Records the result + a drift.detected /
    drift.none audit on the request. Oversight-gated.
    """
    req = _load_request(reference, session)
    if req.status != "provisioned" or req.approval is None:
        raise HTTPException(status_code=400, detail="Drift checks apply to provisioned requests.")
    body, signature = _handoff_payload(req)
    response, error = _post_to_orchestrator(body, signature, path="/drift")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        return JSONResponse(status_code=502,
                            content={"error": f"Drift check failed: {_short_reason(raw)}"})
    result = response.json()
    drifted = bool(result.get("drift"))
    req.drift_detected = drifted
    req.drift_checked_at = datetime.now(timezone.utc)
    if drifted:
        req.status_detail = (f"Drift detected: {result.get('count', 0)} resource change(s) "
                             f"vs the approved state.")
    elif req.status_detail and "drift" in req.status_detail.lower():
        req.status_detail = None
    append_audit(session, "drift.detected" if drifted else "drift.none",
                 reference=reference, jira_key=req.approval.jira_key, actor=actor,
                 detail={"changes": result.get("changes", []), "summary": result.get("summary")})
    session.commit()
    return {"reference": reference, "drift": drifted,
            "changes": result.get("changes", []), "summary": result.get("summary")}


# --- Read-only cloud state sync (reconciliation) -----------------------------

def _reconcile(session: Session, req: Request) -> dict:
    """Compare the portal's registry against the actual cloud state (read-only).

    Asks the orchestrator (signed handoff) for the real state of the request's
    resources and diffs it against the active ProvisionedResource rows. Returns
    {in_sync, resources, divergences} or {error}. Observes only — changes nothing.
    """
    registry = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == req.reference,
            ProvisionedResource.lifecycle_state == "active",
        )
    ).all()
    body, signature = _handoff_payload(req)
    response, error = _post_to_orchestrator(body, signature, path="/state")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        return {"error": _short_reason(raw)}
    actual = response.json().get("resources", [])
    by_name = {a.get("name"): a for a in actual}
    divergences = []
    for res in registry:
        found = by_name.get(res.name)
        if found is None or not found.get("exists", True):
            divergences.append({"resource": res.name, "issue": "missing",
                                "detail": "not found in the cloud (deleted out-of-band?)"})
        elif found.get("status") not in ("active", None):
            divergences.append({"resource": res.name, "issue": str(found.get("status")),
                                "detail": f"cloud reports '{found.get('status')}'"})
    return {"in_sync": not divergences, "resources": actual, "divergences": divergences}


def _apply_reconcile(session: Session, req: Request, result: dict, actor: str) -> None:
    """Persist a reconcile result on the request + audit a change of state."""
    drifted = not result["in_sync"]
    was = req.state_status
    req.state_status = "drifted" if drifted else "in-sync"
    req.state_synced_at = datetime.now(timezone.utc)
    if drifted:
        req.status_detail = (f"Cloud state drift: {len(result['divergences'])} resource(s) "
                             "diverged from the registry.")
    elif req.status_detail and "cloud state" in req.status_detail.lower():
        req.status_detail = None
    if was != req.state_status:  # audit only on a change, to avoid per-cycle noise
        append_audit(session, "state.drift" if drifted else "state.reconciled",
                     reference=req.reference,
                     jira_key=req.approval.jira_key if req.approval else None, actor=actor,
                     detail={"divergences": result["divergences"]})


@app.post("/api/requests/{reference}/reconcile")
def reconcile_state(reference: str, session: Session = Depends(get_session),
                    actor: str = Depends(require_action("view_overview"))):
    """Reconcile a provisioned environment against actual cloud state (read-only).

    Flags any out-of-band change (a resource stopped, resized, or deleted directly
    in OCI/Azure). Records the result + a state.reconciled / state.drift audit.
    Oversight-gated. Observes only — it never changes cloud state.
    """
    req = _load_request(reference, session)
    if req.status != "provisioned" or req.approval is None:
        raise HTTPException(status_code=400, detail="Reconciliation applies to provisioned requests.")
    result = _reconcile(session, req)
    if "error" in result:
        return JSONResponse(status_code=502, content={"error": f"State sync failed: {result['error']}"})
    _apply_reconcile(session, req, result, actor)
    session.commit()
    return {"reference": reference, **result}


@app.get("/api/state")
def state_overview(session: Session = Depends(get_session),
                   _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Cloud-state sync status across the estate (read-only). Each provisioned
    environment's last reconciliation result + the estate roll-up."""
    envs = []
    counts: dict[str, int] = {}
    for req in session.scalars(select(Request).where(Request.status == "provisioned").order_by(Request.id)):
        st = req.state_status or "unknown"
        counts[st] = counts.get(st, 0) + 1
        envs.append({
            "reference": req.reference,
            "environment": req.environment_name or req.target_environment,
            "state_status": st,
            "synced_at": req.state_synced_at.isoformat() if req.state_synced_at else None,
        })
    return {
        "sync_enabled": _cloud_state_sync_enabled(),
        "mode": os.getenv("CLOUD_STATE_MODE", "mock"),
        "count": len(envs),
        "by_status": counts,
        "environments": envs,
    }


def _actuate_env(session: Session, req: Request, action: str, actor: str) -> dict:
    """Stop/start a provisioned environment's compute resources through the
    orchestrator, updating power_state + auditing. Does NOT commit — the caller
    owns the transaction. Shared by the Stop/Start endpoint and the auto-shutdown
    sweep (F-FIN-06), so a manual click and the scheduler can never diverge.

    Only compute (oci-instance) is stoppable — buckets are skipped. Idempotent: a
    no-op if already in the target state. Returns {power, ...} or {error}/{skipped}.
    """
    resources = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == req.reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.kind == "oci-instance",
        )
    ).all()
    if not resources:
        return {"reference": req.reference, "action": action, "skipped": "no compute resources"}
    target = "stopped" if action == "stop" else "running"
    if all((r.power_state or "running") == target for r in resources):
        return {"reference": req.reference, "action": action, "power": target, "idempotent": True}

    sig_body, signature = _handoff_payload(req, action=action)
    response, error = _post_to_orchestrator(sig_body, signature, path="/actuate")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        return {"reference": req.reference, "action": action, "error": _short_reason(raw)}

    reported = {r.get("name"): r.get("power_state") for r in response.json().get("resources", [])}
    for r in resources:
        r.power_state = reported.get(r.name, target)
    # Remember only an auto-shutdown stop, so the sweep re-starts what IT stopped
    # and never a manual stop. Any start (or a manual stop) clears the flag.
    req.auto_stopped = (action == "stop" and actor == "auto-shutdown")
    append_audit(session, "resource.stopped" if action == "stop" else "resource.started",
                 reference=req.reference,
                 jira_key=req.approval.jira_key if req.approval else None, actor=actor,
                 detail={"resources": [r.name for r in resources], "power_state": target,
                         "by": actor})
    return {"reference": req.reference, "action": action,
            "power": _derive_power([r.power_state for r in resources])}


class ActuateIn(BaseModel):
    action: str  # stop | start


@app.post("/api/requests/{reference}/actuate")
def actuate_request(reference: str, body: ActuateIn,
                    session: Session = Depends(get_session),
                    actor: str = Depends(require_action("execute"))) -> dict:
    """Stop or start a provisioned environment's compute from the portal (cloud-sync
    increment 2 — actuation). A reversible operational action, gated to
    platform_admin under a standing operational policy (no per-action Jira ticket)
    and fully audited. It goes through the orchestrator (signed handoff) — the
    execute layer that holds the cloud credentials and re-verifies — never the API
    directly. Mock models the action and changes no real cloud; live calls the
    provider APIs. Idempotent: acting when already in the target state is a no-op."""
    req = _load_request(reference, session)
    if req.status != "provisioned" or req.approval is None:
        raise HTTPException(status_code=400,
                            detail="Only provisioned environments can be stopped or started.")
    action = (body.action or "").strip().lower()
    if action not in ("stop", "start"):
        raise HTTPException(status_code=422, detail="action must be 'stop' or 'start'.")

    result = _actuate_env(session, req, action, actor)
    if result.get("skipped"):
        raise HTTPException(status_code=400,
                            detail="This environment has no stoppable compute resources.")
    if "error" in result:
        return JSONResponse(status_code=502,
                            content={"error": f"{action.title()} failed: {result['error']}"})
    session.commit()
    out = {"reference": reference, "action": action, "power": result.get("power")}
    if result.get("idempotent"):
        out["idempotent"] = True
        out["message"] = f"{reference} is already {result['power']}."
    return out


class RequestShutdownIn(BaseModel):
    enabled: bool | None = None
    days: str | None = None
    start: str | None = None
    end: str | None = None
    tz: str | None = None
    clear: bool = False  # remove the override (inherit the global schedule)

    @field_validator("start", "end")
    @classmethod
    def _valid_hm(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            hh, mm = (v or "").strip().split(":")
            hh, mm = int(hh), int(mm)
            assert 0 <= hh <= 23 and 0 <= mm <= 59
            return f"{hh:02d}:{mm:02d}"
        except Exception:
            raise ValueError("time must be HH:MM (00:00–23:59)")

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo((v or "").strip())
            return v.strip()
        except Exception:
            raise ValueError("tz must be a valid IANA timezone, e.g. 'Asia/Dubai'")

    @field_validator("days")
    @classmethod
    def _clean_days(cls, v: str | None) -> str | None:
        return (v or "").strip().lower() or None


@app.put("/api/requests/{reference}/shutdown")
def set_request_shutdown(reference: str, body: RequestShutdownIn,
                         session: Session = Depends(get_session),
                         actor: str = Depends(require_action("execute"))) -> dict:
    """Set or clear a request's per-request auto-shutdown override (F-FIN-06
    increment B), platform_admin. The override is MERGED over the global policy —
    the fields it sets win, the rest inherit global. `clear=true` removes it
    (inherit global entirely). Audited."""
    req = _load_request(reference, session)
    if body.clear:
        req.shutdown_override = None
    else:
        override = {k: v for k, v in body.model_dump(exclude={"clear"}).items() if v is not None}
        req.shutdown_override = override or None
    append_audit(session, "shutdown.override.set", reference=reference, actor=actor,
                 detail={"override": req.shutdown_override})
    session.commit()
    return {"reference": reference, "override": req.shutdown_override,
            "effective": autoshutdown.effective_policy(session, req)}


# --- Backup & restore (F-LCM-06) ---------------------------------------------

def _owner_or_admin(req: Request, actor: str) -> bool:
    """Owner self-service: a platform_admin, a member of the owning GROUP
    (F-IAM-09), or a named individual owner / the requester."""
    if roles_mod.can(roles_mod.resolve_roles(actor), "execute"):
        return True
    if _in_owner_group(actor, req):
        return True
    owners = {req.requester, _resolve_owner(req), req.environment_owner,
              req.application_owner, req.technical_owner, req.business_owner}
    return actor in {o for o in owners if o}


class BackupIn(BaseModel):
    label: str | None = None


@app.post("/api/requests/{reference}/backup")
def create_backup(reference: str, body: BackupIn, session: Session = Depends(get_session),
                  actor: str = Depends(_authed_requester)) -> dict:
    """Take a backup restore-point of a provisioned environment (F-LCM-06). Owner
    self-service — the env owner or a platform_admin, NO Jira approval (approval
    governs the restore, not the backup). Mock records metadata; a real snapshot is
    an orchestrator extension point. Audited `backup.created`."""
    req = _load_request(reference, session)
    if req.status != "provisioned":
        raise HTTPException(status_code=400, detail="Only a provisioned environment can be backed up.")
    if not _owner_or_admin(req, actor):
        raise HTTPException(status_code=403,
                            detail="Only the environment owner or a platform administrator can back it up.")
    label = (body.label or "").strip() or f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    backup = Backup(reference=reference, label=label, created_by=actor,
                    details={"mode": os.getenv("BACKUP_MODE", "mock")})
    session.add(backup)
    append_audit(session, "backup.created", reference=reference, actor=actor, detail={"label": label})
    session.commit()
    return _backup_row(backup)


@app.get("/api/requests/{reference}/backups")
def list_backups(reference: str, session: Session = Depends(get_session),
                 _auth: str = Depends(_authed_requester)) -> dict:
    """The provisioned environment's backup restore-points, newest first."""
    _load_request(reference, session)
    rows = session.scalars(
        select(Backup).where(Backup.reference == reference).order_by(Backup.id.desc())
    ).all()
    return {"reference": reference, "backups": [_backup_row(b) for b in rows]}


def _restore_handoff(req: Request, target: Request, backup: Backup) -> tuple[bytes, str]:
    """Build + sign the restore handoff — the target env + the backup to restore."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": f"{req.approval.jira_key}:restore:{backup.id}",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "operation": "restore",
        "target": {"reference": target.reference, "policy_input": _policy_input(target)},
        "backup": {"id": backup.id, "label": backup.label},
    }
    body = json.dumps(payload, sort_keys=True).encode()
    return body, sign(WEBHOOK_SECRET, body)


def _restore(session: Session, req: Request, actor: str) -> dict:
    """Restore the target provisioned env (source_reference) to a chosen backup
    (restore_backup_id), and record the verification result (F-LCM-06).
    Approval-governed + orchestrator-executed, mirroring decommission/refresh. Mock
    records it (verified); live is a restore extension point. The target stays
    provisioned."""
    target = session.scalar(select(Request).where(Request.reference == req.source_reference))
    backup = session.get(Backup, req.restore_backup_id) if req.restore_backup_id else None
    if target is None or backup is None or backup.reference != req.source_reference:
        req.status = "restore-failed"
        req.status_detail = "Restore target or backup not found (or mismatched)."
        append_audit(session, "restore.source_missing", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"target": req.source_reference, "backup_id": req.restore_backup_id})
        session.commit()
        return {"approval": "approved", "restored": False, "error": req.status_detail}

    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    session.commit()

    body, signature = _restore_handoff(req, target, backup)
    append_audit(session, "restore.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"target": target.reference, "backup": backup.label})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/restore")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "restore-failed"
        req.status_detail = reason
        append_audit(session, "restore.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"target": target.reference, "backup": backup.label, "error": raw})
        add_comment(req.approval.jira_key, "⚠️ Automated restore failed; nothing was "
                                           f"changed.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "restored": False, "error": reason}

    result = response.json()
    verified = bool(result.get("verified"))
    summary = result.get("summary")
    req.status = "restored"
    req.status_detail = None
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    append_audit(session, "restore.performed", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"target": target.reference, "backup": backup.label,
                         "verified": verified, "summary": summary})
    session.commit()
    return {"approval": "approved", "restored": True, "target": target.reference,
            "backup": backup.label, "verified": verified,
            "message": f"Restored {target.reference} from '{backup.label}': {summary}"}


# --- Just-in-time access + vault credential delivery (F-IAM-07 / F-INT-05) ---

_ACCESS_SCOPES = {"ssh", "db-read", "db-admin", "read-only", "admin"}
_ACCESS_MAX_TTL_HOURS = 72


def _access_row(g: AccessGrant) -> dict:
    """Grant metadata for the portal — NEVER the credential/link."""
    return {"id": g.id, "grantee": g.grantee, "scope": g.scope, "granted_by": g.granted_by,
            "granted_at": g.granted_at.isoformat() if g.granted_at else None,
            "expires_at": g.expires_at.isoformat() if g.expires_at else None, "status": g.status}


def _access_out(session: Session, req: Request) -> list | None:
    """Active JIT access grants on a provisioned env, for the portal (no secrets)."""
    if req.status != "provisioned":
        return None
    now = datetime.now(timezone.utc)
    rows = session.scalars(
        select(AccessGrant).where(AccessGrant.reference == req.reference,
                                  AccessGrant.status == "active", AccessGrant.expires_at > now)
        .order_by(AccessGrant.id.desc())
    ).all()
    return [_access_row(g) for g in rows]


class AccessGrantIn(BaseModel):
    grantee: str
    scope: str = "read-only"
    ttl_hours: int = 4

    @field_validator("grantee")
    @classmethod
    def _clean_grantee(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("grantee is required")
        return v.strip()

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _ACCESS_SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(sorted(_ACCESS_SCOPES))}")
        return v

    @field_validator("ttl_hours")
    @classmethod
    def _valid_ttl(cls, v: int) -> int:
        if not (1 <= int(v) <= _ACCESS_MAX_TTL_HOURS):
            raise ValueError(f"ttl_hours must be 1–{_ACCESS_MAX_TTL_HOURS}")
        return int(v)


@app.post("/api/requests/{reference}/access")
def grant_access(reference: str, body: AccessGrantIn, session: Session = Depends(get_session),
                 actor: str = Depends(require_action("grant_access"))) -> dict:
    """Grant TIME-BOUND just-in-time access to a provisioned environment (F-IAM-07),
    gated to approver/platform_admin. The vault mints a short-lived credential and
    returns a ONE-TIME link (F-INT-05) — shown once here, never stored or logged.
    The portal records only the grant metadata + a vault handle. Audited
    `access.granted` (never the secret)."""
    req = _load_request(reference, session)
    if req.status != "provisioned":
        raise HTTPException(status_code=400,
                            detail="Access can only be granted to a provisioned environment.")
    try:
        cred = vault.issue_credential(reference, body.grantee, body.scope, body.ttl_hours)
    except vault.VaultUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    grant = AccessGrant(reference=reference, grantee=body.grantee, scope=body.scope,
                        granted_by=actor, expires_at=datetime.fromisoformat(cred["expires_at"]),
                        status="active", vault_handle=cred.get("handle"))
    session.add(grant)
    append_audit(session, "access.granted", reference=reference, actor=actor,
                 detail={"grantee": body.grantee, "scope": body.scope,
                         "expires_at": cred["expires_at"], "handle": cred.get("handle")})
    session.commit()
    # The credential link is returned ONCE and never persisted portal-side.
    return {"grant": _access_row(grant),
            "credential": {"link": cred.get("link"), "expires_at": cred["expires_at"],
                           "mock": cred.get("mock", False), "note": cred.get("note")}}


@app.get("/api/requests/{reference}/access")
def list_access(reference: str, session: Session = Depends(get_session),
                _auth: str = Depends(require_action("grant_access"))) -> dict:
    """The environment's JIT access grants (metadata only — never the credential)."""
    _load_request(reference, session)
    rows = session.scalars(
        select(AccessGrant).where(AccessGrant.reference == reference).order_by(AccessGrant.id.desc())
    ).all()
    return {"reference": reference, "grants": [_access_row(g) for g in rows]}


@app.post("/api/requests/{reference}/access/{grant_id}/revoke")
def revoke_access(reference: str, grant_id: int, session: Session = Depends(get_session),
                  actor: str = Depends(require_action("grant_access"))) -> dict:
    """Revoke a JIT access grant early (approver/platform_admin). Revokes the vault
    credential and audits `access.revoked`."""
    grant = session.get(AccessGrant, grant_id)
    if grant is None or grant.reference != reference:
        raise HTTPException(status_code=404, detail="Access grant not found.")
    if grant.status != "active":
        return {"id": grant.id, "status": grant.status, "message": f"Already {grant.status}."}
    vault.revoke(grant.vault_handle)
    grant.status = "revoked"
    append_audit(session, "access.revoked", reference=reference, actor=actor,
                 detail={"grantee": grant.grantee, "scope": grant.scope})
    session.commit()
    return {"id": grant.id, "status": "revoked"}


def _sweep_access(session: Session) -> None:
    """Expire time-bound access grants past their expiry, revoking the vault
    credential (F-IAM-07). Runs each poll cycle; cheap when nothing is due."""
    now = datetime.now(timezone.utc)
    due = session.scalars(
        select(AccessGrant).where(AccessGrant.status == "active", AccessGrant.expires_at <= now)
    ).all()
    for grant in due:
        try:
            vault.revoke(grant.vault_handle)
        except Exception:  # noqa: BLE001 — a vault hiccup must not stall the sweep
            pass
        grant.status = "expired"
        append_audit(session, "access.expired", reference=grant.reference, actor="system",
                     detail={"grantee": grant.grantee, "scope": grant.scope})
    if due:
        session.commit()


# --- Outbound webhooks (F-INT-10) --------------------------------------------

class WebhookIn(BaseModel):
    url: str
    secret: str
    events: list[str] = []  # [] or ["*"] = all lifecycle events

    @field_validator("url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        v = (v or "").strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("secret")
    @classmethod
    def _valid_secret(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 8:
            raise ValueError("secret must be at least 8 characters")
        return v


def _webhook_row(sub: WebhookSubscription, session: Session) -> dict:
    """Subscription for the portal — the secret is NEVER returned."""
    last = session.scalar(select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id)
                          .order_by(WebhookDelivery.id.desc()))
    return {"id": sub.id, "url": sub.url, "events": sub.events or [], "active": sub.active,
            "created_by": sub.created_by,
            "last_delivery": ({"event": last.event, "status": last.status, "attempts": last.attempts,
                               "error": last.last_error} if last else None)}


@app.post("/api/webhooks")
def create_webhook(body: WebhookIn, session: Session = Depends(get_session),
                   actor: str = Depends(require_action("execute"))) -> dict:
    """Register an outbound webhook endpoint (F-INT-10). platform_admin. Lifecycle
    events are delivered here, HMAC-signed; the payload never contains secrets."""
    sub = WebhookSubscription(url=body.url, secret=body.secret,
                              events=[e.strip() for e in body.events if e.strip()], created_by=actor)
    session.add(sub)
    append_audit(session, "webhook.subscribed", actor=actor,
                 detail={"url": body.url, "events": sub.events})
    session.commit()
    return _webhook_row(sub, session)


@app.get("/api/webhooks")
def list_webhooks(session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("execute"))) -> dict:
    subs = session.scalars(select(WebhookSubscription).order_by(WebhookSubscription.id.desc())).all()
    return {"enabled": webhooks.enabled(), "webhooks": [_webhook_row(s, session) for s in subs]}


@app.delete("/api/webhooks/{webhook_id}")
def delete_webhook(webhook_id: int, session: Session = Depends(get_session),
                   actor: str = Depends(require_action("execute"))) -> dict:
    sub = session.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    url = sub.url
    session.delete(sub)
    append_audit(session, "webhook.unsubscribed", actor=actor, detail={"url": url})
    session.commit()
    return {"deleted": webhook_id}


@app.post("/api/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: int, session: Session = Depends(get_session),
                 actor: str = Depends(require_action("execute"))) -> dict:
    """Send a signed test event to the endpoint now — an explicit admin action, so
    it fires even when WEBHOOKS_ENABLED is off. Returns the delivery result."""
    sub = session.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    payload = {"id": 0, "event": "webhook.test", "reference": None, "actor": actor,
               "detail": {"message": "Test event from the infra portal."},
               "created_at": datetime.now(timezone.utc).isoformat()}
    ok, err = webhooks.deliver(sub, payload)
    append_audit(session, "webhook.tested", actor=actor, detail={"url": sub.url, "ok": ok, "error": err or None})
    session.commit()
    return {"ok": ok, "error": err or None}


def _decommission(session: Session, req: Request, actor: str) -> dict:
    """Tear down the resources of the provisioned request this decommission
    request targets, then mark both decommissioned.

    Reused by the approve endpoint and the poller. The signed handoff is built
    from the SOURCE request so the orchestrator destroys the right bucket (its
    per-request state and name), and the source's ProvisionedResource rows and
    status are updated to reflect the teardown.
    """
    source = session.scalar(
        select(Request).where(Request.reference == req.source_reference)
    )
    if source is None:
        req.status = "decommission-failed"
        req.status_detail = f"Source request {req.source_reference} not found."
        append_audit(session, "decommission.source_missing", reference=req.reference,
                     jira_key=req.approval.jira_key, detail={"source": req.source_reference})
        session.commit()
        return {"approval": "approved", "decommissioned": False,
                "error": f"Source request {req.source_reference} not found."}

    # Move the ticket into the provisioning lifecycle (Assigned -> In Progress)
    # so that, like provisioning, the Resolve transition is reachable afterwards
    # (the workflow has no direct Assigned -> Resolved hop).
    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    session.commit()

    body, signature = _handoff_payload(source)
    append_audit(session, "destroy.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"source": source.reference})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/destroy")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "decommission-failed"
        req.status_detail = reason
        append_audit(session, "destroy.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"source": source.reference, "error": raw})
        add_comment(req.approval.jira_key, "⚠️ Automated decommission failed; resources "
                                           f"were not removed.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "decommissioned": False, "error": reason}

    # Mark the source's live resources and the source request decommissioned.
    for res in session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == source.reference,
            ProvisionedResource.lifecycle_state == "active",
        )
    ):
        res.lifecycle_state = "decommissioned"
    source.status = "decommissioned"
    req.status = "decommissioned"
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    summary = response.json().get("summary")
    append_audit(session, "decommissioned", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"source": source.reference, "summary": summary})
    session.commit()
    return {"approval": "approved", "decommissioned": True, "source": source.reference,
            "message": f"Decommissioned {source.reference}: {summary}"}


_SENSITIVE = {"restricted", "confidential"}


def _refresh_handoff(req: Request, target: Request, src: Request, mask: bool) -> tuple[bytes, str]:
    """Build + sign the refresh handoff — the target, the copy-from source, and
    whether sensitive data must be masked."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": req.approval.jira_key,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "operation": "refresh",
        "target": {"reference": target.reference, "policy_input": _policy_input(target)},
        "source": {"reference": src.reference, "policy_input": _policy_input(src)},
        "mask": mask,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    return body, sign(WEBHOOK_SECRET, body)


def _refresh(session: Session, req: Request, actor: str) -> dict:
    """Refresh the target non-prod env (source_reference) from a higher source
    (refresh_from_reference), masking sensitive source data (F-LCM-03). Governed +
    orchestrator-executed, mirroring decommission. Mock records it; live is a
    data-copy + masking extension point. The target stays provisioned."""
    target = session.scalar(select(Request).where(Request.reference == req.source_reference))
    src = session.scalar(select(Request).where(Request.reference == req.refresh_from_reference))
    if target is None or src is None:
        missing = req.source_reference if target is None else req.refresh_from_reference
        req.status = "refresh-failed"
        req.status_detail = f"Refresh target/source {missing} not found."
        append_audit(session, "refresh.source_missing", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"target": req.source_reference, "from": req.refresh_from_reference})
        session.commit()
        return {"approval": "approved", "refreshed": False, "error": req.status_detail}

    mask = (src.data_classification or "").strip().lower() in _SENSITIVE
    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    session.commit()

    body, signature = _refresh_handoff(req, target, src, mask)
    append_audit(session, "refresh.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"target": target.reference, "from": src.reference, "mask": mask})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/refresh")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "refresh-failed"
        req.status_detail = reason
        append_audit(session, "refresh.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"target": target.reference, "from": src.reference, "error": raw})
        add_comment(req.approval.jira_key, "⚠️ Automated refresh failed; no data was "
                                           f"copied.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "refreshed": False, "error": reason}

    summary = response.json().get("summary")
    req.status = "refreshed"
    req.status_detail = None
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    append_audit(session, "refresh.performed", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"target": target.reference, "from": src.reference, "mask": mask,
                         "summary": summary})
    session.commit()
    return {"approval": "approved", "refreshed": True, "target": target.reference,
            "source": src.reference, "masked": mask,
            "message": f"Refreshed {target.reference} from {src.reference}: {summary}"}


# --- Automatic Jira-status poller (increment 2.7) ----------------------------
# A background thread that pulls the live Jira status on a timer and advances
# approved requests automatically — the same steps the portal's buttons run,
# just without a human click. Jira still holds the approval (a manager moving
# the ticket to Assigned); the orchestrator still re-verifies it (§4). The
# poller only *reacts* to that approval; it never decides one.

_poller_stop = threading.Event()
_poller_thread: threading.Thread | None = None


def provision_mode() -> str:
    """The real-provisioning mode the orchestrator is running (mock|plan|apply).

    The poller reads the same switch so it only auto-*applies* (creates real
    resources) when apply is enabled; otherwise it stops at the plan.
    """
    return os.getenv("PROVISION_MODE", "mock").strip().lower()


def auto_provision_enabled() -> bool:
    """Master on/off switch for the poller (default off). Nothing polls unless set."""
    return os.getenv("AUTO_PROVISION", "false").strip().lower() == "true"


def _record_scan(session: Session, req: Request, jira_key: str, scan: dict | None) -> None:
    """Record the orchestrator's IaC scan findings (F-SEC-03/04) as a tamper-
    evident scan.findings event, and surface a HIGH-severity summary. Passive:
    enforcement (blocking) is the orchestrator's call; here we only record what
    it reported so the findings show in the evidence pack + My Requests."""
    if not scan or not scan.get("findings"):
        return
    append_audit(session, "scan.findings", reference=req.reference, jira_key=jira_key,
                 actor="poller",
                 detail={"counts": scan.get("counts"), "findings": scan["findings"]})
    if scan.get("high"):
        req.status_detail = (f"IaC scan: {scan['high']} high-severity finding(s) in the "
                             f"terraform plan — review the audit trail.")
    session.commit()


def _advance_request(session: Session, req: Request) -> str:
    """Advance one request as far as its live Jira status allows, in one pass.

    Idempotent and safe to call every cycle: each step commits and re-entry
    resumes from the committed state. Returns the request status afterwards.
    Reuses the exact building blocks the manual buttons use, so the poller and
    a manual click can never diverge or double-provision (the orchestrator keeps
    its own idempotency ledger keyed on the Jira key).
    """
    # Give each autonomously-advanced request its own trace (F-OPS-04), so its
    # steps (handoff, orchestrator result, provisioned, …) group under one id.
    set_trace_id(new_trace_id())
    appr = req.approval
    if appr is None:
        return req.status
    jira_key = appr.jira_key

    # Step 1 — approve + plan (from 'submitted'). Read the approval from Jira;
    # never decide it here.
    if req.status == "submitted":
        status = get_status(jira_key)  # live Jira; JiraError bubbles to the caller
        if status == "rejected":
            if appr.status != "rejected":
                appr.status = "rejected"
                req.status = "rejected"
                append_audit(session, "approval.rejected", reference=req.reference,
                             jira_key=jira_key, actor="poller")
                session.commit()
            return req.status
        if status != "approved":
            if appr.status != status:  # reflect 'pending' etc. without audit noise
                appr.status = status
                session.commit()
            return req.status
        # Four-eyes (F-GOV-08): refuse if the Jira approver is the requester.
        if not _four_eyes_ok(session, req):
            return req.status
        # Approved — record it once, then run the same signed handoff (plan) the
        # approve button runs. On a retry (a previous plan failed) appr.status is
        # already 'approved', so we skip straight to re-planning.
        if appr.status != "approved":
            appr.status = "approved"
            append_audit(session, "approval.approved", reference=req.reference,
                         jira_key=jira_key, actor="poller")
            session.commit()
        # Approval quorum (F-GOV-06): require N distinct approvers before handoff.
        if not _quorum_ok(session, req):
            return req.status
        # Change window (F-GOV-05): hold provisioning outside the allowed window.
        if not _hold_for_change_window(session, req):
            return req.status
        # Decommission tears down the referenced request instead of provisioning.
        if req.request_type == "decommission":
            _decommission(session, req, actor="poller")
            return req.status
        # Refresh copies data from a higher env into the target (F-LCM-03).
        if req.request_type == "refresh":
            _refresh(session, req, actor="poller")
            return req.status
        # Restore rolls the target back to one of its backups (F-LCM-06).
        if req.request_type == "restore":
            _restore(session, req, actor="poller")
            return req.status
        body, signature = _handoff_payload(req)
        append_audit(session, "orchestrator.handoff", reference=req.reference,
                     jira_key=jira_key, detail={"contract": CONTRACT_VERSION})
        session.commit()
        response, error = _post_to_orchestrator(body, signature)
        if response is None or response.status_code != 200:
            raw = error or (response.text if response else "")
            req.status_detail = _short_reason(raw)  # surface why (e.g. an IaC scan block)
            append_audit(session, "plan.failed", reference=req.reference, jira_key=jira_key,
                         detail={"error": raw})
            session.commit()
            return req.status  # still 'submitted' — retried next cycle
        result = response.json()
        _record_scan(session, req, jira_key, result.get("scan"))  # IaC findings (F-SEC-03/04)
        if result.get("provisioned"):  # mock mode fully provisions on the handoff
            res = result.get("resource")
            if res:  # mock now records the resource, so the registry + control plane work
                ttl_dt = _ttl_for(req)
                session.add(ProvisionedResource(
                    reference=req.reference, kind=res.get("kind", "resource"),
                    name=res.get("name", ""), region=res.get("region"),
                    details=res.get("outputs", {}) or {}, ttl_expiry=ttl_dt,
                    lifecycle_state="active",
                ))
            req.status = "provisioned"
            append_audit(session, "provisioned", reference=req.reference, jira_key=jira_key,
                         detail=result)
            session.commit()
            return req.status
        req.status = "planned"
        append_audit(session, "plan.previewed", reference=req.reference, jira_key=jira_key,
                     detail={"plan_summary": result.get("plan_summary")})
        session.commit()

    # Step 2 — apply (from 'planned'), only when real apply is enabled. This is
    # the same work the Apply button starts; here it runs inline in the poller
    # thread. Respects PROVISION_MODE so plan-mode auto-runs stop at the plan.
    if req.status == "planned" and provision_mode() == "apply":
        ttl_dt = _ttl_for(req)  # non-prod gets a TTL; prod/dr exempt (F-FIN-07)
        ttl_iso = ttl_dt.isoformat() if ttl_dt else None
        body, signature = _handoff_payload(req, ttl_expiry=ttl_iso)
        _transition_jira(session, req, inprogress_status(), "jira.in_progress")
        req.status = "in-progress"
        append_audit(session, "provisioning.started", reference=req.reference,
                     jira_key=jira_key, actor="poller")
        session.commit()
        _provision_in_background(req.reference, jira_key, body, signature, ttl_iso)
        session.refresh(req)  # pick up 'provisioned' / 'apply-failed' from the apply
    return req.status


# --- Change windows (F-GOV-05) -----------------------------------------------

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _change_window_enabled() -> bool:
    return os.getenv("CHANGE_WINDOW_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


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


def change_window_status(now: datetime | None = None) -> dict:
    """Whether provisioning is allowed right now (F-GOV-05). Returns {open, reason}.
    Disabled (the default) always returns open, so nothing is ever held."""
    if not _change_window_enabled():
        return {"open": True, "reason": ""}
    tz_name = os.getenv("CHANGE_WINDOW_TZ", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz, tz_name = timezone.utc, "UTC"
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    days = _parse_days(os.getenv("CHANGE_WINDOW_DAYS", "mon-fri"))
    start = _parse_hm(os.getenv("CHANGE_WINDOW_START", "08:00"), dtime(8, 0))
    end = _parse_hm(os.getenv("CHANGE_WINDOW_END", "18:00"), dtime(18, 0))
    if local.weekday() in days and start <= local.time() <= end:
        return {"open": True, "reason": ""}
    window = (f"{os.getenv('CHANGE_WINDOW_DAYS', 'mon-fri')} "
              f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} {tz_name}")
    return {"open": False, "reason": f"outside the change window (allowed {window})"}


def _hold_for_change_window(session: Session, req: Request) -> bool:
    """Gate provisioning on the change window (F-GOV-05). Returns True if it's OK
    to proceed. When held, records status_detail + a change_window.held audit
    (once) and re-checks each poll, so it provisions when the window opens."""
    status = change_window_status()
    if status["open"]:
        if req.status_detail and "change window" in req.status_detail:
            req.status_detail = None  # window opened — clear the stale hold note
        return True
    if req.change_window_held_at is None:
        req.change_window_held_at = datetime.now(timezone.utc)
        append_audit(session, "change_window.held", reference=req.reference,
                     jira_key=req.approval.jira_key if req.approval else None, actor="poller",
                     detail={"reason": status["reason"]})
    req.status_detail = f"Held: {status['reason']}; will provision when the window opens."
    session.commit()
    return False


def _escalate_sla(session: Session, req: Request) -> None:
    """Escalate an approval that has breached its SLA — once (F-GOV-01).

    Posts a Jira comment (best-effort), records a tamper-evident `sla.breached`
    audit event, and stamps sla_escalated_at so it never fires twice.
    """
    if req.status != "submitted" or req.sla_escalated_at is not None:
        return
    sla = _compute_sla(req.status, req.submitted_at, req.created_at)
    if not sla or sla["status"] != "breached":
        return
    if req.approval and jira_mode() == "live":
        try:
            add_comment(
                req.approval.jira_key,
                f"⏰ Approval SLA breached: {sla['elapsed_hours']:.1f}h elapsed "
                f"(SLA {sla['sla_hours']:.0f}h). Escalating for review.",
            )
        except JiraError:
            pass  # comment is best-effort; the audit + flag still record the breach
    req.sla_escalated_at = datetime.now(timezone.utc)
    append_audit(session, "sla.breached", reference=req.reference,
                 jira_key=req.approval.jira_key if req.approval else None,
                 detail={"elapsed_hours": sla["elapsed_hours"], "sla_hours": sla["sla_hours"]})
    session.commit()


def _resolve_owner(req: Request) -> str | None:
    """The environment's effective owner (F-LCM-10): the named environment owner,
    else the application owner, else the requester."""
    return req.environment_owner or req.application_owner or req.requester or None


def _departed_owners() -> set[str]:
    """People who have left, whose environments should be flagged for reassignment
    (F-LCM-10). Populated from DEPARTED_OWNERS; a live directory sync fills this later."""
    return {o.strip().lower() for o in os.getenv("DEPARTED_OWNERS", "").split(",") if o.strip()}


def _is_orphan(req: Request) -> bool:
    """Whether a provisioned environment has no resolvable owner (F-LCM-10): none
    at all, or the effective owner has left (is in DEPARTED_OWNERS). An environment
    with an owning GROUP is never orphaned — group ownership survives an individual
    leaving (F-IAM-09)."""
    if (req.owner_group or "").strip():
        return False
    owner = _resolve_owner(req)
    if not owner:
        return True
    return owner.strip().lower() in _departed_owners()


def _in_owner_group(actor: str, req: Request) -> bool:
    """Whether `actor` is a member of the environment's owning group (F-IAM-09)."""
    group = (req.owner_group or "").strip()
    return bool(group) and group in roles_mod.user_groups(actor)


def _ttl_contact(req: Request) -> str | None:
    """Best contact for a TTL notice: the environment owner, else the requester."""
    return _resolve_owner(req)


def _ttl_warn(session: Session, req: Request, expiry: datetime) -> None:
    """Warn the owner once that a non-prod environment is nearing expiry (F-FIN-07)."""
    if req.ttl_notified is not None:
        return
    days = max(0, (expiry - datetime.now(timezone.utc)).days)
    req.ttl_notified = "expiring"
    req.status_detail = (f"Expiring on {expiry.date().isoformat()} "
                         f"(in {days} day(s)) — renew it to keep it.")
    if req.approval is not None:
        try:
            add_comment(req.approval.jira_key,
                        f"⏳ Environment {req.reference} expires on {expiry.date().isoformat()} "
                        f"(in {days} day(s)). Renew it if it is still needed "
                        f"(contact: {_ttl_contact(req)}).")
        except JiraError:
            pass
    append_audit(session, "ttl.expiring", reference=req.reference,
                 jira_key=req.approval.jira_key if req.approval else None, actor="poller",
                 detail={"expiry": expiry.isoformat(), "days_left": days})
    session.commit()


def _ttl_decommission(session: Session, req: Request) -> None:
    """Auto-reclaim an expired non-prod environment when TTL_ENFORCE is on
    (F-FIN-07). Tears down its resources via the same signed /destroy handoff the
    destroy button uses; leaves the resources untouched on any failure."""
    body, signature = _handoff_payload(req)
    append_audit(session, "ttl.decommission.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key if req.approval else None, actor="poller")
    session.commit()
    response, error = _post_to_orchestrator(body, signature, path="/destroy")
    if response is None or response.status_code != 200:
        append_audit(session, "ttl.decommission.failed", reference=req.reference,
                     jira_key=req.approval.jira_key if req.approval else None,
                     detail={"error": error or (response.text if response else "")})
        session.commit()
        return
    for res in session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == req.reference,
            ProvisionedResource.lifecycle_state == "active",
        )
    ):
        res.lifecycle_state = "decommissioned"
    req.status = "decommissioned"
    req.status_detail = f"Auto-decommissioned at TTL expiry ({datetime.now(timezone.utc).date().isoformat()})."
    append_audit(session, "ttl.decommissioned", reference=req.reference,
                 jira_key=req.approval.jira_key if req.approval else None,
                 detail=response.json())
    session.commit()


def _ttl_expired(session: Session, req: Request, expiry: datetime) -> None:
    """Flag an expired non-prod environment once, and reclaim it if enforced."""
    if req.ttl_notified != "expired":
        enforced = _ttl_enforce()
        req.ttl_notified = "expired"
        req.status_detail = (f"Expired on {expiry.date().isoformat()}. "
                             + ("Decommissioning now." if enforced
                                else "Renew it, or enable TTL_ENFORCE to auto-reclaim."))
        if req.approval is not None:
            try:
                add_comment(req.approval.jira_key,
                            f"⛔ Environment {req.reference} expired on {expiry.date().isoformat()}. "
                            + ("It is being decommissioned." if enforced
                               else "Renew it to keep it."))
            except JiraError:
                pass
        append_audit(session, "ttl.expired", reference=req.reference,
                     jira_key=req.approval.jira_key if req.approval else None, actor="poller",
                     detail={"expiry": expiry.isoformat(), "enforced": enforced})
        session.commit()
    if _ttl_enforce():
        _ttl_decommission(session, req)


def _sweep_shutdowns(session: Session) -> None:
    """Enforce each non-prod compute environment's effective auto-shutdown schedule
    (F-FIN-06). Per-environment (increment B): the request's override merged over
    the global policy wins, so different environments can have different windows —
    or opt out entirely. Desired-state, not a global boundary:

      - effective policy disabled  -> skipped (the opt-out / master-off).
      - off-hours + running        -> stop  (actor 'auto-shutdown', marks auto_stopped).
      - in-hours + auto_stopped    -> start (only re-starts what the sweep stopped).
      - a manual stop              -> left alone (never auto-started).

    Runs the SAME `_actuate_env` path as a manual click. Mock changes no real cloud;
    live + OCI_ACTUATE_ENABLED really powers the VM."""
    acted: list[tuple[str, str]] = []
    for req in autoshutdown.nonprod_provisioned(session):
        if req.approval is None:
            continue
        policy = autoshutdown.effective_policy(session, req)
        if not autoshutdown.enabled(policy):
            continue
        power = _power_for(session, req.reference)  # compute-only; None if no VM
        if power is None:
            continue
        off = autoshutdown.is_off_hours(policy)
        if off and power != "stopped":
            action = "stop"
        elif not off and power == "stopped" and req.auto_stopped:
            action = "start"
        else:
            continue
        result = _actuate_env(session, req, action, actor="auto-shutdown")
        if result.get("power") and not result.get("idempotent"):
            acted.append((req.reference, action))
    if acted:
        append_audit(session, "shutdown.swept", actor="auto-shutdown", detail={"actions": acted})
    session.commit()


def _cloud_state_sync_enabled() -> bool:
    """Whether the reconciliation sweep runs. Off by default — the on-demand
    reconcile endpoint + saving view still work."""
    return os.getenv("CLOUD_STATE_SYNC_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _sweep_state(session: Session) -> None:
    """Reconcile provisioned environments against actual cloud state (read-only).

    Opt-in (CLOUD_STATE_SYNC_ENABLED). Records each request's sync status and
    audits a *change* of state (drift detected / recovered). No-op when disabled;
    a transient orchestrator error skips that request and retries next cycle."""
    if not _cloud_state_sync_enabled():
        return
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        if req.approval is None:
            continue
        result = _reconcile(session, req)
        if "error" in result:
            continue  # transient — try again next cycle
        _apply_reconcile(session, req, result, actor="state-sync")
        session.commit()


def _sweep_ttls(session: Session) -> None:
    """One TTL pass (F-FIN-07): warn owners of non-prod environments nearing
    expiry, flag expired ones, and — only when TTL_ENFORCE is on — reclaim them.
    Operates per environment (earliest active-resource expiry); fires once per
    stage via the ttl_notified flag."""
    now = datetime.now(timezone.utc)
    warn_cutoff = now + timedelta(days=_ttl_warn_days())
    rows = session.execute(
        select(ProvisionedResource.reference, func.min(ProvisionedResource.ttl_expiry))
        .where(ProvisionedResource.lifecycle_state == "active",
               ProvisionedResource.ttl_expiry.is_not(None))
        .group_by(ProvisionedResource.reference)
    ).all()
    for reference, expiry in rows:
        if expiry is None:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        req = session.scalar(select(Request).where(Request.reference == reference))
        if req is None or req.status != "provisioned":
            continue
        if expiry <= now:
            _ttl_expired(session, req, expiry)
        elif expiry <= warn_cutoff:
            _ttl_warn(session, req, expiry)


def _sweep_orphans(session: Session) -> None:
    """Flag newly-orphaned provisioned environments once (F-LCM-10), and clear the
    flag when an environment gets an owner again. Best-effort Jira note for ops."""
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        orphan = _is_orphan(req)
        if orphan and req.orphaned_at is None:
            req.orphaned_at = datetime.now(timezone.utc)
            owner = _resolve_owner(req)
            reason = "has no assigned owner" if not owner else f"owner '{owner}' has left"
            req.status_detail = f"Orphaned: {reason}; reassign an owner."
            if req.approval is not None:
                try:
                    add_comment(req.approval.jira_key,
                                f"👤 Environment {req.reference} is orphaned ({reason}). "
                                f"Reassign an owner.")
                except JiraError:
                    pass
            append_audit(session, "ownership.orphaned", reference=req.reference,
                         jira_key=req.approval.jira_key if req.approval else None, actor="poller",
                         detail={"owner": owner, "reason": reason})
            session.commit()
        elif not orphan and req.orphaned_at is not None:
            req.orphaned_at = None  # owner reassigned / un-departed — clear the flag
            if req.status_detail and "orphan" in req.status_detail.lower():
                req.status_detail = None
            session.commit()


def _poll_once() -> None:
    """One sweep: advance every request that isn't finished, each in its own
    session so one bad request can't abort the others."""
    with SessionLocal() as session:
        refs = [
            r.reference
            for r in session.scalars(
                select(Request).where(Request.status.in_(("submitted", "planned")))
            ).all()
            if r.approval is not None
        ]
    for ref in refs:
        try:
            with SessionLocal() as session:
                req = session.scalar(select(Request).where(Request.reference == ref))
                if req is not None and req.approval is not None:
                    # SLA escalation runs first + independently, so a transient
                    # Jira error while advancing can't skip it (F-GOV-01).
                    _escalate_sla(session, req)
                    _advance_request(session, req)
        except JiraError:
            continue  # transient — try again next cycle, no audit noise
        except Exception as exc:  # noqa: BLE001 — never let one request stop the sweep
            try:
                with SessionLocal() as session:
                    append_audit(session, "poll.error", reference=ref, detail={"error": str(exc)})
                    session.commit()
            except Exception:  # noqa: BLE001
                pass

    # TTL sweep (F-FIN-07): warn/flag/reclaim expiring non-prod environments once
    # per cycle, isolated so it can never abort the request advancement above.
    try:
        with SessionLocal() as session:
            _sweep_ttls(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "ttl.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Orphan sweep (F-LCM-10): flag provisioned environments with no owner.
    try:
        with SessionLocal() as session:
            _sweep_orphans(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "orphan.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Scheduled auto-shutdown sweep (F-FIN-06): opt-in; records pause/resume at the
    # business-hours boundary. No-op unless SHUTDOWN_ENABLED.
    try:
        with SessionLocal() as session:
            _sweep_shutdowns(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "shutdown.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Cloud state reconciliation sweep: opt-in; syncs the registry against actual
    # cloud state. No-op unless CLOUD_STATE_SYNC_ENABLED. Read-only.
    try:
        with SessionLocal() as session:
            _sweep_state(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "state.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # JIT access expiry sweep (F-IAM-07): expire time-bound grants + revoke the
    # vault credential. Always on (it's a security control, not opt-in).
    try:
        with SessionLocal() as session:
            _sweep_access(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "access.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Outbound webhook fan-out (F-INT-10): opt-in; deliver new lifecycle events to
    # admin-configured endpoints, signed, with retries. No-op unless WEBHOOKS_ENABLED.
    try:
        with SessionLocal() as session:
            webhooks.process(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "webhook.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass


def _poller_loop() -> None:
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
    while not _poller_stop.is_set():
        try:
            _poll_once()
        except Exception:  # noqa: BLE001 — the loop must survive anything
            pass
        _poller_stop.wait(interval)


def _start_poller() -> None:
    """Start the background poller only if AUTO_PROVISION is on (called on startup)."""
    global _poller_thread
    if auto_provision_enabled() and (_poller_thread is None or not _poller_thread.is_alive()):
        _poller_stop.clear()
        _poller_thread = threading.Thread(target=_poller_loop, name="jira-poller", daemon=True)
        _poller_thread.start()


def _stop_poller() -> None:
    """Signal the poller loop to exit (called on shutdown)."""
    _poller_stop.set()


# --- Subsidiary master data synced from Jira (customfield_36200) --------------

_subsync_stop = threading.Event()
_subsync_thread: threading.Thread | None = None


def subsidiary_sync_enabled() -> bool:
    """The subsidiary sync runs only against live Jira, and can be turned off."""
    return (
        jira_mode() == "live"
        and os.getenv("SUBSIDIARY_SYNC_ENABLED", "true").strip().lower() == "true"
    )


def _sync_subsidiaries_once() -> dict:
    """One sync sweep in its own session: reconcile the Subsidiary table from
    Jira (customfield_36200) and audit it if anything actually changed. Never
    raises — a bad sync must not crash a request or the loop."""
    try:
        with SessionLocal() as session:
            summary = sync_subsidiaries(session)
            if summary.get("changed"):
                append_audit(session, "subsidiaries.synced", actor="jira-sync", detail=summary)
            session.commit()
        return summary
    except Exception as exc:  # noqa: BLE001
        return {"synced": False, "changed": 0, "reason": str(exc)}


def _subsync_loop() -> None:
    interval = int(os.getenv("SUBSIDIARY_SYNC_INTERVAL_SECONDS", "3600"))
    while not _subsync_stop.is_set():
        _sync_subsidiaries_once()  # syncs once immediately, then every interval
        _subsync_stop.wait(interval)


def _start_subsync() -> None:
    """Start the subsidiary-sync thread (live Jira mode only; called on startup)."""
    global _subsync_thread
    if subsidiary_sync_enabled() and (_subsync_thread is None or not _subsync_thread.is_alive()):
        _subsync_stop.clear()
        _subsync_thread = threading.Thread(
            target=_subsync_loop, name="subsidiary-sync", daemon=True
        )
        _subsync_thread.start()


def _stop_subsync() -> None:
    """Signal the subsidiary-sync loop to exit (called on shutdown)."""
    _subsync_stop.set()
