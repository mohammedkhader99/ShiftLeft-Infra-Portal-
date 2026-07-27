"""Jira Service Management adapter (increment 1.8 mock; live added in 2.4).

Approval authority lives in Jira (ARCHITECTURE.md P1). JIRA_MODE selects:
- mock (default): fake issue creation, no external call; approval status is
  driven from our own DB (the demo "approve" button).
- live: create real issues in Jira Server/Data Center (REST API v2, Personal
  Access Token / Bearer auth) and read the real approval status back, so the
  orchestrator re-verifies against Jira itself (§4).

The ticket body carries configuration + cost + plan preview together, and in
live mode the reporter is set to the real requester so it routes to their line
manager via Jira's own workflow.
"""

import os

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Approval, Request


class JiraError(RuntimeError):
    """A live Jira call failed — the caller should surface this, not proceed."""


def jira_mode() -> str:
    return os.getenv("JIRA_MODE", "mock").strip().lower()


def _base_url() -> str:
    return os.getenv("JIRA_BASE_URL", "").rstrip("/")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('JIRA_PAT', '')}",
        "Content-Type": "application/json",
    }


def _project_key() -> str:
    return os.getenv("JIRA_PROJECT_KEY", "INFRA")


def _issue_type() -> str:
    return os.getenv("JIRA_ISSUE_TYPE", "Task")


def _set_reporter() -> bool:
    return os.getenv("JIRA_SET_REPORTER", "true").strip().lower() == "true"


def _status_set(env_var: str, default: str) -> set[str]:
    raw = os.getenv(env_var, default)
    return {s.strip() for s in raw.split(",") if s.strip()}


def normalize_status(name: str) -> str:
    """Map a Jira status name to pending | approved | rejected."""
    name = (name or "").strip()
    if name in _status_set("JIRA_APPROVED_STATUSES", "Approved,Done"):
        return "approved"
    if name in _status_set("JIRA_REJECTED_STATUSES", "Rejected,Cancelled"):
        return "rejected"
    return "pending"


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


def _next_mock_key(session: Session) -> str:
    seq = (session.scalar(select(func.max(Approval.id))) or 0) + 1
    return f"INFRA-{1000 + seq}"


def create_issue(session: Session, req: Request, body: str) -> Approval:
    """Create the Jira issue and return an Approval row (not yet added)."""
    if jira_mode() != "live":
        key = _next_mock_key(session)
        return Approval(
            jira_key=key,
            status="pending",
            ticket_url=f"https://jira.mock.local/browse/{key}",
            ticket_body=body,
        )

    fields = {
        "project": {"key": _project_key()},
        "summary": f"Provisioning request {req.reference} ({req.request_type})",
        "description": body,
        "issuetype": {"name": _issue_type()},
    }
    # Set the reporter to the real requester so Jira routes to their manager.
    if _set_reporter() and req.requester:
        fields["reporter"] = {"name": req.requester}

    try:
        response = httpx.post(
            f"{_base_url()}/rest/api/2/issue",
            json={"fields": fields},
            headers=_headers(),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"could not reach Jira: {exc}") from exc
    if response.status_code >= 300:
        raise JiraError(f"Jira returned {response.status_code}: {response.text}")

    key = response.json()["key"]
    return Approval(
        jira_key=key,
        status="pending",
        ticket_url=f"{_base_url()}/browse/{key}",
        ticket_body=body,
    )


def get_status(jira_key: str) -> str:
    """Read the live approval status from Jira: pending | approved | rejected."""
    try:
        response = httpx.get(
            f"{_base_url()}/rest/api/2/issue/{jira_key}",
            params={"fields": "status"},
            headers=_headers(),
            timeout=8.0,
        )
        response.raise_for_status()
        name = response.json()["fields"]["status"]["name"]
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"could not read Jira status: {exc}") from exc
    return normalize_status(name)
