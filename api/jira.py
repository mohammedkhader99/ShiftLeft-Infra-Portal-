"""Jira Service Management adapter (increment 1.8).

Approval authority lives in Jira (ARCHITECTURE.md P1). In mock mode (the
default until Phase 2) this fakes issue creation: it assigns a ticket key and
returns the stored-shape ticket, without any external call. The ticket body
carries configuration + cost + plan preview together so the approver sees the
price with the request (§4 / cost-before-approval).
"""

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Approval, Request


def _is_mock() -> bool:
    return os.getenv("USE_MOCK", "true").strip().lower() == "true"


def _next_key(session: Session) -> str:
    seq = (session.scalar(select(func.max(Approval.id))) or 0) + 1
    return f"INFRA-{1000 + seq}"


def build_ticket_body(req: Request, estimate: dict, plan_preview: str) -> str:
    """Assemble the ticket text: configuration + cost + plan preview together."""
    lines = [
        f"Request {req.reference} ({req.request_type})",
        f"Requester: {req.requester}",
        f"Project: {req.project_code}    Cost centre: {req.cost_centre_code}",
        f"Deployment target: {req.deployment_target}",
        f"Data classification: {req.data_classification}",
    ]
    if req.environment_name:
        lines.append(f"New environment: {req.environment_name}")
    if req.target_environment:
        lines.append(f"Target environment: {req.target_environment}")

    lines.append("")
    lines.append("Components:")
    for comp in req.components:
        lines.append(f"  - {comp.technology_code} ({comp.size})")

    totals = estimate.get("totals", {})
    lines.append("")
    lines.append(
        f"Estimated cost ({estimate.get('currency', 'AED')}): "
        f"one-time {totals.get('one_time', 0):.2f}, "
        f"monthly {totals.get('monthly', 0):.2f}, "
        f"annual {totals.get('annual', 0):.2f}"
    )
    lines.append("")
    lines.append("--- Plan preview ---")
    lines.append(plan_preview)
    return "\n".join(lines)


def create_issue(session: Session, req: Request, body: str) -> Approval:
    """Create the (mock) Jira issue and return an Approval row (not yet added)."""
    if not _is_mock():
        raise NotImplementedError("Real Jira integration lands in Phase 2 (USE_MOCK=false).")
    key = _next_key(session)
    return Approval(
        jira_key=key,
        status="pending",
        ticket_url=f"https://jira.mock.local/browse/{key}",
        ticket_body=body,
    )
