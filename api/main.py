import hmac
import json
import logging
import os
import re
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
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.orm import Session

from api import ai_chat
from api import ai_drafter
from api import ai_explainer
from api import ai_recommend
from api import ai_triage
from api import golden
from api import repo_facts
from api import discovery
from api import registry
from api import blueprint_capabilities
from api import network_egress
from api import cloud_options
from api import component_options
from api import ai_blueprint
from api import autobuild
from api import catalogue_sync
from api import certification
from api import fulfilment
from api import portal_help
from api import resource_details
from api import settings
from api import anomalies as anomaly_detect
from api import apikeys
from api import chatbot
from api import eventstream
from api import forecast
from api import optimisation as optim
from api import leader
from api import ratelimit
from api import reports
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
from api import policy
from api.policy import PolicyUnavailable, get_policy_evaluator
from api.pricing import estimate_cost
from api import roles as roles_mod
from api.sizing import resolve_components
from api.validation import (CREATE_LIKE_TYPES, DEPLOYMENT_TARGETS, normalise_tier,
                            validate_submission)
from common.security import install_rate_limit, install_security_headers
from common.signing import sign
from db.models import (
    Blueprint,
    AccessGrant,
    ActualCost,
    ApiKey,
    Approval,
    AuditLog,
    Backup,
    Budget,
    ComponentOption,
    CostCentre,
    Quota,
    Environment,
    Estimate,
    Project,
    ProvisionedResource,
    ReportRun,
    ReportSubscription,
    RoleMapping,
    Request,
    RequestComponent,
    Setting,
    SizingAnchor,
    UserRole,
    Subsidiary,
    Technology,
    WebhookDelivery,
    WebhookSubscription,
)
from db.session import SessionLocal

load_dotenv()


def _ensure_tables() -> None:
    """Create any table a model declares but the database does not have.

    WHY THIS EXISTS. There is no migration tool here, and `create_all` only ran
    from `db/seed.py::main()` — a script nobody runs on deploy. So C2's
    `certification_proof` table was never created, and the certification sweep
    raised UndefinedTable every thirty seconds from the moment proofs were
    switched on. The feature was reported as live and was not.

    Deliberately create-only. It adds missing TABLES and touches nothing that
    already exists — it cannot add a column, drop anything, or alter a type, so
    it can never be the reason data is lost. A real schema change still needs a
    migration and a human.

    Failure here must not stop the API: a database that cannot be reached at
    startup is a much louder problem than a missing table, and the error is
    logged rather than swallowed.
    """
    try:
        from db.session import Base, engine
        Base.metadata.create_all(engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logging.getLogger("uvicorn.error").warning(
            "could not ensure database tables exist: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the background workers on startup, stop them on shutdown.

    The functions are defined further down. The Jira poller runs only when
    AUTO_PROVISION is on; the subsidiary sync runs only against live Jira; the
    cloud-option refresh runs only when OCI_CATALOGUE_ENABLED is on. So nothing
    runs by default (mock mode / dev / tests).
    """
    _ensure_tables()
    _start_poller()
    _start_subsync()
    _start_catalogue_refresh()
    try:
        yield
    finally:
        _stop_poller()
        _stop_subsync()
        _stop_catalogue_refresh()


app = FastAPI(title="Infra Portal API", lifespan=_lifespan)

# Application hardening (F-SEC-09): strict headers (JSON-only, so lock everything
# down) + a per-client rate limit. The API sits behind the BFF; these are defence
# in depth. SSE + health are exempt from the rate limit.
install_security_headers(app, csp="default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
# RATE_LIMIT_BACKEND=shared counts in the DB so the limit is global across replicas
# (F-OPS-01). Default is the in-memory limiter — single-instance behaviour unchanged.
_rate_exempt = ("/health", "/api/events/stream", "/internal/")
if ratelimit.shared_enabled():
    ratelimit.install_shared_rate_limit(app, exempt_prefixes=_rate_exempt)
else:
    install_rate_limit(app, exempt_prefixes=_rate_exempt)
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
# Longer than the longest thing the orchestrator is allowed to run, or the API
# gives up while the work is still legitimately going.
#
# It was 300s, retried three times. An OKE apply is allowed 2700s
# (command_timeout_seconds in oci-oke.yaml) because a cluster takes 10-20 minutes
# and its node pool longer. So REQ-2026-0149 timed out at 5 minutes, was retried
# twice more, and at 15 minutes the API declared apply-failed — while the
# orchestrator was still building the cluster that is now running.
#
# Three apply requests were also sent for one cluster. Only the orchestrator's
# idempotency ledger stopped that becoming three clusters.
ORCH_TIMEOUT = float(os.getenv("ORCHESTRATOR_TIMEOUT_SECONDS", "3000"))
# Sandbox resources get a short time-to-live (F-FIN-07 foundation).
PROVISION_TTL_DAYS = int(os.getenv("PROVISION_TTL_DAYS", "7"))

# Environment TTL & renewal (E3.1, F-FIN-07). Only known non-prod tiers auto-
# expire; prod/dr and unknown-tier requests (e.g. add/resize on an existing
# environment) never expire, so production is never wrongly reclaimed.
TTL_NONPROD_TIERS = {"dev", "test", "sit", "uat", "preprod"}


def _ttl_days_nonprod() -> int:
    try:
        return max(1, int(settings.env("TTL_DAYS_NONPROD", "30")))
    except (ValueError, TypeError):
        return 30


def _ttl_days_sandbox() -> int:
    """A sandbox environment's short lifetime (F-CAT). Default 7 days."""
    try:
        return max(1, int(os.getenv("TTL_DAYS_SANDBOX", "7")))
    except (ValueError, TypeError):
        return 7


def _ttl_warn_days() -> int:
    try:
        return max(0, int(settings.env("TTL_WARN_DAYS", "7")))
    except (ValueError, TypeError):
        return 7


def _ttl_enforce() -> bool:
    """Whether an expired non-prod environment is auto-decommissioned. Off by
    default: expiry only warns/flags until an operator turns enforcement on."""
    return settings.env("TTL_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _ttl_for(req: Request) -> datetime | None:
    """Expiry for a newly provisioned environment (F-FIN-07 / F-CAT): a short-lived
    sandbox gets a short default, a temporary env its chosen expiry, other non-prod
    tiers today's policy, and prod/dr never expire."""
    now = datetime.now(timezone.utc)
    if req.request_type == "sandbox":
        return now + timedelta(days=_ttl_days_sandbox())
    if req.request_type == "temporary":
        if req.expires_on:  # the user's chosen date, at end of that day (UTC)
            return datetime(req.expires_on.year, req.expires_on.month, req.expires_on.day,
                            23, 59, 59, tzinfo=timezone.utc)
        return now + timedelta(days=_ttl_days_nonprod())  # fallback if somehow unset
    tier = (req.environment_tier or "").strip().lower()
    if tier in TTL_NONPROD_TIERS:
        return now + timedelta(days=_ttl_days_nonprod())
    return None


def _variance_alert_pct() -> float:
    try:
        return max(0.0, float(settings.env("VARIANCE_ALERT_PCT", "15")))
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


# --- Access control: group -> role map (E1, F-IAM-01) ------------------------

class RoleMappingIn(BaseModel):
    jira_group: str
    role: str

    @field_validator("jira_group")
    @classmethod
    def _clean_group(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("A Jira group name is required.")
        return v[:120]

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in roles_mod.ALL_ROLES:
            raise ValueError(f"role must be one of: {', '.join(sorted(roles_mod.ALL_ROLES))}")
        return v


@app.get("/api/access/role-map")
def list_role_map(session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("manage_access"))) -> dict:
    """The group->role map (F-IAM-01) — platform_admin. Includes the current role
    source so the admin sees whether these mappings are live or in dev/mock."""
    rows = session.scalars(select(RoleMapping).order_by(RoleMapping.jira_group)).all()
    return {
        "mappings": [{"jira_group": r.jira_group, "role": r.role, "updated_by": r.updated_by,
                      "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows],
        "roles": sorted(roles_mod.ALL_ROLES),
        "role_source": roles_mod.role_source(),
    }


@app.put("/api/access/role-map")
def set_role_map(body: RoleMappingIn, session: Session = Depends(get_session),
                 actor: str = Depends(require_action("manage_access"))) -> dict:
    """Add or update a group->role mapping (F-IAM-01). Clears the role cache so it
    takes effect immediately."""
    row = session.get(RoleMapping, body.jira_group)
    if row is None:
        session.add(RoleMapping(jira_group=body.jira_group, role=body.role, updated_by=actor))
    else:
        row.role = body.role
        row.updated_by = actor
        row.updated_at = datetime.now(timezone.utc)
    append_audit(session, "rolemap.set", actor=actor, detail={"group": body.jira_group, "role": body.role})
    session.commit()
    roles_mod.clear_cache()
    return {"jira_group": body.jira_group, "role": body.role}


@app.delete("/api/access/role-map/{jira_group}")
def delete_role_map(jira_group: str, session: Session = Depends(get_session),
                    actor: str = Depends(require_action("manage_access"))) -> dict:
    row = session.get(RoleMapping, jira_group)
    if row is None:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    session.delete(row)
    append_audit(session, "rolemap.removed", actor=actor, detail={"group": jira_group})
    session.commit()
    roles_mod.clear_cache()
    return {"deleted": jira_group}


@app.get("/api/access/resolve")
def resolve_access(email: str, _auth: str = Depends(require_action("manage_access"))) -> dict:
    """Preview what a given user resolves to — their groups and the roles those
    grant (F-IAM-01). Verify the map is right before turning live enforcement on."""
    return roles_mod.resolve_detail(email)


@app.get("/api/access/users")
def list_users(session: Session = Depends(get_session),
               _auth: str = Depends(require_action("manage_access"))) -> dict:
    """Read-only Users & roles (F-IAM-01): the people who've used the portal —
    distinct email actors in the audit trail, most-recently-active first — each
    with their resolved roles + groups, last-seen, and action count. Visibility
    only; reflects the directory. Capped, since live resolution hits Jira."""
    rows = session.execute(
        select(AuditLog.actor, func.count(), func.max(AuditLog.created_at))
        .where(AuditLog.actor.like("%@%"))   # humans are emails; excludes poller/scheduler/leader ids
        .group_by(AuditLog.actor)
        .order_by(func.max(AuditLog.created_at).desc())
        .limit(50)
    ).all()
    users = []
    for actor, count, last_seen in rows:
        detail = roles_mod.resolve_detail(actor)
        users.append({"email": actor, "roles": detail["roles"], "groups": detail["groups"],
                      "actions": int(count), "last_seen": last_seen.isoformat() if last_seen else None})
    return {"users": users, "source": roles_mod.role_source()}


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


@app.get("/api/approval-info")
def approval_info(_requester: str = Depends(_authed_requester)) -> dict:
    """What happens after Submit — the non-sensitive approval facts, for any
    signed-in requester (F-GOV-*). Unlike /api/config (admin posture), this exposes
    only what a requester legitimately needs: where approval happens, how many
    approvers + by when, that they can't self-approve, and the change window."""
    return {
        "system_of_record": "Jira" if jira_mode() == "live" else "Jira (mock mode)",
        "quorum": _approval_quorum(),
        "sla_hours": float(settings.env("APPROVAL_SLA_HOURS", "24")),
        "four_eyes": _four_eyes_enforced(),
        "sod_enforced": _sod_enforced(),
        "change_window": {
            "enabled": _change_window_enabled(),
            "open_now": change_window_status()["open"],
            "days": settings.env("CHANGE_WINDOW_DAYS", "mon-fri"),
            "start": settings.env("CHANGE_WINDOW_START", "08:00"),
            "end": settings.env("CHANGE_WINDOW_END", "18:00"),
            "tz": settings.env("CHANGE_WINDOW_TZ", "UTC"),
        },
    }


@app.get("/api/search")
def search_requests(q: str = "", requester: str = Depends(_authed_requester),
                    session: Session = Depends(get_session)) -> dict:
    """Header quick-search over the caller's OWN requests (Portal UI polish).
    Case-insensitive contains on reference / environment / project / cost centre;
    scoped server-side to the caller. Short queries (<2 chars) return nothing."""
    term = q.strip()
    if len(term) < 2:
        return {"results": []}
    like = f"%{term}%"
    rows = session.scalars(
        select(Request)
        .where(Request.requester == requester)
        .where(or_(
            Request.reference.ilike(like),
            Request.environment_name.ilike(like),
            Request.target_environment.ilike(like),
            Request.project_code.ilike(like),
            Request.cost_centre_code.ilike(like),
        ))
        .order_by(Request.id.desc())
        .limit(15)
    ).all()
    return {"results": [{
        "reference": r.reference,
        "environment": r.environment_name or r.target_environment or "",
        "status": r.status,
        "tier": r.environment_tier or "",
        "request_type": r.request_type,
    } for r in rows]}


def _catalogue_options_posture(session: Session) -> dict:
    """What the component detail form is currently able to offer.

    Catalogue data rather than a setting, but a platform admin still needs to see
    it from the console: "how many technologies can a requester choose a version
    for, and where did those options come from?" is exactly the question that
    goes unanswered until someone reads the seed file.
    """
    rows = session.scalars(select(ComponentOption)).all()
    versioned = {r.technology_code for r in rows if r.field == "version"}
    refreshed = cloud_options.last_refreshed(session)
    return {
        "technologies_total": session.scalar(
            select(func.count()).select_from(Technology)) or 0,
        "with_version_choice": len(versioned),
        "options_total": len(rows),
        # Where the rows came from. "seed" is the hand-curated catalogue;
        # "oci-live" is the hourly fetch. Seeing only "seed" while the fetch is
        # meant to be on is the signal that it is not actually working.
        "sources": sorted({r.source for r in rows}),
        # Numeric options are derived from the sizing anchors rather than stored,
        # so every technology has a working detail form with no catalogue entry.
        "shape_options_from": "sizing anchors (F-CAT-07)",
        # --- the hourly cloud fetch ---
        "live_fetch_enabled": cloud_options.enabled(),
        "refresh_interval_seconds": cloud_options.refresh_interval_seconds(),
        "last_refreshed": refreshed.isoformat() if refreshed else None,
        "images_cached": sum(1 for r in rows if r.field == "image"),
        "shapes_cached": sum(1 for r in rows if r.field == "shape"),
        # Whether the portal has read the orchestrator's blueprint capabilities.
        # False means the OS-image filter is INERT — every image is offered for
        # every technology — which is indistinguishable from "nothing needed
        # filtering" unless it is stated.
        "blueprint_capabilities_known": blueprint_capabilities.known(),
        # Whether the portal knows what the build network can reach. False
        # means the OS-image list is NOT being filtered on that basis, so an
        # image whose packages are unreachable can still be chosen — the
        # machine's own boot report is then the only thing that will catch it.
        "network_egress_known": network_egress.known(),
    }


@app.get("/api/config")
def system_config(session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("execute"))) -> dict:
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
            "approval_sla_hours": float(settings.env("APPROVAL_SLA_HOURS", "24")),
            "audit_hmac": bool(os.getenv("AUDIT_HMAC_KEY", "").strip()),
        },
        "change_window": {
            "enabled": _change_window_enabled(),
            "open_now": change_window_status()["open"],
            "days": settings.env("CHANGE_WINDOW_DAYS", "mon-fri"),
            "start": settings.env("CHANGE_WINDOW_START", "08:00"),
            "end": settings.env("CHANGE_WINDOW_END", "18:00"),
            "tz": settings.env("CHANGE_WINDOW_TZ", "UTC"),
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
        "catalogue": _catalogue_options_posture(session),
    }


class SettingIn(BaseModel):
    value: str


# Execution gates live in the ORCHESTRATOR's environment, not the API's. Labels
# for the console; the values come from the orchestrator itself (see below).
_EXECUTION_GATE_LABELS = {
    "provision_mode": "Provisioning mode",
    "psql_enabled": "Managed PostgreSQL allowed",
    "psql_configured": "Managed PostgreSQL configured (subnet + vault secret)",
    "dns_mode": "DNS mode",
    "dns_enabled": "DNS record creation allowed",
    "dns_zone_set": "DNS zone configured",
    "config_enabled": "First-boot configuration",
    "marketplace_enabled": "OCI Marketplace listings readable",
    "backup_mode": "Backup mode",
    "restore_mode": "Restore mode",
    "reduce_mode": "Capacity reduction mode",
    "refresh_mode": "Data refresh mode",
    "cloud_state_mode": "Cloud-state sync mode",
    "actuate_enabled": "Real stop/start allowed",
    "oke_bastion_cidr_set": "OKE bastion source range configured",
    # A list of tier names, not a boolean: "no tiers mapped" and "only
    # Development mapped" are different operational facts, and a request for
    # an unmapped tier is refused rather than built in another tier's VCN.
    "oke_mapped_tiers": "OKE networks mapped for these environment tiers",
    "oke_kubernetes_version": "OKE Kubernetes version",
    "oke_cluster_type": "OKE cluster type",
    "kafka_source_set": "Kafka archive source configured",
    # The gate that lets the portal provision WITHOUT a Jira approval
    # (ARCHITECTURE.md §4, "Attest"). Its bounds are shown by value, not as
    # booleans: "proofs are on" is not useful without "in which tier, and up to
    # what monthly cost".
    "generated_blueprints": "Blueprints written by the agent (not reviewed)",
    "generated_blueprints_dir": "Where agent-written blueprints land",
    "proof_enabled": "Certification proof builds allowed (real, billable)",
    "proof_sandbox_tier": "Proof builds restricted to this tier",
    "proof_cost_cap_monthly": "Proof build cost ceiling (monthly)",
    # Shown so a version skew is visible BEFORE a proof rather than during
    # one. When the two services disagree here, every proof is refused with
    # "not a proof reference" — which reads like an attack, not a stale image.
    "proof_reference_pattern": "Proof reference format the runner accepts",
    "generated_profiles_dir": "Where agent-written technology profiles are kept",
    "generated_profiles": "Technology profiles the agent has written",
    "catalogue_mode": "Cloud option catalogue mode",
    "catalogue_shapes_allowed": "Compute shapes allow-listed (0 = offer none)",
    "catalogue_image_filter_set": "OS image filter configured",
    "boot_report_configured": "Boot self-report destination configured",
}


def _orchestrator_posture() -> dict | None:
    """Ask the orchestrator which execution gates are open.

    Returns None if it can't be reached. The caller shows that as 'unavailable'
    rather than substituting the API's own environment — these switches are read
    by a different process, so guessing would display a confident falsehood about
    whether the portal can create billable infrastructure.
    """
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(), "operation": "posture"}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _error = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw), path="/posture")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _orchestrator_cloud_options() -> dict | None:
    """Ask the orchestrator what the tenancy offers (shapes + OS images).

    Same signed channel and same reasoning as _orchestrator_posture: listing
    shapes needs OCI credentials, and this process holds none. Read-only at the
    far end — the endpoint calls list_* and nothing else.
    """
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(),
               "operation": "catalogue"}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _error = _post_to_orchestrator(
        raw, sign(WEBHOOK_SECRET, raw), path="/catalogue/oci-options")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _refresh_cloud_options_once() -> dict:
    """One cache refresh. Safe to call from a thread or an admin's button."""
    with SessionLocal() as session:
        return cloud_options.refresh(session, _orchestrator_cloud_options)


@app.post("/api/catalogue/refresh")
def refresh_cloud_options_now(
    requester: str = Depends(_authed_requester),
) -> dict:
    """Refresh the cloud option cache immediately (platform admin).

    The hourly thread does this on its own; this is the "refresh now" for an
    admin who has just changed the allowlist, and how the integration gets
    verified without waiting an hour.
    """
    if roles_mod.PLATFORM_ADMIN not in roles_mod.resolve_roles(requester):
        raise HTTPException(
            status_code=403,
            detail="Only a platform administrator can refresh the cloud catalogue.",
        )
    return _refresh_cloud_options_once()


@app.get("/api/admin/settings")
def list_settings(_auth: str = Depends(require_action("manage_settings"))) -> dict:
    """The editable runtime settings + the read-only .env-managed posture (F-OPS-09).

    Only non-secret, allow-listed governance/FinOps/AI keys are editable, each with
    its current value and source (database / env / default). Secrets and the
    security/provisioning switches are shown read-only and managed in .env — this
    endpoint never returns a secret value.
    """
    editable = [
        {
            "key": key,
            "label": meta["label"],
            "help": meta["help"],
            "type": meta["type"],
            "group": meta["group"],
            "choices": meta.get("choices"),
            "default": meta["default"],
            "value": settings.effective(key),
            "source": settings.source_of(key),
        }
        for key, meta in settings.ALLOWLIST.items()
    ]
    read_only = [
        {
            "key": key,
            "label": label,
            "value": os.getenv(key) or "(default)",
            "source": "env" if os.getenv(key) is not None else "default",
        }
        for key, label in settings.READ_ONLY_ENV.items()
    ]
    # The execution gates that decide whether real, billable infrastructure can be
    # created. They live in the orchestrator's environment, so they are fetched
    # from it rather than guessed — and reported as unavailable if it is down.
    posture = _orchestrator_posture()
    execution: list[dict] = []
    for key, label in _EXECUTION_GATE_LABELS.items():
        if posture is None:
            value, source = "unavailable", "unreachable"
        elif key not in posture:
            # An orchestrator that predates this gate (mid-rollout, or a version
            # skew). Say so rather than rendering a bare None as if it were a value.
            value, source = "unknown", "orchestrator (older version)"
        else:
            raw = posture[key]
            value = ("on" if raw else "off") if isinstance(raw, bool) else str(raw)
            source = "orchestrator .env"
        execution.append({"key": key, "label": label, "value": value, "source": source})

    return {
        "editable": editable,
        "read_only": read_only,
        "execution": execution,
        "execution_available": posture is not None,
        "note": ("Secrets (API keys, credentials) and security/provisioning switches "
                 "are managed in .env / the vault and are never editable here."),
    }


def _orchestrator_blueprints() -> list[dict] | None:
    """The build recipes the orchestrator actually ships, or None if unreachable.

    Read from the executing layer rather than assumed, so the portal can never
    advertise a blueprint that isn't there — and can flag the reverse, a
    certified blueprint the orchestrator has lost, which is the dangerous state.
    """
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(), "operation": "blueprints"}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _err = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw), path="/blueprints")
    if response is None or response.status_code != 200:
        return None
    try:
        return (response.json() or {}).get("available") or []
    except ValueError:
        return None


def _orchestrator_network_egress() -> dict | None:
    """What the subnet the orchestrator builds VMs in can reach, or None.

    Read from the executing layer, never assumed: the API holds no cloud
    credentials and cannot see a route table, so guessing here would be exactly
    the sort of confident wrongness that offered Ubuntu on a subnet with no
    route to the internet.
    """
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(),
               "operation": "network-egress"}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _err = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw),
                                           path="/network/egress")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json() or {}
    except ValueError:
        return None


class BlueprintIn(BaseModel):
    technology_code: str
    deployment_target: str
    blueprint_ref: str
    version: str = ""
    notes: str | None = None


@app.get("/api/blueprints")
def list_blueprints(session: Session = Depends(get_session),
                    _auth: str = Depends(require_action("manage_settings"))) -> dict:
    """The technology → blueprint map, per cloud (F-CAT-10).

    Merges what the orchestrator SHIPS with what an admin has CERTIFIED, so the
    page shows three distinct states rather than a flat list:
      certified  — approved; the portal builds this automatically
      available  — the recipe exists but nobody has approved it yet
      missing    — certified here but absent from the orchestrator (a red flag)
    """
    shipped = _orchestrator_blueprints()
    certified = {(b.technology_code, b.deployment_target): b
                 for b in session.scalars(select(Blueprint)).all()}

    # What each shipped recipe can build, flattened to (technology, target).
    available: dict[tuple[str, str], dict] = {}
    for bp in shipped or []:
        for code in bp.get("builds", []):
            available[(code, bp.get("target"))] = bp

    # Every (technology, target) the catalogue actually offers — so a gap shows as
    # a row with status 'none' rather than being invisible. Respects each
    # technology's own targets, so nonsense pairs (S3 on OCI) never appear.
    offered: set[tuple[str, str]] = set()
    for tech in session.scalars(select(Technology).where(Technology.lifecycle_state != "eol")).all():
        for target in (tech.targets or "").split(","):
            target = target.strip()
            if target in DEPLOYMENT_TARGETS:
                offered.add((tech.code, target))

    rows = []
    for key in sorted(offered | set(available) | set(certified)):
        code, target = key
        row_cert, row_avail = certified.get(key), available.get(key)
        if row_cert and row_cert.status == "certified":
            # Certified but the orchestrator no longer ships it — the dangerous
            # direction, surfaced rather than quietly dropped.
            state = "certified" if row_avail else "missing"
        elif row_cert and row_cert.status == certification.STALE:
            # Certified once, and the proof has aged out (ARCHITECTURE.md P8).
            # Not the same as suspended: nothing failed, the evidence simply
            # expired. An admin re-proves it rather than investigating it.
            state = "stale"
        elif row_cert and row_cert.status == certification.SUSPENDED:
            # WITHDRAWN BY EVIDENCE (C1), not merely un-approved. Its own state,
            # because "nobody certified this yet" and "this was certified and
            # then failed three times running" mean very different things to an
            # admin deciding what to do next. The reason travels with it.
            state = "suspended"
        elif row_avail:
            state = "draft"        # recipe exists, nobody has approved it
        elif row_cert:
            state = "draft"
        else:
            state = "none"         # no recipe at all — fulfilled by hand
        rows.append({
            "technology_code": code,
            "deployment_target": target,
            "blueprint_ref": (row_cert.blueprint_ref if row_cert else (row_avail or {}).get("ref", "")),
            "version": row_cert.version if row_cert else "",
            "state": state,
            "certified_by": row_cert.certified_by if row_cert else None,
            "certified_at": row_cert.certified_at.isoformat() if row_cert and row_cert.certified_at else None,
            # Why the portal withdrew it, when it did. A suspension with no
            # explanation is just an outage nobody can act on.
            "notes": (row_cert.notes if row_cert else None),
            "description": (row_avail or {}).get("description", ""),
            # From the manifest: whether this recipe's own preconditions are met.
            # Lets the page say 'certified but not configured' rather than the
            # request discovering it at apply time.
            "ready": (row_avail or {}).get("ready", True),
            "missing_config": (row_avail or {}).get("missing_config", []),
            "available_version": (row_avail or {}).get("version", ""),
        })
    return {
        "orchestrator_available": shipped is not None,
        "blueprints": rows,
        "certified": sum(1 for r in rows if r["state"] == "certified"),
        "missing": sum(1 for r in rows if r["state"] == "missing"),
        "suspended": sum(1 for r in rows if r["state"] == "suspended"),
        "stale": sum(1 for r in rows if r["state"] == "stale"),
        "targets": sorted(DEPLOYMENT_TARGETS),
    }


def _unproven_families(code: str, manifest: dict) -> set[str] | None:
    """OS families this blueprint offers `code` on that no machine has proven.

    None means the evidence could not be read at all — which is NOT the same as
    "nothing objected" and must never be treated as permission.

    A blueprint that offers no OS families builds no machine (a bucket, a managed
    database), so boot evidence cannot apply to it and this returns an empty set.
    That is a deliberate, stated gap: proving those needs the resource itself to
    be asked whether it exists and is healthy, which is separate work.
    """
    # A technology the blueprint itself says nothing can inspect. Kept ahead of
    # the family arithmetic because it is a different fact: not "unproven on
    # rhel", which is what a Windows machine used to be told, but "no machine can
    # report what this became". Returning an empty set here would let it through
    # exactly as a bucket does, and a bucket at least cannot be broken silently.
    blocked = blueprint_capabilities.unverifiable(code)
    if blocked:
        return {blocked}

    found = blueprint_capabilities.evidence(code)
    if not found["known"]:
        blueprint_capabilities.families_for(code, _orchestrator_blueprints)
        found = blueprint_capabilities.evidence(code)
    if not found["known"]:
        return None
    offered = {str(f).strip().lower() for f in (manifest.get("os_families") or [])}
    return offered - found["proven"] - found["refuted"]


@app.post("/api/blueprints")
def certify_blueprint(body: BlueprintIn, session: Session = Depends(get_session),
                      admin: str = Depends(require_action("manage_settings"))) -> dict:
    """Certify a blueprint for real provisioning (F-CAT-10). Audited.

    A deliberate human act: it is what turns a recipe the orchestrator merely has
    into one the portal will use to build real infrastructure.
    """
    code = (body.technology_code or "").strip()
    target = (body.deployment_target or "").strip().lower()
    if target not in DEPLOYMENT_TARGETS:
        raise HTTPException(status_code=422, detail=f"Unknown deployment target '{target}'.")
    if session.scalar(select(Technology).where(Technology.code == code)) is None:
        raise HTTPException(status_code=422, detail=f"Unknown technology '{code}'.")
    # Refuse to certify something the orchestrator doesn't have — that would be
    # advertising a capability that cannot run.
    shipped = _orchestrator_blueprints()
    if shipped is None:
        raise HTTPException(status_code=503,
                            detail="The orchestrator is unreachable, so its blueprints can't be confirmed.")
    manifest = next((bp for bp in shipped
                     if code in bp.get("builds", []) and bp.get("target") == target), None)
    if manifest is None:
        raise HTTPException(
            status_code=422,
            detail=f"The orchestrator ships no blueprint building '{code}' on {target}.")

    # THE GATE. Certification is what makes the portal build a thing for real, so
    # it must not rest on somebody's recollection that it once worked.
    #
    # The rule: for every OS family this blueprint offers the technology on, a
    # real machine must have been built and asked. A family that was built and
    # DISPROVEN is acceptable here — the form already refuses those images, so no
    # requester can reach the broken combination — but a family nobody has ever
    # tried is not.
    #
    # Standing requirement (user, 15 Aug 2026): "once you certify the component it
    # should not fail when the user selects the component."
    unproven = _unproven_families(code, manifest)
    if unproven is None:
        raise HTTPException(
            status_code=503,
            detail=("The portal cannot read what has been proven, so it cannot "
                    "tell whether this is safe to certify. Try again once the "
                    "orchestrator is reachable."))
    if unproven:
        raise HTTPException(
            status_code=422,
            detail=(f"{code} has not been proven on {', '.join(sorted(unproven))}. "
                    f"Certifying it would let a requester pick an operating system "
                    f"nobody has built it on. Raise a request that builds {code} on "
                    f"each of those images, let the machine report, then certify."))

    row = session.get(Blueprint, (code, target))
    now = datetime.now(timezone.utc)
    if row is None:
        row = Blueprint(technology_code=code, deployment_target=target)
        session.add(row)
    row.blueprint_ref = (body.blueprint_ref or "").strip()
    # From the manifest, so provisioning derives what to build from the SAME
    # source as the catalogue badge.
    row.resource_kind = manifest.get("resource_kind", "")
    row.version = (body.version or "").strip()
    row.notes = body.notes
    row.status = "certified"
    row.certified_by = admin
    row.certified_at = now
    append_audit(session, "blueprint.certified", actor=admin,
                 detail={"technology": code, "target": target,
                         "ref": row.blueprint_ref, "version": row.version})
    session.commit()
    fulfilment.invalidate_cache()  # the catalogue badge follows certification
    return {"technology_code": code, "deployment_target": target, "state": "certified"}


@app.delete("/api/blueprints/{technology_code}/{deployment_target}")
def decertify_blueprint(technology_code: str, deployment_target: str,
                        session: Session = Depends(get_session),
                        admin: str = Depends(require_action("manage_settings"))) -> dict:
    """Withdraw certification. The recipe stays in the orchestrator; the portal
    stops treating it as automated. Audited."""
    row = session.get(Blueprint, (technology_code, deployment_target.lower()))
    if row is None:
        raise HTTPException(status_code=404, detail="That blueprint is not certified.")
    session.delete(row)
    append_audit(session, "blueprint.decertified", actor=admin,
                 detail={"technology": technology_code, "target": deployment_target})
    session.commit()
    fulfilment.invalidate_cache()
    return {"technology_code": technology_code, "deployment_target": deployment_target,
            "state": "available"}


class UserRoleIn(BaseModel):
    email: str
    role: str


@app.get("/api/access/user-roles")
def list_user_roles(session: Session = Depends(get_session),
                    _auth: str = Depends(require_action("manage_access"))) -> dict:
    """Who holds which portal role (F-IAM-01).

    The portal's own person -> role list. It exists because there is no external
    source here: Entra provides login only, and Jira's authority model is
    per-ticket Assignment Groups, which cannot answer 'who may administer the
    portal'. Identity stays federated; authorisation is maintained here.
    """
    rows = session.scalars(select(UserRole).order_by(UserRole.email, UserRole.role)).all()
    people: dict[str, list[str]] = {}
    for row in rows:
        people.setdefault(row.email, []).append(row.role)
    return {
        "role_source": roles_mod.role_source(),
        "default_role": sorted(roles_mod._default_roles()),
        "bootstrap_admins": sorted(roles_mod.bootstrap_admins()),
        "roles": sorted(roles_mod.ALL_ROLES),
        "users": [{"email": e, "roles": sorted(r)} for e, r in sorted(people.items())],
    }


@app.post("/api/access/user-roles")
def grant_user_role(body: UserRoleIn, session: Session = Depends(get_session),
                    admin: str = Depends(require_action("manage_access"))) -> dict:
    """Grant a portal role to a person. Audited (`access.role_granted`)."""
    email = (body.email or "").strip().lower()
    role = (body.role or "").strip()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Enter the person's email address.")
    if role not in roles_mod.ALL_ROLES:
        raise HTTPException(status_code=422,
                            detail=f"Unknown role. Choose one of: {', '.join(sorted(roles_mod.ALL_ROLES))}.")
    if session.get(UserRole, (email, role)) is None:
        session.add(UserRole(email=email, role=role, granted_by=admin))
        append_audit(session, "access.role_granted", actor=admin,
                     detail={"email": email, "role": role})
        session.commit()
    return {"email": email, "role": role, "granted": True}


@app.delete("/api/access/user-roles/{email}/{role}")
def revoke_user_role(email: str, role: str, session: Session = Depends(get_session),
                     admin: str = Depends(require_action("manage_access"))) -> dict:
    """Remove a portal role from a person. Audited (`access.role_revoked`).

    Removing every role does not lock the person out — they fall back to the
    default role — and PORTAL_BOOTSTRAP_ADMINS always restores administrator
    access, so the console can never be locked away entirely.
    """
    email = (email or "").strip().lower()
    row = session.get(UserRole, (email, role))
    if row is None:
        raise HTTPException(status_code=404, detail="That person does not hold that role.")
    session.delete(row)
    append_audit(session, "access.role_revoked", actor=admin,
                 detail={"email": email, "role": role})
    session.commit()
    return {"email": email, "role": role, "granted": False}


@app.get("/api/admin/policies")
def list_policies(_auth: str = Depends(require_action("manage_settings"))) -> dict:
    """The governance rules OPA currently enforces (F-GOV-03).

    Read from the RUNNING OPA, with each rule's description taken from its own
    `# METADATA` annotation — so the page shows what is actually loaded, and a
    description cannot drift from the rule it describes. Read-only: policy is
    code, reviewed and deployed; editing the governance authority from a browser
    is the same category as editing the approval gate.
    """
    try:
        return {**policy.describe_policies(), "available": True}
    except policy.PolicyUnavailable as exc:
        # Never imply "no rules" when the truth is "cannot tell".
        return {"available": False, "error": str(exc), "overview": None,
                "rules": [], "blocking": 0, "advisory": 0, "modules": []}


@app.put("/api/admin/settings/{key}")
def set_setting(
    key: str,
    body: SettingIn,
    session: Session = Depends(get_session),
    admin: str = Depends(require_action("manage_settings")),
) -> dict:
    """Override a runtime setting from the Admin console (F-OPS-09). Validated,
    audited (`setting.changed`), and takes effect on the next read. Only
    allow-listed non-secret keys are accepted."""
    if key not in settings.ALLOWLIST:
        raise HTTPException(status_code=404, detail="Unknown or non-editable setting.")
    try:
        value = settings.coerce(key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    before = settings.effective(key)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=admin))
    else:
        row.value = value
        row.updated_by = admin
        row.updated_at = datetime.now(timezone.utc)
    append_audit(session, "setting.changed", actor=admin,
                 detail={"key": key, "from": before, "to": value})
    session.commit()
    settings.invalidate_cache()
    return {"key": key, "value": settings.effective(key), "source": settings.source_of(key)}


@app.delete("/api/admin/settings/{key}")
def reset_setting(
    key: str,
    session: Session = Depends(get_session),
    admin: str = Depends(require_action("manage_settings")),
) -> dict:
    """Remove a runtime override so the setting reverts to its .env / built-in
    default (F-OPS-09). Audited (`setting.reset`)."""
    if key not in settings.ALLOWLIST:
        raise HTTPException(status_code=404, detail="Unknown or non-editable setting.")
    row = session.get(Setting, key)
    if row is not None:
        before = row.value
        session.delete(row)
        append_audit(session, "setting.reset", actor=admin, detail={"key": key, "from": before})
        session.commit()
        settings.invalidate_cache()
    return {"key": key, "value": settings.effective(key), "source": settings.source_of(key)}


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
            "in_flight": sum_of("submitted", "planned", "in-progress",
                                "manual-fulfil"),
            "failed": sum_of("apply-failed", "teardown-failed", "rejected",
                             "verify-failed"),
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
    return settings.env("BUDGET_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _budget_warn_pct() -> float:
    try:
        return max(0.0, min(100.0, float(settings.env("BUDGET_WARN_PCT", "90"))))
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
    return settings.env("QUOTA_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _environment_count(session: Session, project_code: str | None,
                       exclude_ref: str | None = None) -> int:
    """A project's active environments: committed (provisioned + in-flight)
    'create' requests, optionally excluding one."""
    if not project_code:
        return 0
    stmt = (select(func.count()).select_from(Request)
            .where(Request.project_code == project_code,
                   Request.request_type.in_(tuple(CREATE_LIKE_TYPES)),
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


def _project_expiry_enforce() -> bool:
    """Whether a request against an EXPIRED project is a hard block. Off by
    default, matching the budget and quota gates: an expiry date that silently
    started refusing work would be a surprise, and expiry arrives by the calendar
    rather than by anyone's decision. Disabling a project refuses regardless —
    that is deliberate, this is not."""
    return settings.env("PROJECT_EXPIRY_ENFORCED", "false").strip().lower() in (
        "1", "true", "yes", "on")


def _project_expiry_warn_days() -> int:
    try:
        return max(0, int(settings.env("PROJECT_EXPIRY_WARN_DAYS", "30")))
    except (ValueError, TypeError):
        return 30


def _project_expiry_status(session: Session, project_code: str | None) -> dict | None:
    """Where a project stands against its expiry date.

    None when there is no project, no such project, or no expiry set — most
    projects should have none, and an absent date must never gate anything.
    """
    if not project_code:
        return None
    row = session.scalar(select(Project).where(Project.code == project_code))
    if row is None or row.expires_at is None:
        return None
    days_left = (row.expires_at - date.today()).days
    if days_left < 0:
        status = "expired"
    elif days_left <= _project_expiry_warn_days():
        status = "expiring"
    else:
        status = "ok"
    return {"project": project_code, "expires_at": row.expires_at.isoformat(),
            "days_left": days_left, "status": status,
            "owner": row.owner_email or ""}


def _project_expiry_message(p: dict) -> str:
    owner = f" Owner: {p['owner']}." if p["owner"] else ""
    if p["status"] == "expired":
        return (f"Project {p['project']} expired on {p['expires_at']} "
                f"({abs(p['days_left'])} days ago).{owner}")
    return (f"Project {p['project']} expires on {p['expires_at']} "
            f"(in {p['days_left']} days).{owner}")


class ProjectIn(BaseModel):
    """A project as an administrator supplies it.

    `requested_by` is deliberately absent: it comes from the signed-in identity,
    so it records who actually did this rather than who claimed to.
    """
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    owner_email: str | None = Field(default=None, max_length=160)
    cost_centre_code: str | None = Field(default=None, max_length=32)
    expires_at: date | None = None
    active: bool = True

    @field_validator("code")
    @classmethod
    def _code_shape(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,31}", v):
            raise ValueError("Code must be letters, digits and hyphens, starting "
                             "with a letter or digit.")
        return v


def _project_out(p: Project, session: Session) -> dict:
    """A project plus the facts an administrator needs before changing it: what it
    already owns, and where it stands against its expiry date."""
    expiry = _project_expiry_status(session, p.code)
    return {
        "code": p.code, "name": p.name, "description": p.description,
        "owner_email": p.owner_email, "cost_centre_code": p.cost_centre_code,
        "requested_by": p.requested_by, "requested_by_name": p.requested_by_name,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "active": bool(p.active),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "expiry_status": (expiry or {}).get("status", "none"),
        "days_left": (expiry or {}).get("days_left"),
        # What would be affected by disabling or deleting it.
        "request_count": session.scalar(
            select(func.count()).select_from(Request)
            .where(Request.project_code == p.code)) or 0,
        "environment_count": session.scalar(
            select(func.count()).select_from(Environment)
            .where(Environment.project_id == p.id)) or 0,
    }


@app.get("/api/projects")
def list_projects(session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Every project, including disabled ones — the console must show what it can
    re-enable. The request form's dropdown is a different list (/api/lookups) and
    shows only active projects."""
    rows = session.scalars(select(Project).order_by(Project.code)).all()
    return {"projects": [_project_out(p, session) for p in rows]}


@app.post("/api/projects")
def set_project(body: ProjectIn, session: Session = Depends(get_session),
                _auth: str = Depends(require_action("execute")),
                requester_name: str | None = Depends(get_requester_name)) -> dict:
    """Create or update a project. platform_admin.

    Upsert by code, matching how budgets and quotas behave. `requested_by` is
    stamped from the signed-in identity on FIRST creation only — it records who
    asked for the project, not who last edited it; edits are in the audit trail.
    """
    row = session.scalar(select(Project).where(Project.code == body.code))
    created = row is None
    if created:
        row = Project(code=body.code)
        row.requested_by = _auth
        row.requested_by_name = requester_name
        session.add(row)

    before = None if created else {
        "name": row.name, "active": bool(row.active),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "owner_email": row.owner_email,
    }
    row.name = body.name
    row.description = body.description
    row.owner_email = body.owner_email
    row.cost_centre_code = body.cost_centre_code
    row.expires_at = body.expires_at
    row.active = body.active

    append_audit(session, "project.created" if created else "project.updated",
                 actor=_auth,
                 detail={"code": row.code, "name": row.name, "active": row.active,
                         "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                         "owner_email": row.owner_email, "before": before})
    session.commit()
    return _project_out(row, session)


@app.delete("/api/projects/{code}")
def delete_project(code: str, session: Session = Depends(get_session),
                   _auth: str = Depends(require_action("execute"))) -> dict:
    """Delete a project. platform_admin.

    REFUSED once anything references it. Requests carry the project code as part
    of their permanent record and environments hold a foreign key to it, so
    deleting would either break that key or leave history pointing at a project
    that no longer exists. Disabling is the operation that was actually wanted:
    it stops new requests and keeps the record intact.
    """
    row = session.scalar(select(Project).where(Project.code == code.strip().upper()))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No project {code}.")
    requests = session.scalar(select(func.count()).select_from(Request)
                              .where(Request.project_code == row.code)) or 0
    envs = session.scalar(select(func.count()).select_from(Environment)
                          .where(Environment.project_id == row.id)) or 0
    if requests or envs:
        raise HTTPException(
            status_code=409,
            detail=(f"Project {row.code} is referenced by {requests} request(s) and "
                    f"{envs} environment(s), so deleting it would break that history. "
                    "Disable it instead — that stops new requests and keeps the record."))
    session.delete(row)
    append_audit(session, "project.deleted", actor=_auth, detail={"code": row.code})
    session.commit()
    return {"deleted": True, "code": row.code}


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
    """Liveness: the process is up and serving. Used by the load balancer to know
    the instance is alive (F-OPS-01)."""
    return {"ok": True, "mock": is_mock_mode()}


@app.get("/health/ready")
def health_ready() -> dict:
    """Readiness (F-OPS-01): the instance can serve — its database is reachable.
    A load balancer routes only to ready instances; returns 503 if the DB is down."""
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="database not reachable") from exc
    return {"ready": True, "mock": is_mock_mode()}


class RateLimitHitIn(BaseModel):
    identity: str = "?"


@app.post("/internal/ratelimit/hit")
def internal_ratelimit_hit(body: RateLimitHitIn, session: Session = Depends(get_session),
                           x_internal_key: str = Header(default="")) -> dict:
    """Service-to-service (F-OPS-01): increment the shared rate-limit counter for
    `identity` in the current window and return the running count. Lets the BFF
    share the same counter so its limit is global across replicas too. Guarded by
    INTERNAL_API_KEY; disabled (503) until that secret is configured."""
    secret = os.getenv("INTERNAL_API_KEY", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Internal rate-limit endpoint is not configured.")
    if not hmac.compare_digest(x_internal_key or "", secret):
        raise HTTPException(status_code=403, detail="Invalid internal key.")
    return {"count": ratelimit.hit(session, body.identity)}


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
    # Deployment targets this technology is available on (list of target codes),
    # so the form can filter the catalogue by the selected target (F-CAT).
    targets: list[str] = []
    # Of those targets, the ones the orchestrator provisions AUTOMATICALLY. On any
    # other target the request is governed here and then fulfilled by the
    # infrastructure team — the form shows that plainly so the catalogue never
    # promises more than the platform delivers (GAP-ANALYSIS.md step 1).
    automated_targets: list[str] = []

    @field_validator("targets", mode="before")
    @classmethod
    def _split_targets(cls, v):
        """The DB stores a CSV; expose it as a list."""
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v or []


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
    # Capabilities, offered separately from components. They have nothing to
    # size, nothing to price and nothing to build, so putting them in the same
    # list as NGINX asked a requester to choose between two different kinds of
    # thing without telling them so.
    platform_services: list[dict] = []


def _technology_out(tech: Technology, certified: set | None = None) -> TechnologyOut:
    """A catalogue entry plus the targets where it is genuinely automated, so the
    form can tell the requester which items need the infrastructure team.

    `certified` is the blueprint registry's certified (technology, target) pairs —
    passed in so building the whole catalogue costs one lookup, not one per row.
    """
    out = TechnologyOut.model_validate(tech)
    out.automated_targets = fulfilment.automated_targets(tech, out.targets, certified)
    return out


def _is_capability(code: str, session: Session) -> bool:
    """Whether this catalogue entry is an outcome rather than an installable thing."""
    from api import ai_blueprint

    return ai_blueprint.delivery_model(code, session) == "capability"


@app.get("/api/lookups", response_model=LookupsResponse)
def lookups(session: Session = Depends(get_session)) -> LookupsResponse:
    """Read-only reference data for the guided-request form's dropdowns."""
    return LookupsResponse(
        # Active only. The flag existed but was ignored here, so a project
        # disabled in the admin console still appeared in the form — the switch
        # was there and did nothing. Subsidiaries below always filtered; projects
        # did not.
        projects=session.scalars(
            select(Project).where(Project.active.is_(True)).order_by(Project.name)
        ).all(),
        cost_centres=session.scalars(select(CostCentre).order_by(CostCentre.name)).all(),
        # Subsidiaries are synced from Jira (customfield_36200) in live mode; show
        # only the active ones (Jira-removed ones are deactivated, not deleted).
        subsidiaries=session.scalars(
            select(Subsidiary).where(Subsidiary.active.is_(True)).order_by(Subsidiary.name)
        ).all(),
        # CAPABILITIES ARE NOT COMPONENTS. "Backup & Recovery", "Centralised
        # Logging" and their like have no package, no archive and no cloud
        # resource behind them — nothing can ever provision one. Listing them
        # beside NGINX invited a requester to select infrastructure and receive a
        # work item, and REQ-2026-0183 spent a real machine discovering that
        # `dnf install backup` finds nothing.
        #
        # They keep their route: a `platform-service` request asks the
        # infrastructure team directly, with no sizing, no price and no build
        # path to fail at.
        technologies=[_technology_out(t, fulfilment.certified_pairs(session)) for t in
                      session.scalars(select(Technology).order_by(Technology.name)).all()
                      if not _is_capability(t.code, session)],
        # Offered separately, so the form can ask for one without pretending it
        # is a component.
        platform_services=[
            {"code": t.code, "name": t.name,
             "note": ai_blueprint.delivery_note(t.code, session)}
            for t in session.scalars(select(Technology).order_by(Technology.name)).all()
            if _is_capability(t.code, session)],
        environments=session.scalars(select(Environment).order_by(Environment.name)).all(),
    )


@app.get("/api/catalogue/component-options")
def component_option_list(
    technology: str,
    target: str = "",
    image: str = "",
    session: Session = Depends(get_session),
) -> dict:
    """What the component detail form may offer for one technology on one target.

    Deliberately the SAME function that validation calls, so the dropdowns and
    the rules cannot drift apart. `presets` are the size anchors, which the form
    uses to fill the boxes when a size button is clicked — previously a
    hard-coded table in the browser that could disagree with what was priced.
    """
    return component_options.options_for(session, technology, target, image)


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


class AiRecommendIn(BaseModel):
    description: str
    data_classification: str | None = None


@app.post("/api/ai/recommend")
def ai_recommend_endpoint(
    body: AiRecommendIn,
    session: Session = Depends(get_session),
    requester: str = Depends(require_action("create_request")),
) -> dict:
    """Recommend a cloud + component sizes from a plain-English workload (E4).

    The AI **recommends only** (ARCHITECTURE.md §7): it picks a catalogue stack,
    and the portal — not the model — prices that stack across every cloud it can
    run on so the requester can compare and choose. It never sets an authoritative
    charge, submits a request, or provisions. The requester reviews it and can
    pre-fill the form with one click, where validate_submission and the pricing
    engine re-check everything. Every recommendation is audited.
    """
    description = (body.description or "").strip()
    if len(description) < 8:
        raise HTTPException(
            status_code=422,
            detail="Describe the workload in a sentence or two (at least 8 characters).",
        )
    try:
        result = ai_recommend.recommend(
            description, session, classification=body.data_classification
        )
    except ai_recommend.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    append_audit(
        session,
        "ai.recommended",
        actor=requester,
        detail={
            "mode": result["mode"],
            "description": description[:500],
            "recommended_target": result["recommended_target"],
            "eligible_targets": result["eligible_targets"],
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
    "dns_name",
    "dns_type",
    "dns_value",
    "data_classification",
    # Governance metadata (increment 6.1).
    "business_justification",
    "priority",
    "business_criticality",
    "required_delivery_date",
    "expires_on",
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
    # Component detail form. All optional: a draft mid-way through the form, and
    # every request raised before the form existed, carries none of them and
    # resolves from the size anchor. Values are checked against what the server
    # offers in api.component_options — never trusted as sent.
    version: str | None = None
    image: str | None = None
    vcpu: int | None = None
    memory_gb: int | None = None
    storage_gb: int | None = None


class ComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    technology_code: str | None = None
    size: str | None = None
    version: str | None = None
    image: str | None = None
    vcpu: int | None = None
    memory_gb: int | None = None
    storage_gb: int | None = None
    # Whether the resource this component maps to is still running. Null when
    # nothing has been provisioned yet (a draft), so "unknown" stays distinct
    # from "gone". A partial decommission leaves some components dead and the
    # rest alive, and offering a dead one for teardown again is offering to
    # destroy something that no longer exists.
    active: bool | None = None


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
    dns_name: str | None = None
    dns_type: str | None = None
    dns_value: str | None = None
    data_classification: str | None = None
    # Governance metadata (increment 6.1). All optional at draft time.
    business_justification: str | None = None
    priority: str | None = None
    business_criticality: str | None = None
    required_delivery_date: date | None = None
    expires_on: date | None = None
    application_owner: str | None = None
    business_owner: str | None = None
    technical_owner: str | None = None
    environment_owner: str | None = None
    owner_group: str | None = None
    advanced_options: dict | None = None
    components: list[ComponentIn] | None = None

    @field_validator("required_delivery_date", "expires_on", mode="before")
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
    sla_hours = float(settings.env("APPROVAL_SLA_HOURS", "24"))
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
    dns_name: str | None = None
    dns_type: str | None = None
    dns_value: str | None = None
    data_classification: str | None = None
    # Governance metadata (increment 6.1).
    business_justification: str | None = None
    priority: str | None = None
    business_criticality: str | None = None
    required_delivery_date: date | None = None
    expires_on: date | None = None
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
    # What was actually built (F-INT-04): per resource, its OCID, display name,
    # private IP, hostname and URL. Terraform has always recorded these; until now
    # nothing showed them, so a requester could not find their own server.
    resources: list | None = None
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
    # The tier is STORED canonically even though validation accepts the old
    # lowercase spellings. It is the key for the per-tier network map, so "test"
    # and "Test" sitting side by side in the column would be two tiers as far as
    # a lookup is concerned — and one of them would map to no VCN.
    if provided.get("environment_tier"):
        provided["environment_tier"] = (
            normalise_tier(provided["environment_tier"])
            or provided["environment_tier"])
    for field in REQUEST_FIELDS:
        if field in provided:
            setattr(req, field, provided[field])

    # If components were sent, replace the request's component list with them.
    if body.components is not None:
        req.components = [
            RequestComponent(
                technology_code=c.technology_code, size=c.size, version=c.version,
                image=c.image, vcpu=c.vcpu, memory_gb=c.memory_gb,
                storage_gb=c.storage_gb,
            )
            for c in body.components
        ]

    req.status = "draft"
    session.commit()
    return RequestOut.model_validate(req)


@app.get("/api/requests", response_model=list[RequestOut])
def list_requests(
    requester: str | None = None,
    requested_by: str | None = None,
    reference: str | None = None,
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
    if reference:
        stmt = stmt.where(Request.reference == reference)
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
    resource_map: dict[str, list[dict]] = {}
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
        # What each request actually built. Batched with the rest rather than
        # queried per row: this list is polled every few seconds.
        for res in session.scalars(
            select(ProvisionedResource)
            .where(ProvisionedResource.reference.in_(refs))
            .order_by(ProvisionedResource.created_at.desc())
        ).all():
            resource_map.setdefault(res.reference, []).append(
                resource_details.summarise(res))
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
        out.resources = resource_map.get(r.reference) or None
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


def _environment_resource_kinds(session: Session, req: Request) -> list[str]:
    """EVERY cloud resource this environment provisions, in a stable order.

    A request is a stack: "Apache and a VM" is two resources, not one. This used
    to collapse to a single kind and the extras were dropped without a word — the
    request still reported success, so REQ-2026-0094 asked for Apache plus a VM,
    built Apache alone, and closed its Jira ticket as Resolved.

    Ordering is by kind so a workspace layout stays stable across re-plans.
    """
    target = (req.deployment_target or "").strip().lower()
    codes = [c.technology_code for c in req.components if c.technology_code]

    if codes:
        certified = session.scalars(
            select(Blueprint).where(
                Blueprint.status == "certified",
                Blueprint.deployment_target == target,
                Blueprint.technology_code.in_(codes),
                Blueprint.resource_kind != "",
            )
        ).all()
        if certified:
            return sorted({b.resource_kind for b in certified})

    # No certified blueprint anywhere in the stack: the legacy derivation, which
    # yields exactly one kind.
    return [_legacy_resource_kind(session, req)]


def _legacy_resource_kind(session: Session, req: Request) -> str:
    """The pre-blueprint derivation, still driving technologies whose recipes are
    branches in the shared module rather than a blueprint of their own."""
    target = (req.deployment_target or "").strip().lower()
    codes = [c.technology_code for c in req.components if c.technology_code]
    if target == "aws":
        return "aws-bucket"
    if not codes:
        return "oci-bucket"
    kinds = session.scalars(
        select(Technology.resource_kind).where(Technology.code.in_(codes))
    ).all()
    if "oci-postgres" in kinds:
        return "oci-postgres"
    return "oci-instance" if "oci-instance" in kinds else "oci-bucket"


def _unautomated_components(session: Session, req: Request) -> list[str]:
    """Components this request cannot provision automatically, in request order.

    The catalogue already tells a requester this before they submit; until now
    provisioning ignored it and reported success regardless, so a request for
    Kafka quietly received an object-storage bucket and closed as Resolved.
    Same source of truth as the catalogue badge, so the two cannot disagree.
    """
    target = (req.deployment_target or "").strip().lower()
    certified = fulfilment.certified_pairs(session)
    unmet: list[str] = []
    for c in req.components:
        code = (c.technology_code or "").strip()
        if not code or code in unmet:
            continue
        if (code, target) not in certified:  # the same test the catalogue badge makes
            unmet.append(code)
    return unmet


def _environment_resource_kind(session: Session, req: Request) -> str:
    """The PRIMARY resource kind, for the places that still name a single one
    (the request row, a DNS or reduce handoff). Provisioning uses the full list —
    see _environment_resource_kinds."""
    return _environment_resource_kinds(session, req)[0]


@app.get("/api/requests/{reference}", response_model=RequestOut)
def get_request(reference: str, session: Session = Depends(get_session)) -> RequestOut:
    """Load a draft (or submitted request) so it can be resumed/viewed."""
    req = _load_request(reference, session)
    out = RequestOut.model_validate(req)
    _mark_component_lifecycle(session, req, out)
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
    out.resources = _resources_out(session, reference)
    return out


def _resources_out(session: Session, reference: str) -> list | None:
    """What this request actually built, for the request view (F-INT-04).

    Includes decommissioned rows: a torn-down environment should still show what
    it had, and hiding them makes a decommission look like nothing was ever
    provisioned. Newest first.
    """
    rows = session.scalars(
        select(ProvisionedResource)
        .where(ProvisionedResource.reference == reference)
        .order_by(ProvisionedResource.created_at.desc())
    ).all()
    return [resource_details.summarise(r) for r in rows] or None


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

    components_data = [component_options.as_dict(c) for c in req.components]
    data = {field: _jsonable(getattr(req, field)) for field in REQUEST_FIELDS}
    data["components"] = components_data
    # Carried for the duplicate-in-flight check, so it can tell "another request
    # is already doing this" from "this request is already a row in the table".
    #
    # Defence in depth, and honestly labelled as such: TODAY nothing reaches that
    # rule able to match itself, because a request is still `draft` here and
    # drafts are excluded, and because the early return above makes a re-submit
    # idempotent. Removing this line therefore breaks no test — I tried. It earns
    # its place by making the rule correct on its own terms rather than by
    # accident of two other decisions, either of which could reasonably change.
    #
    # Deliberately NOT added to REQUEST_FIELDS: that tuple also shapes the policy
    # input and the signed orchestrator handoff, and neither needs it.
    data["reference"] = req.reference
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

    # A PLATFORM SERVICE HAS NO PRICE, and that is not the same as costing zero.
    # It buys the infrastructure team's time, not a cloud resource, so there is
    # nothing for estimate_cost to compute and nothing for the cost guard to
    # re-validate. Pricing it would produce the fictional 0.00 this portal spent
    # a day removing.
    if req.request_type == "platform-service":
        components_data = []

    # Server-computed estimate (1.6) — needed now for the budget guardrail below.
    breakdown = estimate_cost(components_data, req.deployment_target, session, req.advanced_options)
    monthly = float(breakdown["totals"]["monthly"])

    # A PRICE NOBODY CAN COMPUTE IS NOT A PRICE OF ZERO — SAY SO, DO NOT BLOCK.
    #
    # REQ-2026-0176 was quoted 0.00 AED for keycloak, approved at that figure,
    # and then refused at execution: "monthly 90.59 exceeds approved 0.00". The
    # execution guard was right; what was wrong was a fictional price reaching an
    # approver looking like a real one.
    #
    # THIS MUST NOT REFUSE THE SUBMISSION. Manual fulfilment is a documented
    # feature — the portal validates, prices, approves and audits requests the
    # infrastructure team builds by hand — and a component with no certified
    # blueprint is exactly the case it serves. Blocking here took kafka, mongodb,
    # vault, elasticsearch and every other uncertified technology off the portal
    # entirely; the whole of test_no_substitute_resource.py exists to say that
    # refusing manual fulfilment is not the fix for anything.
    #
    # So it warns, loudly, on the same channel as budget and policy warnings, and
    # the form shows "Not priced" instead of a figure while it is being filled in.
    # The approver sees an absence rather than a zero, which is the honest thing
    # a warning can do and a refusal cannot.
    unpriced_components = breakdown.get("unpriced") or []
    provisional_components = breakdown.get("provisional") or []
    if unpriced_components:
        append_audit(session, "cost.unpriceable", reference=req.reference,
                     detail={"components": unpriced_components})
    if provisional_components:
        # Priced, on a component nothing has certified yet. Recorded separately
        # from unpriceable because the approver is signing a REAL figure here —
        # the same arithmetic execution will do — and the evidence pack should
        # show that the number was provisional at the time it was approved.
        append_audit(session, "cost.provisional", reference=req.reference,
                     detail={"components": provisional_components,
                             "monthly": monthly})

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

    # Quota guardrail (F-FIN-08): cap environments per project — types that create
    # a new environment (create/clone/sandbox/temporary; add/resize/decommission don't).
    if req.request_type in CREATE_LIKE_TYPES:
        qcount = _environment_count(session, req.project_code, exclude_ref=req.reference)
        qstatus = _quota_status(session, req.project_code, qcount + 1)
        if qstatus is not None and qstatus["status"] == "over" and _quota_enforce():
            append_audit(session, "quota.blocked", reference=req.reference, detail=qstatus)
            session.commit()
            return JSONResponse(status_code=422, content={"quota_error": _quota_message(qstatus)})
        if qstatus is not None and qstatus["status"] in ("over", "near"):
            budget_warnings.append(_quota_message(qstatus))
            append_audit(session, "quota.warning", reference=req.reference, detail=qstatus)

    # Project lifetime. An expired project hard-blocks only when enforcement is
    # on; otherwise expiring or expired is an advisory warning. A project with no
    # expiry date is ungated, which is most of them. Nothing here touches what the
    # project already owns — a record reaching its date must never tear down
    # running infrastructure.
    pstatus = _project_expiry_status(session, req.project_code)
    if pstatus is not None and pstatus["status"] == "expired" and _project_expiry_enforce():
        append_audit(session, "project.blocked", reference=req.reference, detail=pstatus)
        session.commit()
        return JSONResponse(status_code=422,
                            content={"project_error": _project_expiry_message(pstatus)})
    if pstatus is not None and pstatus["status"] in ("expired", "expiring"):
        budget_warnings.append(_project_expiry_message(pstatus))
        append_audit(session, "project.warning", reference=req.reference, detail=pstatus)

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
    unpriced_warning = ([
        f"Not costed: {', '.join(str(u) for u in unpriced_components)}. No certified "
        f"blueprint says what this builds, and what it builds is what decides how it "
        f"is charged — so the estimate below excludes it and is NOT the whole cost. "
        f"It will be fulfilled by the infrastructure team unless it is certified first."
    ] if unpriced_components else [])
    # A DIFFERENT THING, SAID DIFFERENTLY. This figure is real — the same
    # calculation execution will make — but the component is not certified yet,
    # so whether it gets built this way is still open. Saying "not costed" here
    # would understate what is known; saying nothing would overstate it.
    provisional_warning = ([
        f"Provisional: {', '.join(str(c) for c in provisional_components)} is not "
        f"certified yet. The figure below is what it will cost if the portal builds "
        f"it as software on a machine, which is what it would do — but if that "
        f"cannot be proven, the infrastructure team fulfils it instead and the "
        f"final cost may differ."
    ] if provisional_components else [])
    warnings = (policy_warnings + budget_warnings + unpriced_warning
                + provisional_warning)

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
    # model_dump carries the detail fields too, so the live sizing panel reflects
    # an explicitly chosen shape rather than the size anchor it overrides.
    return resolve_components([c.model_dump() for c in body.components], session)


# --- Cost estimation (increment 1.5) -----------------------------------------


class CostIn(BaseModel):
    deployment_target: str | None = None
    components: list[ComponentIn] = []
    advanced_options: dict | None = None


@app.post("/api/cost")
def cost(body: CostIn, session: Session = Depends(get_session)) -> dict:
    """Estimate one-time/monthly/annual cost for the components on a target."""
    # The detail fields ride along, so the price the requester sees while filling
    # the form is the price of the shape they actually chose.
    return estimate_cost([c.model_dump() for c in body.components],
                         body.deployment_target, session, body.advanced_options)


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
    data["components"] = [component_options.as_dict(c) for c in req.components]
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
    return settings.env("SOD_ENFORCED", "true").strip().lower() in ("1", "true", "yes", "on")


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
    return settings.env("FOUR_EYES_ENFORCED", "true").strip().lower() in ("1", "true", "yes", "on")


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


def _max_provision_attempts() -> int:
    """How many failed handoffs before the poller stops retrying a request.
    Default 3: enough to ride out a transient error, few enough that a permanent
    one stops hammering the orchestrator within a minute or two."""
    try:
        return max(1, int(settings.env("PROVISION_MAX_ATTEMPTS", "3")))
    except (ValueError, TypeError):
        return 3


def _approval_quorum() -> int:
    """How many distinct approvers a request needs before provisioning (F-GOV-06).
    Default 1 = today's single-approver behaviour."""
    try:
        return max(1, int(settings.env("APPROVAL_QUORUM", "1")))
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

    # Reduce scales chosen components of the target down (F-CAT).
    if req.request_type == "reduce":
        return _reduce(session, req, actor="approver")

    # DNS gives the target environment a name (GAP-ANALYSIS step 5).
    if req.request_type == "dns":
        return _dns(session, req, actor="approver")

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


def _proof_contract_skew() -> str:
    """A sentence naming the mismatch if the two services disagree, else "".

    Compared against the orchestrator's own reported pattern rather than assumed
    equal: they run from separate images, and only one of them may have been
    rebuilt.
    """
    from common import proof_rules

    posture = _orchestrator_posture()
    if not posture:
        return ""          # unreachable is a different problem, reported elsewhere
    theirs = posture.get("proof_reference_pattern")
    if theirs and theirs != proof_rules._REFERENCE.pattern:
        return ("The API and the orchestrator disagree on what a proof reference "
                "looks like, so every proof would be refused. They share "
                "common/proof_rules.py — rebuild BOTH images.")
    return ""


def _autobuild_component(session: Session, code: str, target: str) -> dict:
    """Make one component buildable and certify it, on proof evidence.

    Assembles the real collaborators and hands them to autobuild.ensure. Kept
    here rather than inside the loop above so the request path reads as what it
    is — ask the agent, then re-check — and so the wiring has one home.
    """
    from api import autobuild, certification, proof, proof_wiring

    post = proof_wiring.make_post(_post_to_orchestrator, sign, WEBHOOK_SECRET)
    price = proof_wiring.make_price(session, target)
    verify = proof_wiring.make_verify(post)
    publish = proof_wiring.make_publish()
    withdraw = proof_wiring.make_withdraw()
    stored = proof_wiring.make_stored()

    def shipped(candidate: str) -> dict | None:
        """An existing recipe that already builds this, or None.

        nginx lives here: oci/service-vm builds it and four of its five
        technologies are certified. Nothing needs writing — it needs proving.
        """
        for manifest in (_orchestrator_blueprints() or []):
            if candidate in [str(b) for b in (manifest.get("builds") or [])]:
                if str(manifest.get("target", "")).lower() == target.lower():
                    return manifest
        return None

    def run_proof(sess, manifest):
        row = Blueprint(technology_code=code, deployment_target=target,
                        blueprint_ref=(manifest or {}).get("ref", f"{target}/{code}"),
                        resource_kind=(manifest or {}).get(
                            "resource_kind", f"{target}-{code}"),
                        status="draft")
        return proof.run_proof(sess, row, post=post, price=price, verify=verify,
                               capture=proof_wiring.make_capture(post))

    def certify(manifest, proof_reference):
        return certification.certify_from_proof(
            session, code, target, manifest.get("ref", f"{target}/{code}"),
            manifest.get("resource_kind", f"{target}-{code}"), proof_reference,
            version=manifest.get("version", ""))

    # BOTH SERVICES MUST AGREE ON WHAT A PROOF LOOKS LIKE. common/proof_rules.py
    # is shared, so a change to it needs BOTH images rebuilt — and when only one
    # was, every proof failed with "not a proof reference", which reads like an
    # attack rather than a stale container.
    skew = _proof_contract_skew()
    if skew:
        return {"status": "refused", "detail": skew}

    # Technologies a REVIEWED manifest already builds. Asked of the orchestrator
    # over the signed channel, never by importing it: orchestrator/ is not in this
    # image, so the import would pass every test and raise in the container.
    # `builds_generated` is subtracted so the agent's own earlier profiles do not
    # count as shipped — otherwise it could never correct one of its own.
    shipped_codes = frozenset(
        str(b) for m in (_orchestrator_blueprints() or [])
        for b in (set(m.get("builds") or []) - set(m.get("builds_generated") or [])))

    # Whether the build subnet can reach the public internet — asked of the
    # orchestrator, which is the layer that can see the network. An archive
    # install needs it and the OS-family check cannot see that.
    def reachable() -> bool:
        return network_egress.reaches_internet(_orchestrator_network_egress)

    # WHAT THE MACHINE THAT JUST SAID NO ALSO FOUND OUT (C7).
    #
    # REQ-2026-0188 was told "rabbitmq NOT INSTALLED" and stopped, while the
    # machine saying it had dnf, the full repository metadata, and the answer
    # (`rabbitmq-server`). Its report is fetched over the signed channel, exactly
    # as the boot-report endpoint already does — the orchestrator holds the
    # credential that can read it and this never imports the orchestrator.
    def discover(proof_reference: str, candidate: str) -> dict | None:
        if not proof_reference:
            return None
        reports = _orchestrator_boot_reports(proof_reference, ["oci-service-vm"])
        if not reports:
            return None
        # `reports` is a LIST of {kind, available, files: {name: body}} — the
        # shape the human-facing panel consumes. Flattened here rather than
        # guessed at: reading it as a dict would find nothing, silently, and the
        # rung would simply never appear.
        text = "\n".join(
            str(body)
            for entry in (reports.get("reports") or [])
            if isinstance(entry, dict)
            for body in (entry.get("files") or {}).values())
        # Three outcomes, and they are not interchangeable — see
        # discovery.finding_for, which is where that judgement lives so it can
        # be tested rather than buried in a closure.
        return discovery.finding_for(text, candidate)

    # WHEN NOTHING IS PACKAGED, ASK WHETHER ANYTHING IS PUBLISHED (C8).
    #
    # Three real machines established that RabbitMQ is not in Oracle Linux 9's
    # repositories or in EPEL 9. Its official image has been pulled nearly four
    # billion times. This is the question that follows a SOUND search finding
    # nothing — and it is answered for any technology without a row being typed.
    #
    # Fail-soft, deliberately: a public registry being unreachable must never
    # refuse a request, only mean there is no container rung this time.
    def find_image(candidate: str) -> dict | None:
        try:
            return registry.find(candidate)
        except Exception:  # noqa: BLE001 - a registry outage is not our failure
            return None

    # WHAT THE CONTAINER ACTUALLY BOUND, asked of the machine that just ran it.
    # The image declares its whole interface; a default container listens on a
    # fraction of it, and the difference is firewall surface on a real machine.
    def observe_ports(proof_reference: str, candidate: str) -> list[int]:
        if not proof_reference:
            return []
        reports = _orchestrator_boot_reports(proof_reference, ["oci-service-vm"])
        if not reports:
            return []
        text = "\n".join(
            str(body)
            for entry in (reports.get("reports") or [])
            if isinstance(entry, dict)
            for body in (entry.get("files") or {}).values())
        return discovery.listening_inside(text, candidate)

    def ask_repository(recipe):
        """What the repositories say about this recipe, before a machine (C10).

        Returns an objection or None, and None is NOT approval — it is the
        answer this gate gave before it existed. See api/repo_facts.py.
        """
        # The family the build machine boots. Read from the setting rather
        # than imported: `api/` must not import `orchestrator/`, and the
        # guarded import would pass under pytest and raise in the container.
        return repo_facts.check_recipe(
            recipe, family=(os.getenv("CONFIG_OS_FAMILY") or "rhel").strip().lower())

    result = autobuild.ensure(code, session, target=target, shipped=shipped,
                              run_proof=run_proof, publish=publish,
                              certify=certify, withdraw=withdraw, stored=stored,
                              shipped_codes=shipped_codes, reachable=reachable,
                              ask_repository=ask_repository,
                              search=repo_facts.search_packages,
                              discover=discover, find_image=find_image,
                              observe_ports=observe_ports)
    session.commit()
    return {"status": result.status, "detail": result.detail[:300]}


def _orchestrator_offered_versions() -> dict | None:
    """Which versions of each service the cloud currently offers, or None."""
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(),
               "operation": "catalogue-versions"}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _err = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw),
                                           path="/catalogue/versions")
    if response is None or response.status_code != 200:
        return None
    try:
        return (response.json() or {}).get("families") or {}
    except ValueError:
        return None


def _catalogue_gaps(session: Session) -> list[dict]:
    """What the cloud offers that the catalogue does not, and what it no longer will."""
    offered = _orchestrator_offered_versions()
    if offered is None:
        return []
    codes = [t.code for t in session.scalars(select(Technology)).all()]
    return catalogue_sync.find_gaps(
        offered, codes, pinned_kubernetes=os.getenv("OCI_OKE_KUBERNETES_VERSION", ""))


def _sweep_catalogue_gaps(session: Session) -> None:
    """Notice a catalogue gap ONCE, not every thirty seconds.

    A retirement is worth an audit entry the moment it appears — it means the
    portal is selling something the cloud will refuse. Repeating that on every
    sweep would bury it in its own noise, so the signature of the current gaps is
    compared with the last one recorded, and only a change is written.
    """
    gaps = _catalogue_gaps(session)
    signature = json.dumps([{k: g[k] for k in ("family", "retired", "newer")}
                            for g in gaps], sort_keys=True)
    last = session.scalars(
        select(AuditLog).where(AuditLog.event == "catalogue.gap")
        .order_by(desc(AuditLog.id)).limit(1)).first()
    if last is not None and (last.detail or {}).get("signature") == signature:
        return
    if not gaps and last is None:
        return          # nothing to say, and nothing said before
    append_audit(session, "catalogue.gap", actor="catalogue-sync",
                 detail={"signature": signature, "gaps": gaps})
    session.commit()


class DraftBlueprintIn(BaseModel):
    candidate: str
    deployment_target: str = "oci"


@app.post("/api/catalogue/autobuild")
def autobuild_candidate(body: DraftBlueprintIn, session: Session = Depends(get_session),
                        admin: str = Depends(require_action("manage_settings"))) -> dict:
    """Draft a recipe, prove it by building it, and publish it if it survives.

    The reviewer's requirement of 2026-08-21: the portal's agent writes its own
    Terraform. This is the trigger for that loop.

    EXPLICIT, NOT SCHEDULED. Every attempt is a real build in a real tenancy, and
    the cadence question is still open — a timer here would spend money on a
    schedule nobody has agreed. An admin asks for one candidate at a time.

    What it publishes is a DRAFT blueprint: discovered by the orchestrator,
    provable, and NOT offered to requesters until somebody certifies it. That
    last step is the human review ARCHITECTURE.md §7 requires, and the agent
    cannot perform it.
    """
    from api import autobuild, proof_wiring

    blueprint = session.get(Blueprint, (body.candidate, body.deployment_target.lower()))
    if blueprint is None:
        # A blueprint row is what the proof runner names and tags its resources
        # with. A candidate with no row has never been near this portal.
        blueprint = Blueprint(technology_code=body.candidate,
                              deployment_target=body.deployment_target.lower(),
                              blueprint_ref=f"{body.deployment_target}/{body.candidate}",
                              resource_kind=f"{body.deployment_target}-{body.candidate}",
                              status="draft")

    post = proof_wiring.make_post(_post_to_orchestrator, sign, WEBHOOK_SECRET)
    price = proof_wiring.make_price(session, body.deployment_target)
    verify = proof_wiring.make_verify(post)
    publish = proof_wiring.make_publish()

    def run_proof(sess, bp):
        from api import proof
        return proof.run_proof(sess, bp, post=post, price=price, verify=verify,
                               capture=proof_wiring.make_capture(post))

    result = autobuild.build(body.candidate, session, blueprint=blueprint,
                             run_proof=run_proof, publish=publish,
                             target=body.deployment_target)
    record = autobuild.summarise(result)
    append_audit(session, "blueprint.autobuilt", actor=admin, detail=record)
    session.commit()
    return {
        **record,
        "certified": False,
        "note": ("Published as a DRAFT. The orchestrator will discover it, but no "
                 "requester is offered it until it is certified — that review is "
                 "the one step the agent cannot do for you."
                 if result.published else result.detail),
    }


@app.post("/api/catalogue/draft")
def draft_blueprint(body: DraftBlueprintIn, session: Session = Depends(get_session),
                    admin: str = Depends(require_action("manage_settings"))) -> dict:
    """Propose a recipe for a catalogue candidate (C4). Applies nothing.

    ARCHITECTURE.md §7: the agent produces a PROPOSAL — files and reasoning,
    reviewable as a diff. Nothing here is written to the repository, no
    orchestrator is called, and nothing reaches a request until a human puts it
    there and a proof build passes.

    Every draft is linted against the defects this project has already paid for,
    whether a model wrote it or not. Four of the eight found on 2026-08-17 were
    written by an AI with the documentation open, so the checks are the gate.
    """
    try:
        proposal = ai_blueprint.draft(body.candidate, session,
                                      target=body.deployment_target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    append_audit(session, "blueprint.drafted", actor=admin,
                 detail={"candidate": body.candidate, "kind": proposal.kind,
                         "source": proposal.source,
                         "blocked": proposal.blocked,
                         "findings": [f.rule for f in proposal.findings]})
    session.commit()
    return {
        "candidate": proposal.candidate,
        "kind": proposal.kind,
        "source": proposal.source,
        "reasoning": proposal.reasoning,
        "files": proposal.files,
        "catalogue_rows": proposal.catalogue_rows,
        "findings": [{"severity": f.severity, "rule": f.rule, "detail": f.detail}
                     for f in proposal.findings],
        # Not "rejected" — a blocked draft is still worth reading, and the
        # findings are the most useful part of it.
        "blocked": proposal.blocked,
        "applied": False,
        "note": ("A proposal only. Nothing has been written to the repository and "
                 "nothing has been provisioned. It reaches a user only after a "
                 "human reviews the diff and a proof build passes."),
    }


@app.get("/api/catalogue/delivery")
def catalogue_delivery(target: str = "oci",
                       session: Session = Depends(get_session),
                       _auth: str = Depends(require_action("create_request"))) -> dict:
    """The catalogue grouped by HOW each entry is delivered, and who operates it.

    "SaaS or IaaS?" is the question people ask about a catalogue like this, and
    neither word answers it: nothing here is SaaS in the strict sense (a finished
    business application), and "IaaS" covers both a bare machine and a machine
    with Kafka on it — which are very different asks. The groups below are named
    for what actually differs: who runs it, who patches it, and whether there is
    a machine at all.

    It also exposes what the agent now reads instead of guessing from the code
    name, so the grouping a requester sees and the decision the agent makes come
    from ONE source. They were two, and the two disagreed: `postgres16` is OCI's
    managed database and the old rule read it as software.
    """
    from api import ai_blueprint
    from db.models import Technology

    target = (target or "oci").strip().lower()
    groups: dict[str, list[dict]] = {m: [] for m in
                                     ("managed", "software", "machine", "capability")}
    unrecorded: list[dict] = []

    certified = {b.technology_code: b for b in session.scalars(
        select(Blueprint).where(Blueprint.deployment_target == target))}

    for tech in session.scalars(select(Technology).order_by(Technology.code)):
        if target not in (tech.targets or ""):
            continue
        blueprint = certified.get(tech.code)
        entry = {
            "code": tech.code,
            "name": tech.name,
            "certified": blueprint is not None,
            "certified_by": blueprint.certified_by if blueprint else None,
            "resource_kind": blueprint.resource_kind if blueprint else None,
            "note": ai_blueprint.delivery_note(tech.code, session),
        }
        model = ai_blueprint.delivery_model(tech.code, session)
        if model in groups:
            groups[model].append(entry)
        else:
            # Not classified. Reported separately rather than defaulted into a
            # group, because "we have not decided" is a different fact from any
            # of the four and hiding it would make the catalogue look complete.
            entry["guessed_as"] = ai_blueprint.classify(tech.code, session)[0]
            unrecorded.append(entry)

    return {
        "target": target,
        "labels": {
            "managed": "Managed cloud services — the provider runs it",
            "software": "Self-managed software on a machine you own",
            "machine": "Bare infrastructure — a machine, nothing installed",
            "capability": "Capabilities — an outcome, not an installable thing",
        },
        "groups": groups,
        "unclassified": unrecorded,
        "totals": {model: len(items) for model, items in groups.items()}
              | {"unclassified": len(unrecorded)},
    }


@app.get("/api/catalogue/gaps")
def catalogue_gaps(session: Session = Depends(get_session),
                   _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Where the catalogue and the cloud disagree about what exists.

    Two directions, and the dangerous one is not the obvious one: a version the
    catalogue still sells but the cloud has retired is a promise the portal
    cannot keep, and a user discovers it by having an approved request fail at
    apply. That is REQ-2026-0148.
    """
    offered = _orchestrator_offered_versions()
    if offered is None:
        return {"reachable": False, "gaps": [],
                "note": "The execution layer could not be reached, so the cloud "
                        "could not be asked what it offers."}
    codes = [t.code for t in session.scalars(select(Technology)).all()]
    gaps = catalogue_sync.find_gaps(
        offered, codes, pinned_kubernetes=os.getenv("OCI_OKE_KUBERNETES_VERSION", ""))
    return {
        "reachable": True,
        "gaps": gaps,
        "retired": sum(1 for g in gaps if g["severity"] == "retired"),
        "behind": sum(1 for g in gaps if g["severity"] == "behind"),
        "families_asked": sorted(offered),
        "families_unreachable": sorted(k for k, v in offered.items() if v is None),
    }


def _orchestrator_boot_reports(reference: str, kinds: list[str]) -> dict | None:
    """The machines' own reports for one request, or None if unreachable.

    The orchestrator holds the credential that can read them; this asks it. The
    pre-authenticated URL never comes back here and never reaches a browser —
    only the text does.
    """
    payload = {"issued_at": datetime.now(timezone.utc).isoformat(),
               "operation": "boot-report", "reference": reference,
               "resource_kinds": kinds}
    raw = json.dumps(payload, sort_keys=True).encode()
    response, _err = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw),
                                           path="/boot-report")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json() or {}
    except ValueError:
        return None


@app.get("/api/requests/{reference}/boot-report")
def request_boot_report(reference: str, session: Session = Depends(get_session),
                        _auth: str = Depends(require_action("view_audit"))) -> dict:
    """What the machines of this request said about themselves, in their own words.

    The portal already reads these to decide whether a request provisioned, and
    then discards them. They answer "what did I actually get" better than any
    status can: the OS, the package versions, whether the service is running,
    whether the port answers, whether the firewall is open.

    A PROXY, NOT A LINK. The reports live behind a pre-authenticated OCI URL,
    which is a credential — publishing it would give anyone who ever saw it
    permanent, sign-in-free read access to every machine's report. The text is
    fetched server-side and returned; the URL stays in the orchestrator.

    Guarded by view_audit: the same people already trusted with this request's
    history. The report lists open ports and package versions of a live machine,
    which is useful to its owner and reconnaissance to anyone else.
    """
    req = _load_request(reference, session)
    kinds = _environment_resource_kinds(session, req)
    found = _orchestrator_boot_reports(reference, kinds)
    if found is None:
        # Distinguished from "no report yet": one is a portal problem the user
        # can do nothing about, the other is a machine that has not answered.
        return {"reference": reference, "reachable": False, "reports": [],
                "note": "The execution layer could not be reached, so the "
                        "machines could not be asked."}
    return {
        "reference": reference,
        "reachable": True,
        # A decommissioned environment often still has its report, and that is
        # usually exactly what someone wants to look back at. Shown, labelled as
        # historical, rather than hidden.
        "historical": req.status in ("decommissioned", "expired"),
        "reports": found.get("reports", []),
    }


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


class AiChatIn(BaseModel):
    message: str


@app.post("/api/ai/chat")
def ai_chat_endpoint(
    body: AiChatIn,
    session: Session = Depends(get_session),
    requester: str = Depends(_authed_requester),
) -> dict:
    """Natural-language front-end to the approvals bot (F-INT-08, AI Assistant).

    Claude (or the offline parser) maps a plain-English message to ONE safe
    command. It **interprets only** (ARCHITECTURE.md §7/P3): read-only intents
    (pending/status/help) run through the existing authority-preserving engine
    immediately; approve/reject are **never executed here** — they come back as a
    proposed command the caller must confirm, which then runs the normal
    `/api/chatops` path (Jira transition + RBAC + segregation of duties + audit).
    The interpretation itself is audited (`ai.chat`).
    """
    message = (body.message or "").strip()
    if len(message) < 2:
        raise HTTPException(
            status_code=422,
            detail="Type a message, e.g. \"what's pending?\" or \"approve REQ-2026-0001\".",
        )
    try:
        intent = ai_chat.interpret(message)
    except ai_chat.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    action = intent["action"]
    reference = intent["reference"]
    note = intent["note"]

    append_audit(
        session,
        "ai.chat",
        actor=requester,
        detail={
            "mode": intent["mode"],
            "action": action,
            "reference": reference,
            "message": message[:300],
        },
    )
    session.commit()

    base = {
        "mode": intent["mode"],
        "action": action,
        "reference": reference,
        "note": note,
        "intent": intent,
    }

    # Read-only intents: safe to run the existing engine now.
    if action == "help":
        extra = ("\n\nYou can also ask what any page or feature does — e.g. "
                 "\"what does the Reduce capacity request do?\" or \"what's the Admin page for?\".")
        return {**base, "needs_confirmation": False, "ok": True, "response": chatbot.HELP + extra}
    if action == "pending":
        result = chatbot.handle_command("pending", requester, session)
        return {**base, "needs_confirmation": False, "ok": result["ok"],
                "response": result["response"]}
    if action == "status":
        result = chatbot.handle_command(f"status {reference}", requester, session)
        return {**base, "needs_confirmation": False, "ok": result["ok"],
                "response": result["response"]}

    # Deciding intents: propose a command, never execute it here.
    if action in ("approve", "reject"):
        command = f"{action} {reference}" + (f" {note}" if note else "")
        lines = [f"Did you mean to {action} {reference}?"]
        if note:
            lines.append(f"Note: “{note}”")
        lines.append("Confirm to record your decision in Jira — I won't act until you do.")
        return {**base, "needs_confirmation": True, "ok": True,
                "command": command, "response": "\n".join(lines)}

    # A question about the portal ('explain'), or anything else we couldn't map to
    # a command ('unknown'): answer from the help knowledge base. Grounded — the
    # answer only ever comes from the curated KB, never invented.
    try:
        help_result = portal_help.answer(message)
    except portal_help.AiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {**base, "action": "explain", "needs_confirmation": False,
            "ok": bool(help_result["matched"]), "topic": help_result.get("title"),
            "response": help_result["response"]}


@app.get("/api/requests/{reference}/evidence.pdf")
def request_evidence(reference: str, session: Session = Depends(get_session),
                     _auth: str = Depends(require_action("view_audit"))) -> Response:
    """Download the governance evidence pack for a request (F-GOV-10): request +
    cost + approval + full audit trail + a re-verified tamper-evidence attestation."""
    req = _load_request(reference, session)
    components = [component_options.as_dict(c) for c in req.components]
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
    components = [component_options.as_dict(c) for c in req.components]
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


def _mark_component_lifecycle(session: Session, req: Request, out) -> None:
    """Flag each component alive or dead, from the resources actually on record.

    A component has no lifecycle of its own — the RESOURCE it maps to does — so
    this joins the two through the same blueprint mapping provisioning uses. With
    partial decommission the distinction became visible: a request can have one
    component torn down and another still running, and the form must not offer
    the dead one for decommission a second time.
    """
    rows = session.scalars(
        select(ProvisionedResource).where(ProvisionedResource.reference == req.reference)
    ).all()
    if not rows:
        return  # nothing provisioned yet — leave every flag null, meaning unknown
    live_kinds = {r.kind for r in rows if r.lifecycle_state == "active"}
    target = (req.deployment_target or "").strip().lower()
    for component in out.components:
        code = component.technology_code
        if not code:
            continue
        blueprint = session.scalar(
            select(Blueprint).where(
                Blueprint.technology_code == code,
                Blueprint.deployment_target == target,
                Blueprint.resource_kind != "",
            )
        )
        kind = blueprint.resource_kind if blueprint else None
        # No blueprint mapping (legacy single-resource request): the component is
        # alive exactly when anything of this request still is.
        component.active = (kind in live_kinds) if kind else bool(live_kinds)


def _golden_images_for(req: Request) -> dict[str, str]:
    """Proven images this request's machine may boot instead of installing (G2).

    FAIL-SOFT BY CONSTRUCTION. Every path out of here that is not a confident
    yes returns `{}`, and `{}` means the request provisions exactly as it did
    before golden images existed: install from repositories at first boot. No
    caller checks for an error, because there is no error to check for.

    That is not defensive coding for its own sake. A golden image is a speed-up
    on a path that already works, so the only way this feature can hurt is by
    being load-bearing — and the way to stop it being load-bearing is to make
    every failure indistinguishable from "no image today".
    """
    session = Session.object_session(req)
    if session is None:
        return {}
    try:
        components = [{"technology_code": c.technology_code}
                      for c in (req.components or [])]
        ready = golden.usable_for(
            session, [c["technology_code"] for c in components],
            req.deployment_target or "")
        return golden.one_image_per_machine(ready, components)
    except Exception:  # noqa: BLE001 - see the contract above
        return {}


def _handoff_payload(req: Request, *, ttl_expiry: str | None = None,
                     action: str | None = None,
                     resource_kinds: list[str] | None = None,
                     partial_destroy: bool = False) -> tuple[bytes, str]:
    """Build and sign the orchestrator handoff for a request.

    The full resource list is derived here rather than at each call site, so
    plan, apply, drift, state and destroy cannot disagree about what a request
    consists of — the kind of split that lost REQ-2026-0094's second resource.
    """
    if resource_kinds is None:
        from sqlalchemy.orm import object_session
        session = object_session(req)
        if session is not None:
            resource_kinds = _environment_resource_kinds(session, req)
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
        # EVERY resource this request builds. A stack is more than one thing, and
        # sending only the primary is how REQ-2026-0094 lost its VM. Older
        # orchestrators ignore this and still see resource_kind, so the contract
        # stays backward compatible.
        "resource_kinds": resource_kinds or [req.resource_kind or "oci-bucket"],
        # True when a destroy must remove ONLY the kinds above. The orchestrator
        # otherwise sweeps up every workspace on disk, which is right for
        # retiring a whole environment and catastrophic for removing one
        # component of a stack. Only the portal knows which this is.
        "partial_destroy": bool(partial_destroy),
    }
    # A PROVEN IMAGE, when this machine's technology has one. Omitted entirely
    # when it does not, so an orchestrator that has never heard of golden images
    # sees the payload it has always seen.
    proven = _golden_images_for(req)
    if proven:
        payload["golden_images"] = proven
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


def _result_resources(result: dict) -> list[dict]:
    """Every resource an orchestrator response reports. `resources` is the stack;
    `resource` is the single-resource form older responses use."""
    resources = result.get("resources")
    if isinstance(resources, list) and resources:
        return [r for r in resources if isinstance(r, dict)]
    one = result.get("resource")
    return [one] if isinstance(one, dict) and one else []


def _record_unautomated(session: Session, req: Request, jira_key: str | None) -> str | None:
    """Record, audit and put on the Jira ticket anything the portal did NOT build.

    Returns the request's status_detail: a plain sentence naming what is still
    outstanding, or None when the stack was fully provisioned. Reporting a
    partially delivered request as a clean success is how a requester ends up
    waiting for something nobody is going to build.
    """
    unmet = _unautomated_components(session, req)
    if not unmet:
        return None
    listed = ", ".join(unmet)
    append_audit(session, "fulfilment.partial", reference=req.reference, jira_key=jira_key,
                 detail={"not_automated": unmet})
    if jira_key:
        add_comment(jira_key, "⚠️ This request was provisioned, but the following "
                              f"component(s) were NOT built automatically and still need "
                              f"manual fulfilment: {listed}.")
    return (f"Provisioned, but {len(unmet)} component(s) were not built automatically "
            f"and still need manual fulfilment: {listed}.")


def _provision_in_background(reference: str, jira_key: str, body: bytes, signature: str,
                            ttl_expiry_iso: str) -> None:
    """Run the real apply, then set Jira Resolved + status provisioned (or failed)."""
    with SessionLocal() as session:
        req = session.scalar(select(Request).where(Request.reference == reference))
        if req is None:
            return
        response, error = _post_to_orchestrator(body, signature, path="/apply")

        # A TIMEOUT IS NOT A FAILURE. The orchestrator may still be building, and
        # concluding otherwise loses track of live infrastructure: REQ-2026-0149's
        # cluster and its two worker nodes were created, are billing, and the
        # portal recorded nothing because it believed the apply had failed.
        #
        # Reporting failure falsely is worse than reporting success falsely — a
        # false success is at least recorded and can be torn down. So the request
        # stays in flight and the verification path asks the CLOUD what exists,
        # which is the only source that actually knows.
        timed_out = response is None and "timed out" in (error or "").lower()
        if timed_out:
            append_audit(session, "apply.timeout", reference=reference,
                         jira_key=jira_key, detail={"error": error})
            add_comment(jira_key, "⏳ The portal stopped waiting for this apply, but "
                                  "it may still be running. Checking what actually "
                                  "exists in the cloud before deciding.")
            session.commit()
            result = {"resources": [], "summary": "apply timed out; reconciling"}
        elif response is None or response.status_code != 200:
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

        if not timed_out:
            result = response.json()
        ttl_dt = datetime.fromisoformat(ttl_expiry_iso) if ttl_expiry_iso else None
        # Recorded FIRST and unconditionally. These resources are real and are
        # billing from this moment; a verdict arriving later must never be the
        # reason nobody can find them to tear them down.
        for res in _result_resources(result):
            session.add(ProvisionedResource(
                reference=reference, kind=res.get("kind", "resource"), name=res.get("name", ""),
                region=res.get("region"), details=res.get("outputs", {}) or {},
                ttl_expiry=ttl_dt, lifecycle_state="active",
            ))
        append_audit(session, "apply.completed", reference=reference, jira_key=jira_key,
                     detail={"summary": result.get("summary")})
        session.commit()

    # Ask the machines themselves, with NO database session held: first boot takes
    # minutes, and a transaction left open that long blocks everything behind it.
    outcome = _await_boot_reports(reference, body, signature)

    with SessionLocal() as session:
        req = session.scalar(select(Request).where(Request.reference == reference))
        if req is None:
            return
        if outcome["ok"]:
            _transition_jira(session, req, resolved_status(), "jira.resolved")
            req.status = "provisioned"
            req.status_detail = _record_unautomated(session, req, jira_key)
            append_audit(session, "provisioned", reference=reference, jira_key=jira_key,
                         detail={**result, "verification": outcome["detail"]})
        else:
            # The apply SUCCEEDED and the resources exist — this is not
            # 'apply-failed', which tells an operator nothing was created and so
            # nothing needs cleaning up. What failed is the machine.
            #
            # 'verify-failed' and not 'verification-failed' because Request.status
            # is varchar(16) and there is no migration tooling in this project, so
            # a 19-character status is a runtime error on Postgres that SQLite
            # would let every test pass. 'decommission-failed' was exactly that,
            # sitting unexploded until the first decommission failed.
            req.status = "verify-failed"
            # status_detail is varchar(500) and a machine can list many problems.
            req.status_detail = outcome["summary"][:500]
            append_audit(session, outcome["event"], reference=reference, jira_key=jira_key,
                         detail=outcome["detail"])
            add_comment(jira_key, "⚠️ The infrastructure was created, but the machine "
                                  "did not confirm it is working, so this request is "
                                  f"NOT complete.\n\n{outcome['summary']}\n\nThe "
                                  "resources exist and are billing — they need fixing "
                                  "or decommissioning.")
        session.commit()


def _boot_verify_enforced() -> bool:
    """Whether a machine that says it is broken blocks the request (F-LCM).

    On by default. Off still COLLECTS and shows the report — turning it off means
    'do not let this stop a request', never 'stop asking'.
    """
    return settings.env("BOOT_VERIFY_ENFORCED", "true").strip().lower() in (
        "1", "true", "yes", "on")


def _boot_verify_int(key: str, fallback: int) -> int:
    try:
        return max(1, int(settings.env(key, str(fallback))))
    except (TypeError, ValueError):
        return fallback


def _await_boot_reports(reference: str, body: bytes, signature: str) -> dict:
    """Wait for every machine to say what it actually became, and judge it.

    WHY THE PORTAL WAITS. Terraform returns when an instance reaches RUNNING,
    which is BEFORE cloud-init has finished installing anything. Asking straight
    away would find no report and call a healthy machine silent, so the portal
    waits for the answer rather than assuming one.

    Three outcomes, deliberately distinct, because they need different actions:
      ok       — every machine reports the software installed and running
      broken   — a machine says what is wrong, in its own words
      silent   — nothing reported in time, so the portal does not KNOW. Absence of
                 evidence is not evidence of health; this is the exact inference
                 that reported four broken machines as provisioned.
    """
    if not _boot_verify_enforced():
        return {"ok": True, "event": "verification.skipped",
                "summary": "", "detail": {"enforced": False}}

    deadline_minutes = _boot_verify_int("BOOT_VERIFY_DEADLINE_MINUTES", 15)
    interval = _boot_verify_int("BOOT_VERIFY_POLL_SECONDS", 20)
    give_up_at = time.monotonic() + deadline_minutes * 60
    last: dict = {}

    while True:
        unreachable = None
        response, error = _post_to_orchestrator(body, signature, path="/verify")
        if response is not None and response.status_code == 200:
            last = response.json()
            if last.get("checked", 0) == 0:
                # Nothing here can report — a bucket, a managed database, or mock
                # mode. Silence is the correct and complete answer.
                return {"ok": True, "event": "verification.not_applicable",
                        "summary": "", "detail": last}
            if last.get("settled"):
                break
        else:
            unreachable = error or (response.text if response is not None else "no response")

        # Read the clock ONCE. Asking twice let the deadline fall between the two
        # readings, so "we could not ask the machines" was reported as "we asked
        # and they said nothing" — two different faults needing two different
        # fixes, decided by a microsecond.
        out_of_time = time.monotonic() >= give_up_at
        if out_of_time and unreachable:
            return {"ok": False, "event": "verification.unavailable",
                    "summary": ("Could not ask the machines whether they work: "
                                f"{unreachable}"),
                    "detail": {"error": unreachable}}
        if out_of_time:
            waiting = last.get("waiting") or ["the machines"]
            return {"ok": False, "event": "verification.silent",
                    "summary": (f"No report arrived from {', '.join(waiting)} within "
                                f"{deadline_minutes} minutes. The machine may still be "
                                f"installing, may have failed before it could report, or "
                                f"may not be able to reach Object Storage. It has NOT "
                                f"been confirmed working."),
                    "detail": last}
        time.sleep(interval)

    if last.get("all_ok"):
        return {"ok": True, "event": "verification.passed", "summary": "", "detail": last}

    problems: list[str] = []
    for resource in last.get("resources", []):
        problems += resource.get("problems") or []
        if resource.get("state") == "unreadable":
            problems.append(f"{resource['kind']}: {resource.get('note', 'unreadable')}")
    return {"ok": False, "event": "verification.failed",
            "summary": ("The machines were built but reported problems: "
                        + "; ".join(problems)),
            "detail": last}


@app.post("/api/requests/{reference}/retry")
def retry_request(reference: str, session: Session = Depends(get_session),
                  _auth: str = Depends(require_action("execute"))):
    """Resume a request the portal stopped retrying.

    Clears the failure counter so the next poll cycle picks it up again. Deciding
    that the cause is fixed is a human judgement, which is exactly why halting is
    not self-clearing: an automatic reset would just resume the same loop.
    """
    req = _load_request(reference, session)
    attempts = req.provision_attempts or 0
    if attempts == 0:
        return {"status": req.status, "message": f"{reference} is not being held."}
    req.provision_attempts = 0
    req.status_detail = None
    append_audit(session, "provision.retry", reference=reference,
                 jira_key=req.approval.jira_key if req.approval else None,
                 actor=_auth, detail={"cleared_attempts": attempts})
    session.commit()
    return {"status": req.status,
            "message": f"{reference} will be retried on the next cycle."}


# --- Cancelling a request ----------------------------------------------------

# Statuses a request can be cancelled FROM. Everything here has the same
# property: the portal is not part-way through building something.
#
# `provisioned` is deliberately absent — a running environment is torn down by
# DECOMMISSION, which destroys the infrastructure and closes the loop. Letting
# cancel close the record while the resources kept running is precisely how an
# orphan is made, and this portal already has a page for finding those.
CANCELLABLE = {"draft", "submitted", "planned", "in-progress",
               "apply-failed", "verify-failed", "manual-fulfil",
               # Waiting for somebody to agree a price it did not have when it
               # was approved. Nothing has been built, and a request whose only
               # exit is an approval that may never come is how REQ-2026-0176
               # ended up closed by editing the database.
               "awaiting-reapproval"}
CANCELLED = "cancelled"


class CancelIn(BaseModel):
    reason: str = ""


@app.post("/api/requests/{reference}/cancel", response_model=RequestOut)
def cancel_request(reference: str, body: CancelIn,
                   session: Session = Depends(get_session),
                   requester: str = Depends(require_action("create_request"))):
    """Stop a request that is not going to be fulfilled. Creates nothing, destroys nothing.

    REQ-2026-0176 is why this exists. It was quoted 0.00 AED for an unpriceable
    component, approved at that fiction, and refused at execution — leaving it
    sitting in `in-progress` for ever, looking active. There was no way to close
    it, so it was closed by hand in the database.

    THE RULE IS ABOUT RESOURCES, NOT STATUS. A request that owns anything real is
    refused and pointed at decommission, whatever state its record is in: closing
    the record while the infrastructure keeps running is how an orphan is made,
    and it would still be billing.
    """
    req = _load_request(reference, session)
    actor = requester

    if req.status == CANCELLED:
        return RequestOut.model_validate(req)

    # Yours, or you hold oversight. A requester cancelling their own request
    # needs no ceremony; cancelling somebody else's is an operational act.
    oversight = roles_mod.can(roles_mod.resolve_roles(actor), "view_overview")
    if (req.requester or "").lower() != (actor or "").lower() and not oversight:
        raise HTTPException(
            status_code=403,
            detail="You can cancel your own requests; cancelling someone else's "
                   "needs oversight.")

    built = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == reference)).all()
    if built:
        kinds = ", ".join(sorted({r.kind for r in built})) or "resources"
        raise HTTPException(
            status_code=409,
            detail=(f"{reference} has already built {kinds}, so it cannot be "
                    f"cancelled — cancelling would close the record and leave the "
                    f"infrastructure running and billing. Decommission it instead, "
                    f"which destroys what was built and then closes it."))

    if req.status not in CANCELLABLE:
        raise HTTPException(
            status_code=409,
            detail=(f"{reference} is {req.status} and cannot be cancelled. "
                    f"Cancellable: {', '.join(sorted(CANCELLABLE))}."))

    was = req.status
    reason = (body.reason or "").strip()
    before = (req.status_detail or "").strip()
    req.status = CANCELLED
    # KEEP WHY IT WAS IN TROUBLE. Overwriting status_detail discards the very
    # thing somebody reading this later wants: a request is usually cancelled
    # BECAUSE of what that field said, and "Cancelled by X" on its own turns a
    # diagnosis into a shrug. REQ-2026-0176 lost "quoted 0.00 AED for an
    # unpriceable component" that way.
    req.status_detail = " ".join(filter(None, [
        f"Cancelled by {actor}. Nothing was created.",
        f"Reason: {reason}" if reason else "",
        f"Before cancelling: {before}" if before else "",
    ]))[:2000]

    # The ticket is the system of record, so it must not be left open behind a
    # closed request. A comment always; the transition only if Jira offers one,
    # because a workflow without a cancelled state must not turn this into a 500.
    jira_key = req.approval.jira_key if req.approval else None
    if jira_key:
        try:
            add_comment(jira_key, f"Cancelled in the portal by {actor}. "
                                  f"Nothing was created."
                                  + (f" Reason: {reason}" if reason else ""))
        except Exception as exc:  # noqa: BLE001 — a comment must not block the close
            logger.warning("cancel: could not comment on %s: %s", jira_key, exc)
        # JIRA_REJECTED_STATUSES is a list because workflows differ; try each in
        # turn and stop at the first that this project actually offers.
        for target in [t.strip() for t in os.getenv(
                "JIRA_REJECTED_STATUSES", "Rejected,Cancelled").split(",") if t.strip()]:
            try:
                transition_issue(jira_key, target)
                break
            except Exception:  # noqa: BLE001 — try the next configured name
                continue

    append_audit(session, "request.cancelled", reference=reference,
                 jira_key=jira_key, actor=actor,
                 detail={"was": was, "reason": reason})
    session.commit()
    return RequestOut.model_validate(req)


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


def _db_system_ocid(session: Session, reference: str) -> str:
    """The managed-database OCID recorded for a provisioned environment, from the
    Terraform outputs. Empty for environments that aren't a managed database."""
    res = session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == reference,
            ProvisionedResource.kind == "oci-postgres",
            ProvisionedResource.lifecycle_state == "active",
        )
    ).first()
    return ((res.details or {}).get("postgres_ocid") or "") if res else ""


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
    mode = os.getenv("BACKUP_MODE", "mock").strip().lower()
    details: dict = {"mode": mode}

    # Live mode takes a REAL snapshot through the orchestrator (the layer that
    # holds cloud credentials) and records the cloud backup id — without it a
    # restore has nothing to restore from, so a failure here must not be recorded
    # as a successful backup.
    if mode == "live":
        payload = {
            "contract_version": CONTRACT_VERSION,
            "idempotency_key": f"{reference}:backup:{label}",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "reference": reference,
            "operation": "backup",
            "target": {"reference": reference,
                       "resource_id": _db_system_ocid(session, reference)},
            "backup": {"label": label},
        }
        raw = json.dumps(payload, sort_keys=True).encode()
        response, error = _post_to_orchestrator(raw, sign(WEBHOOK_SECRET, raw), path="/backup")
        if response is None or response.status_code != 200:
            reason = _short_reason(error or (response.text if response else ""))
            append_audit(session, "backup.failed", reference=reference, actor=actor,
                         detail={"label": label, "error": reason})
            session.commit()
            raise HTTPException(status_code=502, detail=f"The backup did not run: {reason}")
        result = response.json()
        details.update({"backup_id": result.get("backup_id", ""),
                        "summary": result.get("summary")})

    backup = Backup(reference=reference, label=label, created_by=actor, details=details)
    session.add(backup)
    append_audit(session, "backup.created", reference=reference, actor=actor,
                 detail={"label": label, "mode": mode,
                         "backup_id": details.get("backup_id", "")})
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
        "target": {"reference": target.reference, "policy_input": _policy_input(target),
                   # The cloud id of the system being restored (empty for non-DB envs).
                   "resource_id": (backup.details or {}).get("db_system_id", "")},
        # backup_id is the CLOUD backup (e.g. an OCI backup OCID); without it a live
        # restore has nothing to restore from and refuses rather than pretending.
        "backup": {"id": backup.id, "label": backup.label,
                   "backup_id": (backup.details or {}).get("backup_id", "")},
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
    # A live OCI restore creates a NEW database system — there is no in-place
    # rollback. Carry that fact through to the requester and the Jira ticket
    # instead of implying the original database was rewound.
    new_system = result.get("new_db_system_id") or ""
    if result.get("in_place") is False:
        req.status_detail = (
            "Restored into a NEW database system"
            + (f" ({new_system})" if new_system else "")
            + " — the original is unchanged. Repoint the application at the new endpoint."
        )
        add_comment(req.approval.jira_key,
                    "ℹ️ Restore complete, but NOT in place: OCI managed PostgreSQL "
                    "restores into a new database system"
                    + (f" ({new_system})" if new_system else "")
                    + ". The original database is untouched — repoint the application "
                      "at the new endpoint, then retire whichever system is no longer needed.")
    else:
        req.status_detail = None
    req.status = "restored"
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    append_audit(session, "restore.performed", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"target": target.reference, "backup": backup.label,
                         "verified": verified, "summary": summary,
                         "in_place": result.get("in_place", True),
                         "new_db_system_id": new_system})
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


# --- Report subscriptions (F-RPT-11) -----------------------------------------

_REPORT_CADENCES = {"daily": timedelta(days=1), "weekly": timedelta(days=7),
                    "monthly": timedelta(days=30)}


class ReportSubscriptionIn(BaseModel):
    report: str
    cadence: str = "weekly"
    target_url: str | None = None
    secret: str | None = None

    @field_validator("report")
    @classmethod
    def _valid_report(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in reports.REPORT_KINDS:
            raise ValueError(f"report must be one of: {', '.join(reports.REPORT_KINDS)}")
        return v

    @field_validator("cadence")
    @classmethod
    def _valid_cadence(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _REPORT_CADENCES:
            raise ValueError("cadence must be daily, weekly or monthly")
        return v

    @field_validator("target_url")
    @classmethod
    def _valid_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must start with http:// or https://")
        return v


def _report_sub_row(sub: ReportSubscription, session: Session) -> dict:
    """Subscription for the portal — the delivery secret is NEVER returned."""
    last = session.scalar(select(ReportRun).where(ReportRun.subscription_id == sub.id)
                          .order_by(ReportRun.id.desc()))
    return {"id": sub.id, "report": sub.report, "cadence": sub.cadence, "target_url": sub.target_url,
            "active": sub.active, "created_by": sub.created_by,
            "next_due": sub.next_due.isoformat() if sub.next_due else None,
            "last_sent_at": sub.last_sent_at.isoformat() if sub.last_sent_at else None,
            "last_run": ({"id": last.id, "delivered": last.delivered,
                          "generated_at": last.generated_at.isoformat() if last.generated_at else None}
                         if last else None)}


def _run_report(session: Session, sub: ReportSubscription, actor: str = "scheduler") -> ReportRun:
    """Generate a subscription's report, store the run (always viewable), and
    optionally POST it signed to its target URL. Caller commits."""
    data = reports.generate_report(session, sub.report)
    run = ReportRun(subscription_id=sub.id, report=sub.report, summary=data)
    session.add(run)
    delivered = None
    if sub.target_url:
        body = json.dumps({"report": sub.report,
                           "generated_at": datetime.now(timezone.utc).isoformat(), "data": data},
                          sort_keys=True, default=str).encode()
        headers = {"Content-Type": "application/json", "X-Report": sub.report}
        if sub.secret:
            headers["X-Signature"] = sign(sub.secret, body)
        try:
            resp = httpx.post(sub.target_url, content=body, headers=headers, timeout=5.0)
            delivered = "delivered" if 200 <= resp.status_code < 300 else "failed"
        except Exception:  # noqa: BLE001
            delivered = "failed"
    run.delivered = delivered
    append_audit(session, "report.sent", actor=actor,
                 detail={"subscription": sub.id, "report": sub.report, "delivered": delivered})
    return run


@app.post("/api/report-subscriptions")
def create_report_subscription(body: ReportSubscriptionIn, session: Session = Depends(get_session),
                               actor: str = Depends(require_action("view_overview"))) -> dict:
    """Subscribe to a scheduled report (F-RPT-11) — oversight (finance/security/
    admin). Generated on its cadence + stored; optionally POSTed signed to a URL."""
    sub = ReportSubscription(report=body.report, cadence=body.cadence, target_url=body.target_url,
                             secret=body.secret, created_by=actor,
                             next_due=datetime.now(timezone.utc))  # first run on the next sweep
    session.add(sub)
    append_audit(session, "report.subscribed", actor=actor,
                 detail={"report": body.report, "cadence": body.cadence})
    session.commit()
    return _report_sub_row(sub, session)


@app.get("/api/report-subscriptions")
def list_report_subscriptions(session: Session = Depends(get_session),
                              _auth: str = Depends(require_action("view_overview"))) -> dict:
    subs = session.scalars(select(ReportSubscription).order_by(ReportSubscription.id.desc())).all()
    return {"subscriptions": [_report_sub_row(s, session) for s in subs]}


@app.delete("/api/report-subscriptions/{sub_id}")
def delete_report_subscription(sub_id: int, session: Session = Depends(get_session),
                               actor: str = Depends(require_action("view_overview"))) -> dict:
    sub = session.get(ReportSubscription, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Report subscription not found.")
    session.delete(sub)
    append_audit(session, "report.unsubscribed", actor=actor, detail={"report": sub.report})
    session.commit()
    return {"deleted": sub_id}


@app.post("/api/report-subscriptions/{sub_id}/run")
def run_report_now(sub_id: int, session: Session = Depends(get_session),
                   actor: str = Depends(require_action("view_overview"))) -> dict:
    """Generate a subscription's report now (an explicit test) — doesn't change the
    schedule."""
    sub = session.get(ReportSubscription, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Report subscription not found.")
    run = _run_report(session, sub, actor=actor)
    session.commit()
    return {"id": run.id, "report": run.report, "delivered": run.delivered, "summary": run.summary}


@app.get("/api/report-subscriptions/{sub_id}/runs")
def list_report_runs(sub_id: int, session: Session = Depends(get_session),
                     _auth: str = Depends(require_action("view_overview"))) -> dict:
    runs = session.scalars(select(ReportRun).where(ReportRun.subscription_id == sub_id)
                           .order_by(ReportRun.id.desc()).limit(20)).all()
    return {"subscription": sub_id,
            "runs": [{"id": r.id, "report": r.report, "delivered": r.delivered,
                      "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                      "summary": r.summary} for r in runs]}


@app.get("/api/reports/{kind}")
def get_report(kind: str, session: Session = Depends(get_session),
               _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Generate a report on demand (F-RPT-11) — oversight. Read-only: nothing is
    stored and nothing is provisioned."""
    if kind not in reports.REPORT_KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown report: {kind}")
    return {"report": kind, "generated_at": datetime.now(timezone.utc).isoformat(),
            "data": reports.generate_report(session, kind)}


def _sweep_reports(session: Session) -> None:
    """Generate + deliver due report subscriptions (F-RPT-11), advancing each by
    its cadence. Runs each poll cycle; cheap when nothing is due."""
    now = datetime.now(timezone.utc)
    due = session.scalars(select(ReportSubscription).where(
        ReportSubscription.active.is_(True), ReportSubscription.next_due <= now)).all()
    for sub in due:
        _run_report(session, sub, actor="scheduler")
        sub.last_sent_at = now
        sub.next_due = now + _REPORT_CADENCES.get(sub.cadence, timedelta(days=7))
    if due:
        session.commit()


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
        req.status = "teardown-failed"
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

    # WHICH resources this decommission removes — from the components the
    # requester SELECTED, not from everything the source built.
    #
    # This used to hand over the source request untouched, so the orchestrator
    # tore down every workspace it could find. A request naming one component of
    # a two-component stack destroyed both machines: the selection was collected,
    # validated against the source's stack, priced and written on the Jira ticket,
    # and then discarded at the last step.
    #
    # The payload still carries the SOURCE's reference and policy input, because
    # the workspaces and names belong to the source; only the kind list narrows.
    selected_kinds = _environment_resource_kinds(session, req)
    remaining = [k for k in _environment_resource_kinds(session, source)
                 if k not in selected_kinds]
    body, signature = _handoff_payload(source, resource_kinds=selected_kinds,
                                       partial_destroy=bool(remaining))
    # CLAIM THE REQUEST BEFORE THE LONG CALL BELOW.
    #
    # _post_to_orchestrator blocks for up to ORCHESTRATOR_TIMEOUT_SECONDS (3000)
    # while Terraform destroys real infrastructure. This used to leave the
    # request at 'submitted' for that whole window, and the poller sweep collects
    # 'submitted' — so every 30 seconds it started ANOTHER destroy of the same
    # thing. REQ-2026-0161 handed off at 20:40:33 and again at 20:48:20, and the
    # second run collided with the first on the Terraform state lock.
    #
    # The provisioning path already does exactly this: it sets 'in-progress'
    # before its blocking call and notes that the sweep only ever picks up
    # 'submitted' and 'planned'. Decommission simply never got the same
    # treatment. This is that asymmetry closed, not a new mechanism.
    req.status = "decommissioning"
    append_audit(session, "destroy.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"source": source.reference, "kinds": selected_kinds,
                         "remaining": remaining, "partial": bool(remaining)})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/destroy")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "teardown-failed"
        req.status_detail = reason
        append_audit(session, "destroy.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"source": source.reference, "error": raw})
        add_comment(req.approval.jira_key, "⚠️ Automated decommission failed; resources "
                                           f"were not removed.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "decommissioned": False, "error": reason}

    # Mark ONLY the resources this request actually removed. Marking every active
    # row — which is what happened before — told the portal a machine was gone
    # while it was still running and still billing, and there is no worse kind of
    # registry error than one that hides live infrastructure.
    #
    # FROM WHAT THE ORCHESTRATOR DESTROYED, NOT FROM WHAT WE ASKED IT TO.
    #
    # These were two independent derivations of the same fact, and on REQ-2026-0226
    # they disagreed. The portal asked for `oci-bucket` — the catalogue default
    # that 41 of 46 technologies still carry. The workspace on disk was
    # `oci-service-vm`. A FULL teardown sweeps up workspaces it was not told
    # about, on purpose, so the orchestrator destroyed the right machine and said
    # so: "oci-service-vm: Destroy complete! Resources: 3 destroyed."
    #
    # Then this loop matched `oci-service-vm` against `['oci-bucket']`, marked
    # nothing, found one row still active, and left the source PROVISIONED — a
    # destroyed machine shown as running, in My Environments and in the cost
    # report, at 790.59 a month.
    #
    # The executing layer is the authority on what it executed. Falling back to
    # `selected_kinds` keeps an older orchestrator working exactly as before.
    body_out = response.json() if response is not None else {}
    destroyed_kinds = [k for k in (body_out.get("kinds") or []) if isinstance(k, str)]
    if not destroyed_kinds:
        destroyed_kinds = selected_kinds

    for res in session.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == source.reference,
            ProvisionedResource.lifecycle_state == "active",
            ProvisionedResource.kind.in_(destroyed_kinds),
        )
    ):
        res.lifecycle_state = "decommissioned"
    session.flush()

    # The source is only finished when nothing of it is left. A partial teardown
    # leaves it PROVISIONED, so it still appears in My Environments, can still be
    # decommissioned again for what remains, and still shows its real cost.
    still_active = session.scalar(
        select(func.count()).select_from(ProvisionedResource).where(
            ProvisionedResource.reference == source.reference,
            ProvisionedResource.lifecycle_state == "active",
        )
    ) or 0
    source.status = "provisioned" if still_active else "decommissioned"
    req.status = "decommissioned"
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    summary = body_out.get("summary")
    append_audit(session, "decommissioned", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"source": source.reference, "summary": summary,
                         "asked": selected_kinds, "destroyed": destroyed_kinds})
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


def _reduce_handoff(req: Request, target: Request, reductions: list[dict],
                    resource_kind: str = "oci-bucket") -> tuple[bytes, str]:
    """Build + sign the reduce-capacity handoff — the target and the per-component
    size reductions (technology, from, to). `resource_kind` tells the orchestrator
    which Terraform module owns the target, so a live resize can re-plan it."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": req.approval.jira_key,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "operation": "reduce",
        "target": {"reference": target.reference, "policy_input": _policy_input(target),
                   "resource_kind": resource_kind},
        "reductions": reductions,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    return body, sign(WEBHOOK_SECRET, body)


def _dns_handoff(req: Request, target: Request, resource_kind: str) -> tuple[bytes, str]:
    """Build + sign the DNS handoff — the target environment and the record."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": f"{req.approval.jira_key}:dns:{req.dns_name}",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "operation": "dns",
        "target": {"reference": target.reference, "policy_input": _policy_input(target),
                   "resource_kind": resource_kind},
        "record": {"name": req.dns_name, "type": (req.dns_type or "A"),
                   "value": req.dns_value or ""},
    }
    body = json.dumps(payload, sort_keys=True).encode()
    return body, sign(WEBHOOK_SECRET, body)


def _dns(session: Session, req: Request, actor: str) -> dict:
    """Create a DNS name for the target environment (source_reference).

    Governed + orchestrator-executed, mirroring reduce. The record is planned into
    the target's own Terraform workspace so it shares that environment's
    lifecycle. The target stays provisioned.
    """
    target = session.scalar(select(Request).where(Request.reference == req.source_reference))
    if target is None:
        req.status = "dns-failed"
        req.status_detail = f"DNS target {req.source_reference} not found."
        append_audit(session, "dns.target_missing", reference=req.reference,
                     jira_key=req.approval.jira_key, detail={"target": req.source_reference})
        session.commit()
        return {"approval": "approved", "created": False, "error": req.status_detail}

    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    session.commit()

    body, signature = _dns_handoff(req, target, _environment_resource_kind(session, target))
    append_audit(session, "dns.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"target": target.reference, "name": req.dns_name,
                         "type": req.dns_type})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/dns")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "dns-failed"
        req.status_detail = reason
        append_audit(session, "dns.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"target": target.reference, "name": req.dns_name, "error": raw})
        add_comment(req.approval.jira_key,
                    f"⚠️ Automated DNS record creation failed; no record was created.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "created": False, "error": reason}

    result = response.json()
    domain = result.get("domain") or req.dns_name
    req.status = "dns-created"
    req.status_detail = (f"{domain} → {result['points_to']}"
                         if result.get("points_to") else None)
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    append_audit(session, "dns.created", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"target": target.reference, "domain": domain,
                         "points_to": result.get("points_to", ""),
                         "summary": result.get("summary")})
    session.commit()
    return {"approval": "approved", "created": True, "target": target.reference,
            "domain": domain, "message": result.get("summary")}


def _reduce(session: Session, req: Request, actor: str) -> dict:
    """Scale chosen components of the target (source_reference) DOWN (F-CAT).
    Governed + orchestrator-executed, mirroring refresh. Mock records it; live is a
    real resize extension point. The target stays provisioned."""
    target = session.scalar(select(Request).where(Request.reference == req.source_reference))
    if target is None:
        req.status = "reduce-failed"
        req.status_detail = f"Reduce target {req.source_reference} not found."
        append_audit(session, "reduce.target_missing", reference=req.reference,
                     jira_key=req.approval.jira_key, detail={"target": req.source_reference})
        session.commit()
        return {"approval": "approved", "reduced": False, "error": req.status_detail}

    current = {c.technology_code: (c.size or "").strip().lower()
               for c in target.components if c.technology_code}
    reductions = [{"technology": c.technology_code, "from": current.get(c.technology_code),
                   "to": (c.size or "").strip().lower()}
                  for c in req.components if c.technology_code]

    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    session.commit()

    body, signature = _reduce_handoff(req, target, reductions,
                                      _environment_resource_kind(session, target))
    append_audit(session, "reduce.handoff", reference=req.reference,
                 jira_key=req.approval.jira_key, actor=actor,
                 detail={"target": target.reference, "reductions": reductions})
    session.commit()

    response, error = _post_to_orchestrator(body, signature, path="/reduce")
    if response is None or response.status_code != 200:
        raw = error or (response.text if response else "")
        reason = _short_reason(raw)
        req.status = "reduce-failed"
        req.status_detail = reason
        append_audit(session, "reduce.failed", reference=req.reference,
                     jira_key=req.approval.jira_key, detail={"target": target.reference, "error": raw})
        add_comment(req.approval.jira_key, f"⚠️ Automated capacity reduction failed.\n\n{reason}")
        session.commit()
        return {"approval": "approved", "reduced": False, "error": reason}

    result = response.json()
    summary = result.get("summary")
    req.status = "reduced"
    # A real resize restarts the resource (a flex-shape change reboots a VM; a
    # database shape change restarts the service). Say so rather than letting
    # "reduced" imply it happened invisibly.
    disruptive = bool(result.get("disruptive"))
    req.status_detail = ("The resource restarted as part of this resize."
                         if disruptive else None)
    if disruptive:
        add_comment(req.approval.jira_key,
                    "ℹ️ Capacity reduced. Note this was a live resize: the resource "
                    "restarted as part of the change, so there was a brief interruption.")
    _transition_jira(session, req, resolved_status(), "jira.resolved")
    append_audit(session, "capacity.reduced", reference=req.reference,
                 jira_key=req.approval.jira_key,
                 detail={"target": target.reference, "reductions": reductions,
                         "summary": summary, "disruptive": disruptive})
    session.commit()
    return {"approval": "approved", "reduced": True, "target": target.reference,
            "reductions": reductions, "message": f"Reduced {target.reference}: {summary}"}


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


# --- a price that became knowable after approval ------------------------------

REAPPROVAL_NEEDED = "awaiting-reapproval"


def _needs_reapproval(raw: str) -> bool:
    """Whether the orchestrator refused because the cost changed since approval.

    Matched on the orchestrator's own refusal text rather than a status code,
    because 409 also carries the proof cost cap and an IaC scan block, and those
    are different problems needing different actions.
    """
    return "Cost re-validation failed" in (raw or "")


def _current_monthly_for(session: Session, req) -> float | None:
    """What this request costs NOW, with whatever is certified today."""
    try:
        components = [component_options.as_dict(c) for c in req.components]
        return float(estimate_cost(components, req.deployment_target, session,
                                   req.advanced_options)["totals"]["monthly"])
    except Exception:  # noqa: BLE001 — a missing figure must not block the ask
        return None


def _ask_for_reapproval(session: Session, req, jira_key: str | None,
                        monthly: float | None) -> None:
    """Put the request back in front of its approver, at the price it now has.

    NOT a retry and not a failure. The work succeeded — a component the portal
    could not price has been certified, and the request now has a real cost that
    nobody has agreed to. Jira holds the approval (ARCHITECTURE.md §4), so Jira
    is where it goes back to; the portal only asks.
    """
    from api.pricing import CURRENCY

    figure = f"{monthly:.2f} {CURRENCY}" if monthly is not None else "a real figure"
    req.status = REAPPROVAL_NEEDED
    req.status_detail = (
        f"Approved before this could be priced, and it now costs {figure} a month. "
        f"Nothing has been built. The component was certified automatically while "
        f"this request was in flight, so the price is real for the first time — and "
        f"a cost nobody has agreed to is not one the portal will build. Approve it "
        f"again at this figure, or cancel the request.")
    append_audit(session, "cost.reapproval_requested", reference=req.reference,
                 jira_key=jira_key, actor="poller",
                 detail={"monthly": monthly, "was_approved_at": "unpriced"})
    if jira_key:
        try:
            add_comment(jira_key,
                        f"This request was approved before its cost could be "
                        f"calculated. The component has since been certified "
                        f"automatically, so it now has a real price: {figure} a "
                        f"month. Nothing has been built. Please approve again at "
                        f"this figure, or cancel the request.")
        except Exception as exc:  # noqa: BLE001 — a comment must not block the state
            logger.warning("re-approval: could not comment on %s: %s", jira_key, exc)


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
        # Reduce scales chosen components of the target down (F-CAT).
        if req.request_type == "reduce":
            _reduce(session, req, actor="poller")
            return req.status
        # DNS gives the target environment a name (GAP-ANALYSIS step 5).
        if req.request_type == "dns":
            _dns(session, req, actor="poller")
            return req.status
        # NOTHING CERTIFIED MEANS NOTHING TO BUILD.
        #
        # Without this the request goes to the orchestrator anyway, and
        # _environment_resource_kinds falls back to a legacy derivation that ends
        # at "oci-bucket". A requester who asked for a Kubernetes cluster received
        # an empty object storage bucket and the request was marked provisioned —
        # REQ-2026-0144 did exactly that with python312, and OKE, Kafka, MongoDB
        # and every other uncertified technology behave the same way today.
        #
        # Manual fulfilment is a real feature and is NOT what is being refused:
        # the portal still validates, prices, approves and audits the request, and
        # the infrastructure team still builds it. What stops is inventing a cloud
        # resource nobody asked for and calling the request finished.
        unmet = _unautomated_components(session, req)

        # THE AGENT MAKES IT BUILDABLE RATHER THAN HANDING IT OVER (2026-08-21).
        #
        # The reviewer's mandatory requirement: if a selected component has no
        # certified blueprint, the agent proves the recipe — writing one first if
        # none exists — certifies it on that evidence, and the request proceeds.
        # Manual fulfilment is now the fallback for when that FAILS, not the
        # first answer.
        #
        # The request is CLAIMED before the attempt, because it involves real
        # builds taking minutes. Leaving it collectable would have every sweep
        # start another one — the REQ-2026-0161 defect, thirty seconds apart.
        if unmet and autobuild.enabled():
            req.status = "auto-building"
            append_audit(session, "autobuild.started", reference=req.reference,
                         jira_key=jira_key, actor="agent",
                         detail={"components": unmet})
            session.commit()
            for code in unmet:
                try:
                    outcome = _autobuild_component(session, code, req.deployment_target)
                except Exception as exc:  # noqa: BLE001 — one failure must not
                    outcome = {"status": "failed", "detail": str(exc)}  # strand the request
                append_audit(session, "autobuild.finished", reference=req.reference,
                             jira_key=jira_key, actor="agent",
                             detail={"component": code, **outcome})
            session.commit()
            # Re-ask rather than assume: the only thing that counts is whether a
            # certified blueprint exists NOW.
            unmet = _unautomated_components(session, req)
            req.status = "in-progress" if not unmet else req.status

        requested = [c.technology_code for c in req.components if c.technology_code]
        if requested and len(unmet) == len(set(requested)):
            listed = ", ".join(unmet)
            req.status = "manual-fulfil"
            req.status_detail = (
                f"No cloud resource was created. Nothing in this request has a "
                f"certified blueprint on {req.deployment_target}: {listed}. The "
                f"infrastructure team fulfils it.")[:500]
            append_audit(session, "fulfilment.manual", reference=req.reference,
                         jira_key=jira_key, actor="poller",
                         detail={"not_automated": unmet})
            add_comment(jira_key,
                        "ℹ️ Approved, and NOT built automatically — no cloud "
                        f"resource was created. {listed} "
                        f"{'have' if len(unmet) > 1 else 'has'} no certified "
                        "blueprint, so this needs the infrastructure team.")
            session.commit()
            return req.status

        # A permanent failure must not be retried forever (see provision_attempts).
        max_attempts = _max_provision_attempts()
        if (req.provision_attempts or 0) >= max_attempts:
            return req.status  # held; a manual retry clears the counter
        body, signature = _handoff_payload(req)
        append_audit(session, "orchestrator.handoff", reference=req.reference,
                     jira_key=jira_key, detail={"contract": CONTRACT_VERSION})
        session.commit()
        response, error = _post_to_orchestrator(body, signature)
        if response is None or response.status_code != 200:
            raw = error or (response.text if response else "")
            reason = _short_reason(raw)
            req.provision_attempts = (req.provision_attempts or 0) + 1
            append_audit(session, "plan.failed", reference=req.reference, jira_key=jira_key,
                         detail={"error": raw, "attempt": req.provision_attempts})
            # THE PRICE BECAME REAL AFTER SOMEBODY APPROVED ITS ABSENCE.
            #
            # REQ-2026-0176 and REQ-2026-0178 both hit this: an uncertified
            # component was approved with no price, the agent then certified it
            # mid-flight, and execution correctly refused to build a 90.59/month
            # resource against a 0.00 approval. The guard is right and must not
            # be weakened — the whole separation of authority rests on the
            # executing layer re-checking what was actually approved.
            #
            # What was wrong is that the request then sat in-progress for ever,
            # looking active, with the work done and nothing to do about it. A
            # cost that has become knowable is not a failure to retry; it is a
            # different request from the one that was approved, and the person
            # who approved the first one is the only one who can approve this.
            if _needs_reapproval(raw):
                _ask_for_reapproval(session, req, jira_key, monthly=_current_monthly_for(session, req))
                session.commit()
                return req.status

            if req.provision_attempts >= max_attempts:
                req.status_detail = (
                    f"Held after {req.provision_attempts} failed attempts — the portal has "
                    f"stopped retrying. Fix the cause and use Retry to resume. {reason}")
                append_audit(session, "provision.halted", reference=req.reference,
                             jira_key=jira_key,
                             detail={"attempts": req.provision_attempts, "reason": reason})
                if jira_key:
                    add_comment(jira_key, "⚠️ Automated provisioning has been halted after "
                                          f"{req.provision_attempts} failed attempts. Nothing "
                                          f"was created.\n\n{reason}")
            else:
                req.status_detail = _short_reason(raw)  # e.g. an IaC scan block
            session.commit()
            return req.status  # still 'submitted' — retried next cycle unless held
        req.provision_attempts = 0  # a good handoff clears the history
        result = response.json()
        _record_scan(session, req, jira_key, result.get("scan"))  # IaC findings (F-SEC-03/04)
        if result.get("provisioned"):  # mock mode fully provisions on the handoff
            ttl_dt = _ttl_for(req)
            for res in _result_resources(result):
                session.add(ProvisionedResource(
                    reference=req.reference, kind=res.get("kind", "resource"),
                    name=res.get("name", ""), region=res.get("region"),
                    details=res.get("outputs", {}) or {}, ttl_expiry=ttl_dt,
                    lifecycle_state="active",
                ))
            req.status_detail = _record_unautomated(session, req, jira_key)
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
        # Picks up 'provisioned' / 'apply-failed' / 'verify-failed'. This
        # blocks the sweep while the machines finish booting and report, which is
        # the same bargain the sweep already makes with a long Terraform apply.
        # Safe to sit in 'in-progress' meanwhile: the sweep only ever picks up
        # 'submitted' and 'planned', so nothing re-provisions it underneath us.
        session.refresh(req)
    return req.status


# --- Change windows (F-GOV-05) -----------------------------------------------

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _change_window_enabled() -> bool:
    return settings.env("CHANGE_WINDOW_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


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
    tz_name = settings.env("CHANGE_WINDOW_TZ", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz, tz_name = timezone.utc, "UTC"
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    days = _parse_days(settings.env("CHANGE_WINDOW_DAYS", "mon-fri"))
    start = _parse_hm(settings.env("CHANGE_WINDOW_START", "08:00"), dtime(8, 0))
    end = _parse_hm(settings.env("CHANGE_WINDOW_END", "18:00"), dtime(18, 0))
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


def _sweep_project_expiry(session: Session) -> None:
    """Announce a project crossing into 'expiring' or 'expired', once per stage.

    Records it and nothing more. A project reaching its date must never touch
    what it already owns — that would be a database row deciding to tear down a
    production environment. The point is that somebody finds out before the date
    passes, not that anything happens automatically.
    """
    for p in session.scalars(select(Project).where(Project.expires_at.is_not(None))):
        status = (_project_expiry_status(session, p.code) or {}).get("status")
        if status not in ("expiring", "expired"):
            # Date pushed out, or removed: allow it to warn again next time.
            if p.expiry_notified is not None:
                p.expiry_notified = None
            continue
        if p.expiry_notified == status:
            continue  # already said, at this stage
        p.expiry_notified = status
        owned = session.scalar(
            select(func.count()).select_from(Request)
            .where(Request.project_code == p.code, Request.status == "provisioned")) or 0
        append_audit(session, f"project.{status}", actor="poller",
                     detail={"project": p.code, "owner": p.owner_email or "",
                             "expires_at": p.expires_at.isoformat(),
                             "provisioned_environments": owned})


@app.get("/api/projects/expiring")
def expiring_projects(session: Session = Depends(get_session),
                      _auth: str = Depends(require_action("view_overview"))) -> dict:
    """Projects at or near their expiry date, with what each still owns.

    The count of live environments is the point. 'PAY expires in nine days' is a
    diary note; 'PAY expires in nine days and owns four provisioned environments'
    is a decision someone has to make.
    """
    out = []
    for p in session.scalars(select(Project).where(Project.expires_at.is_not(None))
                             .order_by(Project.expires_at)):
        st = _project_expiry_status(session, p.code)
        if st is None or st["status"] == "ok":
            continue
        envs = session.scalars(
            select(Request).where(Request.project_code == p.code,
                                  Request.status == "provisioned")).all()
        out.append({
            "code": p.code, "name": p.name, "owner_email": p.owner_email,
            "expires_at": p.expires_at.isoformat(), "days_left": st["days_left"],
            "status": st["status"], "active": bool(p.active),
            "environments": [{"reference": r.reference,
                              "environment_name": r.environment_name,
                              "tier": r.environment_tier} for r in envs],
        })
    return {"projects": out, "warn_days": _project_expiry_warn_days()}


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

    # Certification review (C1): withdraw certification from any blueprint whose
    # recent record is nothing but failures. Isolated like the sweeps around it —
    # it must never be the reason requests stop being advanced.
    try:
        with SessionLocal() as session:
            for gone in certification.review(session):
                append_audit(session, "blueprint.suspended", actor="certification",
                             detail=gone)
            for aged in certification.expire(session):
                append_audit(session, "blueprint.expired", actor="certification",
                             detail=aged)
            # Restoration runs LAST, so within a single sweep any evidence
            # against a blueprint is applied before evidence for it.
            # WHAT THE ORCHESTRATOR ACTUALLY BUILDS, so a certification is
            # never restored for a recipe that no longer exists.
            buildable = frozenset(
                str(b) for m in (_orchestrator_blueprints() or [])
                for b in (m.get("builds") or []))
            for back in certification.restore(session, builds=buildable):
                append_audit(session, "blueprint.recertified", actor="certification",
                             detail=back)
            session.commit()
            fulfilment.invalidate_cache()
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "certification.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # WHAT THE REPOSITORIES OFFER (C10), read here so a request never waits for
    # it. A cold read of OL9's AppStream and BaseOS is about two minutes; an
    # hour later it is instant. Its own try, and never load-bearing: a warm that
    # fails leaves the pre-machine check exactly as slow as it would have been.
    try:
        repo_facts.warm((os.getenv("CONFIG_OS_FAMILY") or "rhel").strip().lower())
    except Exception:  # noqa: BLE001
        pass

    # GOLDEN IMAGES BECOME USABLE (G2). A capture is recorded the moment OCI
    # accepts it, which is ten to twenty minutes before the image can boot
    # anything — and only the cloud knows when that changes. Swept here rather
    # than on the request path, so a provisioning never waits on it.
    #
    # ITS OWN try, AFTER certification has committed. This was first written
    # INSIDE the certification sweep, and an unreachable orchestrator then took
    # the whole review down with it: blueprints that should have been suspended
    # stayed certified, silently. A speed-up that can break the thing it sits
    # beside is not a speed-up — the same rule the capture itself follows.
    try:
        with SessionLocal() as session:
            post = proof_wiring.make_post(_post_to_orchestrator, sign,
                                          WEBHOOK_SECRET)
            # ORDER MATTERS, and it is the same shape as the certification sweep
            # above: everything that STOPS an image being offered runs before
            # anything that deletes one, so a single cycle never deletes an
            # image it has not first taken out of service.
            for moved in golden.promote(
                    session, proof_wiring.make_image_states(post)):
                append_audit(session, f"golden.image.{moved['state']}",
                             actor="certification", detail=moved)
            for old_one in golden.supersede(session):
                append_audit(session, "golden.image.superseded",
                             actor="certification", detail=old_one)
            for aged in golden.expire(session):
                append_audit(session, "golden.image.expired",
                             actor="certification", detail=aged)
            # LAST, and only for images retired long enough ago that no request
            # can still be mid-apply holding the OCID.
            for gone in golden.reap(
                    session, proof_wiring.make_image_delete(post)):
                append_audit(session, "golden.image.deleted",
                             actor="certification", detail=gone)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "golden.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Catalogue sync (C3): where the catalogue and the cloud disagree about what
    # exists. Isolated like the sweeps around it.
    try:
        with SessionLocal() as session:
            _sweep_catalogue_gaps(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "catalogue.sync.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Project expiry: announce a project crossing into expiring/expired, once.
    # Isolated like the others so it can never abort the sweeps around it.
    try:
        with SessionLocal() as session:
            _sweep_project_expiry(session)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "project.sweep.error", detail={"error": str(exc)})
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

    # Scheduled report subscriptions (F-RPT-11): generate + deliver due reports,
    # advancing each by its cadence. Cheap when nothing is due.
    try:
        with SessionLocal() as session:
            _sweep_reports(session)
    except Exception as exc:  # noqa: BLE001
        try:
            with SessionLocal() as session:
                append_audit(session, "report.sweep.error", detail={"error": str(exc)})
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    # Shared rate-limit housekeeping (F-OPS-01): prune expired counter windows.
    # No-op unless the shared backend is on; leader-only (runs in _poll_once).
    if ratelimit.shared_enabled():
        try:
            with SessionLocal() as session:
                ratelimit.prune(session)
        except Exception:  # noqa: BLE001 — housekeeping must never crash the loop
            pass


def _audit_leader(event: str) -> None:
    """Record a leadership transition; never let auditing crash the loop."""
    try:
        with SessionLocal() as session:
            append_audit(session, event, actor=leader.INSTANCE_ID, detail={"instance": leader.INSTANCE_ID})
            session.commit()
    except Exception:  # noqa: BLE001
        pass


def sweep_watchdog_seconds() -> float:
    """How long ONE sweep may run while still holding the leader lease.

    Past this the heartbeat stops, the lease lapses, and a standby may take over
    — the escape hatch for a genuinely wedged sweep. It must sit comfortably
    above the longest legitimate sweep (a Terraform apply plus boot reports),
    or a slow-but-healthy build would be treated as a hang.
    """
    default = max(600.0, ORCH_TIMEOUT * 2)
    try:
        return max(60.0, float(os.getenv("POLLER_SWEEP_WATCHDOG_SECONDS", str(default))))
    except ValueError:
        return default


def _renew_lease_during_sweep(stop: threading.Event, every: float,
                              deadline: float) -> None:
    """Hold the leader lease while a sweep is working, but not forever.

    WHY THIS EXISTS. _poll_once runs inline and can block for
    ORCHESTRATOR_TIMEOUT_SECONDS (3000) inside a single handoff, while the lease
    TTL is 60 seconds. The loop could not reach its own renewal, so the lease
    lapsed *while the poller was working perfectly well* — on 2026-08-17 it
    stopped beating at 20:40:24 and looked dead, and restarting the API started a
    SECOND destroy of the same cluster.

    Renewal therefore runs in its own thread rather than at the top of the loop.
    The deadline is what keeps this honest: a wedged sweep stops renewing and
    lets a standby take over, so this cannot become a lease held by a dead loop.
    """
    while not stop.wait(every):
        if time.monotonic() >= deadline:
            _audit_leader("poller.sweep.watchdog")
            return
        try:
            with SessionLocal() as session:
                leader.try_acquire(session, "poller")
        except Exception:  # noqa: BLE001 — a renewal failure is not fatal here
            pass


def _poller_loop() -> None:
    """Run the poller in exactly one replica at a time (F-OPS-01). Each tick we
    renew the leader lease; only the leader runs _poll_once. If the leader dies,
    its lease expires and a standby takes over within the TTL. On the poll cadence
    the renewal is more frequent than the TTL so the lease never lapses in place."""
    interval = max(2, int(os.getenv("POLL_INTERVAL_SECONDS", "30")))
    renew = max(2, min(interval, leader.ttl_seconds() // 2))
    last_poll = 0.0
    was_leader = False
    while not _poller_stop.is_set():
        is_leader = False
        try:
            with SessionLocal() as session:
                is_leader = leader.try_acquire(session, "poller")
        except Exception:  # noqa: BLE001 — treat any lease error as "not leader"
            is_leader = False

        if is_leader and not was_leader:
            _audit_leader("poller.leader.acquired")
        elif was_leader and not is_leader:
            _audit_leader("poller.leader.lost")
        was_leader = is_leader

        now = time.monotonic()
        if is_leader and (now - last_poll) >= interval:
            last_poll = now
            # Renew from a separate thread for the duration of the sweep. The
            # sweep itself blocks on real infrastructure work far longer than the
            # lease TTL, and the loop cannot renew while it does.
            _sweep_stop = threading.Event()
            _sweep_hb = threading.Thread(
                target=_renew_lease_during_sweep,
                args=(_sweep_stop, renew, time.monotonic() + sweep_watchdog_seconds()),
                name="poller-heartbeat", daemon=True)
            _sweep_hb.start()
            try:
                _poll_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                pass
            finally:
                _sweep_stop.set()
        _poller_stop.wait(renew)

    # Graceful handover: release the lease so a standby takes over immediately.
    if was_leader:
        try:
            with SessionLocal() as session:
                leader.release(session, "poller")
            _audit_leader("poller.leader.released")
        except Exception:  # noqa: BLE001
            pass


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


# --- Hourly cloud option cache ----------------------------------------------

_catalogue_thread: threading.Thread | None = None
_catalogue_stop = threading.Event()


def _catalogue_loop() -> None:
    """Refresh the cloud option cache on an interval, on ONE instance only.

    Leader-elected like the poller: several API replicas all calling the tenancy
    every hour would multiply the API cost of the feature by the replica count
    for no benefit, since they all write the same rows to the same database.
    """
    interval = cloud_options.refresh_interval_seconds()
    while not _catalogue_stop.is_set():
        try:
            with SessionLocal() as session:
                if leader.try_acquire(session, "catalogue"):
                    cloud_options.refresh(session, _orchestrator_cloud_options)
        except Exception:  # noqa: BLE001 - a refresh must never kill the loop
            pass
        _catalogue_stop.wait(interval)


def _start_catalogue_refresh() -> None:
    """Start the cloud-option refresh thread (called on startup)."""
    global _catalogue_thread
    if cloud_options.enabled() and (
        _catalogue_thread is None or not _catalogue_thread.is_alive()
    ):
        _catalogue_stop.clear()
        _catalogue_thread = threading.Thread(
            target=_catalogue_loop, name="cloud-catalogue", daemon=True
        )
        _catalogue_thread.start()


def _stop_catalogue_refresh() -> None:
    """Signal the cloud-option refresh loop to exit (called on shutdown)."""
    _catalogue_stop.set()
