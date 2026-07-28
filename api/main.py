import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.attachment import build_request_pdf
from api.audit import append_audit
from api.auth import get_requester
from api.jira import (
    JiraError,
    build_ticket_body,
    create_issue,
    get_status,
    inprogress_status,
    jira_mode,
    resolve_fields,
    resolved_status,
    transition_issue,
)
from api.plan_preview import build_plan_preview
from api.policy import PolicyUnavailable, get_policy_evaluator
from api.pricing import estimate_cost
from api.sizing import resolve_components
from api.validation import validate_submission
from common.signing import sign
from db.models import (
    Approval,
    AuditLog,
    CostCentre,
    Environment,
    Estimate,
    Project,
    ProvisionedResource,
    Request,
    RequestComponent,
    SizingAnchor,
    Technology,
)
from db.session import SessionLocal

load_dotenv()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the background Jira poller on startup, stop it on shutdown.

    The functions are defined further down; they run only when AUTO_PROVISION
    is on, so nothing polls by default.
    """
    _start_poller()
    try:
        yield
    finally:
        _stop_poller()


app = FastAPI(title="Infra Portal API", lifespan=_lifespan)

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


def _post_to_orchestrator(body: bytes, signature: str, path: str = "/provision"):
    """POST the signed handoff to an orchestrator path, retrying transient failures.

    Returns (response, error). error is None on success; otherwise a string.
    Safe to retry because the handoff is idempotent (keyed on the request).
    """
    error = None
    for attempt in range(ORCH_MAX_ATTEMPTS):
        try:
            response = httpx.post(
                f"{ORCHESTRATOR_URL}{path}",
                content=body,
                headers={"X-Signature": signature, "Content-Type": "application/json"},
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
    technologies: list[TechnologyOut]
    environments: list[EnvironmentOut]


@app.get("/api/lookups", response_model=LookupsResponse)
def lookups(session: Session = Depends(get_session)) -> LookupsResponse:
    """Read-only reference data for the guided-request form's dropdowns."""
    return LookupsResponse(
        projects=session.scalars(select(Project).order_by(Project.name)).all(),
        cost_centres=session.scalars(select(CostCentre).order_by(CostCentre.name)).all(),
        technologies=session.scalars(select(Technology).order_by(Technology.name)).all(),
        environments=session.scalars(select(Environment).order_by(Environment.name)).all(),
    )


# --- Requests: drafts + submission (increment 1.3) ---------------------------

# The editable scalar fields a draft carries (components handled separately;
# reference/status/requester are managed).
REQUEST_FIELDS = (
    "request_type",
    "project_code",
    "cost_centre_code",
    "deployment_target",
    "environment_name",
    "target_environment",
    "source_reference",
    "data_classification",
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
    deployment_target: str | None = None
    environment_name: str | None = None
    target_environment: str | None = None
    source_reference: str | None = None
    data_classification: str | None = None
    components: list[ComponentIn] | None = None


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


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reference: str
    status: str
    requester: str
    request_type: str | None = None
    project_code: str | None = None
    cost_centre_code: str | None = None
    deployment_target: str | None = None
    environment_name: str | None = None
    target_environment: str | None = None
    source_reference: str | None = None
    data_classification: str | None = None
    components: list[ComponentOut] = []
    estimate: EstimateOut | None = None
    approval: ApprovalOut | None = None


def _load_request(reference: str, session: Session) -> Request:
    req = session.scalar(select(Request).where(Request.reference == reference))
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{reference}' not found.")
    return req


@app.post("/api/requests/draft", response_model=RequestOut)
def save_draft(
    body: DraftIn,
    session: Session = Depends(get_session),
    requester: str = Depends(get_requester),
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
    status: str | None = None,
    session: Session = Depends(get_session),
) -> list[RequestOut]:
    """List requests newest-first, optionally filtered to one requester/status.

    Powers the portal's 'My requests' dashboard (no status filter) and the
    decommission form's source dropdown (status='provisioned'). Read-only.
    """
    stmt = select(Request).order_by(Request.id.desc())
    if requester:
        stmt = stmt.where(Request.requester == requester)
    if status:
        stmt = stmt.where(Request.status == status)
    return [RequestOut.model_validate(r) for r in session.scalars(stmt)]


@app.get("/api/requests/{reference}", response_model=RequestOut)
def get_request(reference: str, session: Session = Depends(get_session)) -> RequestOut:
    """Load a draft (or submitted request) so it can be resumed/viewed."""
    return RequestOut.model_validate(_load_request(reference, session))


@app.post("/api/requests/{reference}/submit")
def submit_request(
    reference: str,
    session: Session = Depends(get_session),
    policy_eval=Depends(get_policy_evaluator),
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
            req.environment_name = req.environment_name or source.environment_name

    components_data = [
        {"technology_code": c.technology_code, "size": c.size} for c in req.components
    ]
    data = {field: getattr(req, field) for field in REQUEST_FIELDS}
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
        return JSONResponse(
            status_code=422, content={"policy_violations": verdict["violations"]}
        )

    req.status = "submitted"
    # Capture the server-computed estimate as a stored fact at submission (1.6).
    breakdown = estimate_cost(components_data, req.deployment_target, session)
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
    # A one-page request + costing PDF for the approver (2.4c).
    attachment = None
    try:
        sizing = resolve_components(components_data, session)
        pdf = build_request_pdf(req, breakdown, sizing)
        attachment = (f"request-{req.reference}.pdf", pdf)
    except Exception:  # noqa: BLE001 — never fail a submit over the PDF
        attachment = None
    try:
        req.approval = create_issue(session, req, ticket_body, attachment=attachment)
    except JiraError as exc:
        session.rollback()
        return JSONResponse(
            status_code=502,
            content={"error": f"Could not raise the Jira approval ticket: {exc}"},
        )

    session.commit()
    return RequestOut.model_validate(req)


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


@app.post("/api/cost")
def cost(body: CostIn, session: Session = Depends(get_session)) -> dict:
    """Estimate one-time/monthly/annual cost for the components on a target."""
    components = [
        {"technology_code": c.technology_code, "size": c.size} for c in body.components
    ]
    return estimate_cost(components, body.deployment_target, session)


# --- Approval + signed orchestrator handoff (increment 1.9) -------------------


def _policy_input(req: Request) -> dict:
    data = {field: getattr(req, field) for field in REQUEST_FIELDS}
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


@app.post("/api/approvals/{jira_key}/approve")
def approve(jira_key: str, session: Session = Depends(get_session)):
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

    appr.status = "approved"
    append_audit(session, "approval.approved", reference=req.reference, jira_key=jira_key,
                 actor="jira")
    session.commit()

    # Decommission requests tear down the referenced resources instead of
    # provisioning new ones (2.9).
    if req.request_type == "decommission":
        return _decommission(session, req, actor="approver")

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
def request_audit(reference: str, session: Session = Depends(get_session)) -> dict:
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


# --- Real apply / destroy (increment 2.6b) -----------------------------------


def _handoff_payload(req: Request, *, ttl_expiry: str | None = None) -> tuple[bytes, str]:
    """Build and sign the orchestrator handoff for a request."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "idempotency_key": req.approval.jira_key,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": req.approval.jira_key,
        "reference": req.reference,
        "policy_input": _policy_input(req),
        "approved_monthly": float(req.estimate.monthly) if req.estimate else None,
    }
    if ttl_expiry:
        payload["ttl_expiry"] = ttl_expiry
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
            req.status = "apply-failed"
            append_audit(session, "apply.failed", reference=reference, jira_key=jira_key,
                         detail={"error": error or (response.text if response else "")})
            session.commit()
            return

        result = response.json()
        res = result.get("resource", {})
        session.add(ProvisionedResource(
            reference=reference, kind=res.get("kind", "resource"), name=res.get("name", ""),
            region=res.get("region"), details=res.get("outputs", {}),
            ttl_expiry=datetime.fromisoformat(ttl_expiry_iso), lifecycle_state="active",
        ))
        _transition_jira(session, req, resolved_status(), "jira.resolved")
        req.status = "provisioned"
        append_audit(session, "provisioned", reference=reference, jira_key=jira_key, detail=result)
        session.commit()


@app.post("/api/requests/{reference}/apply")
def apply_request(reference: str, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    """Start provisioning: set Jira In Progress, then apply in the background.

    Returns immediately with status 'in-progress' so the portal can show live
    progress; the background task creates the resource and sets Jira Resolved.
    """
    req = _load_request(reference, session)
    if req.approval is None:
        raise HTTPException(status_code=400, detail="Request has no approval to apply.")
    if req.status in ("in-progress", "provisioned"):
        return {"status": req.status, "message": f"{reference} is already {req.status}."}
    if req.status != "planned":
        return JSONResponse(
            status_code=409,
            content={"error": "Request must be approved and planned before it can be applied."},
        )

    ttl_expiry = datetime.now(timezone.utc) + timedelta(days=PROVISION_TTL_DAYS)
    body, signature = _handoff_payload(req, ttl_expiry=ttl_expiry.isoformat())
    # Move Jira to In Progress and mark the request in-progress up front.
    _transition_jira(session, req, inprogress_status(), "jira.in_progress")
    req.status = "in-progress"
    append_audit(session, "provisioning.started", reference=reference,
                 jira_key=req.approval.jira_key, actor="approver")
    session.commit()

    background_tasks.add_task(_provision_in_background, reference, req.approval.jira_key,
                             body, signature, ttl_expiry.isoformat())
    return {"status": "in-progress", "message": f"Provisioning {reference} started."}


@app.post("/api/requests/{reference}/destroy")
def destroy_request(reference: str, session: Session = Depends(get_session)):
    """Destroy the resources created for a request (rollback / cleanup)."""
    req = _load_request(reference, session)
    if req.approval is None:
        raise HTTPException(status_code=400, detail="Request has no approval.")

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
        req.status = "decommission-failed"
        append_audit(session, "destroy.failed", reference=req.reference,
                     jira_key=req.approval.jira_key,
                     detail={"source": source.reference,
                             "error": error or (response.text if response else "")})
        session.commit()
        return {"approval": "approved", "decommissioned": False,
                "error": error or (response.text if response else "destroy failed")}

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


def _advance_request(session: Session, req: Request) -> str:
    """Advance one request as far as its live Jira status allows, in one pass.

    Idempotent and safe to call every cycle: each step commits and re-entry
    resumes from the committed state. Returns the request status afterwards.
    Reuses the exact building blocks the manual buttons use, so the poller and
    a manual click can never diverge or double-provision (the orchestrator keeps
    its own idempotency ledger keyed on the Jira key).
    """
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
        # Approved — record it once, then run the same signed handoff (plan) the
        # approve button runs. On a retry (a previous plan failed) appr.status is
        # already 'approved', so we skip straight to re-planning.
        if appr.status != "approved":
            appr.status = "approved"
            append_audit(session, "approval.approved", reference=req.reference,
                         jira_key=jira_key, actor="poller")
            session.commit()
        # Decommission tears down the referenced request instead of provisioning.
        if req.request_type == "decommission":
            _decommission(session, req, actor="poller")
            return req.status
        body, signature = _handoff_payload(req)
        append_audit(session, "orchestrator.handoff", reference=req.reference,
                     jira_key=jira_key, detail={"contract": CONTRACT_VERSION})
        session.commit()
        response, error = _post_to_orchestrator(body, signature)
        if response is None or response.status_code != 200:
            append_audit(session, "plan.failed", reference=req.reference, jira_key=jira_key,
                         detail={"error": error or (response.text if response else "")})
            session.commit()
            return req.status  # still 'submitted' — retried next cycle
        result = response.json()
        if result.get("provisioned"):  # mock mode fully provisions on the handoff
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
        ttl_expiry = datetime.now(timezone.utc) + timedelta(days=PROVISION_TTL_DAYS)
        body, signature = _handoff_payload(req, ttl_expiry=ttl_expiry.isoformat())
        _transition_jira(session, req, inprogress_status(), "jira.in_progress")
        req.status = "in-progress"
        append_audit(session, "provisioning.started", reference=req.reference,
                     jira_key=jira_key, actor="poller")
        session.commit()
        _provision_in_background(req.reference, jira_key, body, signature,
                                 ttl_expiry.isoformat())
        session.refresh(req)  # pick up 'provisioned' / 'apply-failed' from the apply
    return req.status


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
