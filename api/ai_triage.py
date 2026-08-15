"""AI failure triage (F-RPT-08).

When a provisioning request fails or is blocked, this reads the request's real
failure signals — its status, its `status_detail`, and its failure-related audit
trail (apply.failed, destroy.failed, plan.failed, scan.findings, poll.error,
decommission.source_missing) — and produces a plain-English diagnosis: what went
wrong, the likely cause(s), and suggested next steps.

Authority boundary (ARCHITECTURE.md §7, hard rule): the AI **triages and advises
only**. It never retries, applies, re-provisions, approves, or changes the
request — a human decides what to do with the diagnosis.

Two modes (AI_MODE), reusing the F-RPT-06/07 pattern (see [[ai-features]]):
  - mock  (default): a deterministic classifier over the failure signals. No
           network, no key — demos and tests offline.
  - live:  the Claude API writes a richer diagnosis from the same signals.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ai_drafter import AiUnavailable, ai_mode, ai_model, anthropic_client
from db.models import AuditLog, Request

# Request statuses that mean the request failed outright.
FAILURE_STATUSES = {"apply-failed", "teardown-failed", "rejected",
                    # Built, but the machine itself says it is not working.
                    "verify-failed"}
# Audit events that record a failure or a block worth diagnosing.
FAILURE_EVENTS = {
    "apply.failed", "destroy.failed", "decommission.source_missing",
    "plan.failed", "poll.error", "scan.findings", "change_window.held",
}

# Event-driven classes (precise — keyed on the audit event, not loose keywords).
_SCAN_CAUSE = "The infrastructure plan failed a security scan (high-severity IaC findings)."
_SCAN_STEP = (
    "Review the scan findings in the evidence pack and fix the flagged resource "
    "(e.g. public exposure, missing encryption at rest), or have an admin grant a "
    "documented waiver."
)
_SOURCE_CAUSE = "The request this decommission targets no longer exists or isn't provisioned."
_SOURCE_STEP = "Pick a valid provisioned request to decommission."

# Keyword-driven classes (keywords matched against the lower-cased status_detail +
# the failure events' details). A signal may match several; we keep the first few.
_KEYWORD_CLASSES: list[tuple[tuple[str, ...], str, str]] = [
    (("policy", "violation", "opa"),
     "Blocked by an OPA policy rule.",
     "Adjust the request to comply (the violation message names the rule), or an "
     "approver/admin can grant a policy waiver (F-GOV-02)."),
    (("quota", "budget", "over budget", "guardrail"),
     "Blocked by a FinOps guardrail (project quota or cost-centre budget).",
     "Reduce the request, raise the quota/budget in the Admin console, or obtain an "
     "exception."),
    (("four-eyes", "quorum", "approvals", "approved by the requester", "segregation"),
     "Held for segregation-of-duties / approval quorum.",
     "A different approver (not the requester) must approve in Jira; check the "
     "quorum count on the ticket."),
    (("orchestrator", "unreachable", "could not reach", "connection", "timed out", "timeout"),
     "The orchestrator could not be reached — a transient infrastructure/network issue.",
     "Check the orchestrator service health and retry; the background poller will "
     "also re-attempt on its next cycle."),
    (("credential", "unauthorized", "permission", "denied", "401", "403", "forbidden"),
     "A credential or permission problem in the provisioning backend.",
     "Verify the orchestrator's cloud credentials and IAM permissions for the target."),
    (("terraform", "tofu", "provider", "resource conflict", "already exists", "invalid variable"),
     "Terraform plan/apply failed (a resource conflict or invalid configuration).",
     "Inspect the plan.failed / apply.failed detail for the Terraform error — e.g. a "
     "name/resource conflict (a unique environment name often fixes it), an invalid "
     "variable, or a provider/credential issue — then correct it and re-apply."),
    (("change window", "held outside", "will provision when the window"),
     "Held outside the approved change window.",
     "It will provision automatically when the window opens — no action needed, or "
     "adjust the window in the Admin console."),
]


def _failure_signals(req: Request, session: Session) -> list[dict]:
    """The request's failure-related audit entries, most recent first."""
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.reference == req.reference)
        .order_by(AuditLog.id.desc())
    ).all()
    signals = []
    for r in rows:
        event = r.event or ""
        if event in FAILURE_EVENTS or "fail" in event or "error" in event:
            signals.append({
                "event": event,
                "detail": r.detail or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
    return signals


def _is_failing(req: Request, signals: list[dict]) -> bool:
    return bool(
        req.status in FAILURE_STATUSES
        or signals
        or (req.status_detail and req.status_detail.strip())
    )


def _match_classes(
    text: str, event_names: set[str], request_type: str | None
) -> tuple[list[str], list[str]]:
    causes: list[str] = []
    steps: list[str] = []

    def add(cause: str, step: str) -> None:
        if cause not in causes:
            causes.append(cause)
            steps.append(step)

    # Event-driven (precise): a scan block, and a decommission whose source is gone.
    if "scan.findings" in event_names or "scan" in text or "high-severity" in text:
        add(_SCAN_CAUSE, _SCAN_STEP)
    if "decommission.source_missing" in event_names or (
        request_type == "decommission" and ("not found" in text or "no longer" in text)
    ):
        add(_SOURCE_CAUSE, _SOURCE_STEP)

    # Keyword-driven for the rest.
    for keywords, cause, step in _KEYWORD_CLASSES:
        if any(k in text for k in keywords):
            add(cause, step)
    return causes[:3], steps[:3]


def _triage_mock(req: Request, signals: list[dict]) -> tuple[str, list[str], list[str]]:
    detail_text = " ".join(json.dumps(s["detail"], default=str) for s in signals)
    text = f"{req.status_detail or ''} {detail_text}".lower()
    event_names = {s["event"] for s in signals}
    causes, steps = _match_classes(text, event_names, req.request_type)
    if not causes:
        # Failing, but no known pattern matched — point at the raw signals.
        causes = ["The request failed or was blocked; the specific error is in the status "
                  "detail and the failure audit events."]
        steps = ["Read the status detail and the most recent failure event's detail for the "
                 "underlying error, then correct the request or the backend and re-apply."]
    lead = (req.status_detail or "").strip() or "See the failure events for the specific error."
    summary = f"{req.reference} is in state '{req.status}'. {lead}"
    return summary, causes, steps


_SYSTEM = (
    "You are a platform engineer triaging a failed infrastructure provisioning "
    "request. You are given the request's status, its status detail, and its "
    "failure-related audit events. Produce a short plain-English diagnosis: a "
    "one- to two-sentence summary of what went wrong, the likely cause(s), and "
    "concrete next steps. Base everything on the signals provided — do not invent "
    "errors. You are only diagnosing and advising: you never retry, apply, "
    "re-provision, approve, or change the request."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "likely_causes", "next_steps"],
    "properties": {
        "summary": {"type": "string"},
        "likely_causes": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}


def _triage_live(req: Request, signals: list[dict]) -> tuple[str, list[str], list[str]]:
    client = anthropic_client()
    context = json.dumps({
        "reference": req.reference,
        "status": req.status,
        "status_detail": req.status_detail,
        "request_type": req.request_type,
        "deployment_target": req.deployment_target,
        "failure_events": signals,
    }, indent=2, default=str)
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=1500,
            system=_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{
                "role": "user",
                "content": f"Failure signals:\n{context}\n\nDiagnose the failure and suggest next steps.",
            }],
        )
    except Exception as exc:  # noqa: BLE001
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise AiUnavailable("The AI service returned an empty diagnosis.")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiUnavailable("The AI service returned an unreadable diagnosis.") from exc

    def _strs(values):
        return [v for v in (values or []) if isinstance(v, str)]

    return (parsed.get("summary") or "").strip(), _strs(parsed.get("likely_causes")), _strs(parsed.get("next_steps"))


def triage_request(req: Request, session: Session) -> dict:
    """Diagnose a request's failure. `req` is a loaded Request (the caller handles
    the 404). Returns {mode, reference, status, status_detail, failing, summary,
    likely_causes, next_steps, signals}. Read-only — nothing is retried or changed."""
    signals = _failure_signals(req, session)
    failing = _is_failing(req, signals)

    if not failing:
        return {
            "mode": "mock",
            "reference": req.reference,
            "status": req.status,
            "status_detail": req.status_detail,
            "failing": False,
            "summary": f"No failure detected — {req.reference} is '{req.status}'.",
            "likely_causes": [],
            "next_steps": [],
            "signals": [],
        }

    if ai_mode() == "live":
        summary, causes, steps = _triage_live(req, signals)
        mode = "live"
    else:
        summary, causes, steps = _triage_mock(req, signals)
        mode = "mock"

    return {
        "mode": mode,
        "reference": req.reference,
        "status": req.status,
        "status_detail": req.status_detail,
        "failing": True,
        "summary": summary,
        "likely_causes": causes,
        "next_steps": steps,
        "signals": signals,
    }
