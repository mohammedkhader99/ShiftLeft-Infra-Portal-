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

import json
import os
import re

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Approval, Request

# Cache of template fields (Jira config is stable; fetched once per process).
_template_cache: dict | None = None


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


def _extra_fields() -> dict:
    """Explicit custom-field overrides, supplied as a JSON env value.

    Merged last, so it can override anything the template provides.
    """
    raw = os.getenv("JIRA_EXTRA_FIELDS", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JiraError(f"JIRA_EXTRA_FIELDS is not valid JSON: {exc}") from exc


# Fields we set ourselves or that are never copied from a template.
_TEMPLATE_SKIP = {"summary", "description", "project", "issuetype", "reporter",
                  "attachment", "issuelinks", "comment"}


def _to_write(schema: dict, val) -> object | None:
    """Convert a field's *read* value into its Jira *write* format.

    Handles selects, multi-selects, user pickers, priority, and Insight/Assets
    object fields (which render as "label (KEY)" and are written as
    [{"key": "KEY"}]).
    """
    t = schema.get("type")
    items = schema.get("items")
    if val in (None, "", [], {}):
        return None
    if isinstance(val, dict) and "title" in val and "body" in val:
        return None  # read-only "message" custom field
    if t == "option":
        return {"value": val["value"]} if isinstance(val, dict) and val.get("value") else None
    if t == "user":
        return {"name": val["name"]} if isinstance(val, dict) else None
    if t == "priority":
        return {"id": val["id"]} if isinstance(val, dict) else None
    if t in ("string", "number", "date", "datetime"):
        return val
    if t in ("array", "any") and isinstance(val, list):
        out = []
        for v in val:
            if items == "option" and isinstance(v, dict):
                out.append({"value": v.get("value")})
            elif items == "user" and isinstance(v, dict):
                out.append({"name": v.get("name")})
            elif isinstance(v, str):
                m = re.search(r"\(([^)]+)\)\s*$", v)  # Insight "label (KEY)"
                if m:
                    out.append({"key": m.group(1)})
            elif isinstance(v, dict) and v.get("key"):
                out.append({"key": v["key"]})
        return out or None
    return None


def _template_fields() -> dict:
    """Replicate the create-screen fields of a template ticket (JIRA_TEMPLATE_ISSUE).

    Reads the target project/issue-type's create screen and the template issue,
    then copies each create-screen field's value in the correct write format.
    Cached for the process. Empty if no template is configured.
    """
    global _template_cache
    if _template_cache is not None:
        return _template_cache

    key = os.getenv("JIRA_TEMPLATE_ISSUE", "").strip()
    if not key:
        _template_cache = {}
        return {}

    base, h = _base_url(), _headers()
    project, issue_type = _project_key(), _issue_type()
    try:
        its = httpx.get(f"{base}/rest/api/2/issue/createmeta/{project}/issuetypes",
                        headers=h, timeout=15.0).json()
        values = its.get("values", its if isinstance(its, list) else [])
        match = [v for v in values if v.get("name") == issue_type]
        if not match:
            raise JiraError(f"issue type '{issue_type}' not available on {project}")
        tid = match[0]["id"]
        meta = httpx.get(f"{base}/rest/api/2/issue/createmeta/{project}/issuetypes/{tid}",
                         headers=h, timeout=15.0).json().get("values", [])
        template = httpx.get(f"{base}/rest/api/2/issue/{key}",
                             headers=h, timeout=15.0).json().get("fields", {})
    except JiraError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"could not read Jira template {key}: {exc}") from exc

    fields: dict = {}
    for f in meta:
        fid = f.get("fieldId")
        if fid in _TEMPLATE_SKIP or fid not in template:
            continue
        written = _to_write(f.get("schema", {}), template[fid])
        if written is not None:
            fields[fid] = written
    _template_cache = fields
    return fields


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

    # Replicate a template ticket's fields (auto-discovered), then apply any
    # explicit overrides. Extra fields win over the template.
    fields.update(_template_fields())
    fields.update(_extra_fields())

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
