import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.audit import append_audit
from api.auth import get_requester
from api.jira import build_ticket_body, create_issue
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
    Request,
    RequestComponent,
    SizingAnchor,
    Technology,
)
from db.session import SessionLocal

load_dotenv()

app = FastAPI(title="Infra Portal API")

# No login yet (real identity arrives in increment E1); stamp a fixed requester.
MOCK_REQUESTER = "mohammed.khader@emaratechg.ae"
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:9091")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-mock-secret")


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

    # Raise the Jira approval with config + cost + plan preview together (1.8).
    plan_preview = build_plan_preview(req, session)
    ticket_body = build_ticket_body(req, breakdown, plan_preview)
    req.approval = create_issue(session, req, ticket_body)

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
    """Approval status — used by the orchestrator to re-verify authority."""
    appr = _load_approval(jira_key, session)
    return {
        "jira_key": appr.jira_key,
        "status": appr.status,
        "reference": appr.request.reference,
    }


@app.post("/api/approvals/{jira_key}/approve")
def approve(jira_key: str, session: Session = Depends(get_session)) -> dict:
    """Mock Jira approval action, then fire the signed orchestrator handoff.

    The portal/API never executes: approval lives in Jira, the orchestrator
    executes after re-verifying (ARCHITECTURE.md §4). Here the approval is
    recorded, then a signed webhook is sent to the orchestrator.
    """
    appr = _load_approval(jira_key, session)
    req = appr.request

    appr.status = "approved"
    append_audit(session, "approval.approved", reference=req.reference, jira_key=jira_key,
                 actor="approver@jira.mock")
    session.commit()

    # Signed handoff. The signature is authenticity; the orchestrator re-checks
    # authority (approval + policy) itself.
    payload = {"jira_key": jira_key, "reference": req.reference, "policy_input": _policy_input(req)}
    body = json.dumps(payload, sort_keys=True).encode()
    signature = sign(WEBHOOK_SECRET, body)
    append_audit(session, "orchestrator.handoff", reference=req.reference, jira_key=jira_key,
                 detail={"orchestrator": ORCHESTRATOR_URL})
    session.commit()

    try:
        response = httpx.post(
            f"{ORCHESTRATOR_URL}/provision",
            content=body,
            headers={"X-Signature": signature, "Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        append_audit(session, "orchestrator.unreachable", reference=req.reference,
                     jira_key=jira_key, detail={"error": str(exc)})
        session.commit()
        return JSONResponse(
            status_code=503, content={"error": "Orchestrator unreachable — not provisioned."}
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
