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

from db.models import Approval, Request, Subsidiary

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


# --- Subsidiary master data synced from Jira (customfield_36200) -------------

def subsidiary_field() -> str:
    """The Jira create-screen field whose options are the subsidiary list."""
    return os.getenv("JIRA_SUBSIDIARY_FIELD", "customfield_36200").strip() or "customfield_36200"


def _option_to_row(option) -> dict | None:
    """Map one Jira allowedValue to a {code, name} subsidiary row.

    Handles a normal select option ({id, value}) and a JSM Assets/Insight object
    ({objectId/objectKey, label/name}). Returns None if it carries no name. Codes
    and names are capped to the DB column widths (32 / 120)."""
    if not isinstance(option, dict):
        return None
    name = option.get("value") or option.get("name") or option.get("label")
    code = option.get("id") or option.get("objectKey") or option.get("key") or name
    if not name:
        return None
    return {"code": str(code).strip()[:32], "name": str(name).strip()[:120]}


def fetch_subsidiary_options() -> list[dict]:
    """Read the subsidiary field's allowed values from Jira's create screen.

    Returns a de-duplicated list of {code, name}. Live only — raises JiraError on
    any failure (unreachable, field not on the create screen, etc.). Reuses the
    same createmeta endpoints as the template-field replication above."""
    base, h = _base_url(), _headers()
    project, issue_type = _project_key(), _issue_type()
    field_id = subsidiary_field()
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
    except JiraError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"could not read Jira create metadata: {exc}") from exc

    field = next((f for f in meta if f.get("fieldId") == field_id), None)
    if field is None:
        raise JiraError(
            f"field '{field_id}' is not on the {project}/{issue_type} create screen"
        )
    rows: list[dict] = []
    seen: set[str] = set()
    for option in field.get("allowedValues", []):
        row = _option_to_row(option)
        if row and row["code"] not in seen:
            seen.add(row["code"])
            rows.append(row)
    return rows


def sync_subsidiaries(session: Session) -> dict:
    """Sync the Subsidiary table from Jira's subsidiary field (live only).

    Upserts each Jira option (adds new, renames changed, reactivates) and marks
    any active subsidiary Jira no longer offers as inactive — without deleting
    it, so historical requests keep working. **Guardrail:** a failed or empty
    fetch is a no-op, so a Jira blip can never wipe the list. Does NOT commit —
    the caller owns the transaction (and writes the audit entry). Returns a
    summary including a `changed` count."""
    if jira_mode() != "live":
        return {"synced": False, "changed": 0, "reason": "jira mode is not live"}
    try:
        options = fetch_subsidiary_options()
    except JiraError as exc:
        return {"synced": False, "changed": 0, "reason": str(exc)}
    if not options:
        return {"synced": False, "changed": 0, "reason": "Jira returned no options"}

    existing = {s.code: s for s in session.scalars(select(Subsidiary)).all()}
    live_codes = {opt["code"] for opt in options}
    added = updated = reactivated = deactivated = 0
    for opt in options:
        row = existing.get(opt["code"])
        if row is None:
            session.add(Subsidiary(code=opt["code"], name=opt["name"], active=True))
            added += 1
            continue
        if row.name != opt["name"]:
            row.name = opt["name"]
            updated += 1
        if not row.active:
            row.active = True
            reactivated += 1
    for code, row in existing.items():
        if code not in live_codes and row.active:
            row.active = False
            deactivated += 1
    return {
        "synced": True,
        "field": subsidiary_field(),
        "total": len(options),
        "added": added,
        "updated": updated,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "changed": added + updated + reactivated + deactivated,
    }


def _status_set(env_var: str, default: str) -> set[str]:
    raw = os.getenv(env_var, default)
    return {s.strip() for s in raw.split(",") if s.strip()}


def normalize_status(name: str) -> str:
    """Map a Jira status name to pending | approved | rejected.

    The in-progress and resolved lifecycle statuses also count as "approved".
    A ticket only reaches them *after* a genuine approval — the portal drives
    those transitions once provisioning starts, and In Progress is unreachable
    without first passing the approved (Assigned) state. So the orchestrator's
    independent authority re-verification (§4) must still see authority as
    granted while it is applying, even though the portal has already moved the
    ticket past the approved state. What stays refused: Open (pending, not yet
    approved) and Rejected/Cancelled (approval denied or withdrawn).
    """
    name = (name or "").strip()
    approved = _status_set("JIRA_APPROVED_STATUSES", "Approved,Done")
    approved |= {inprogress_status(), resolved_status()}
    if name in approved:
        return "approved"
    if name in _status_set("JIRA_REJECTED_STATUSES", "Rejected,Cancelled"):
        return "rejected"
    return "pending"


def build_ticket_body(req: Request, estimate: dict, plan_preview: str) -> str:
    """Assemble the ticket text: configuration + cost + plan preview together."""
    is_decommission = req.request_type == "decommission"
    lines = [
        f"Request {req.reference} ({req.request_type})",
        f"Requester: {req.requester}",
        f"Project: {req.project_code}    Cost centre: {req.cost_centre_code}",
        f"Deployment target: {req.deployment_target}",
        f"Data classification: {req.data_classification}",
    ]
    if is_decommission and getattr(req, "source_reference", None):
        lines.append(f"Decommissioning provisioned request: {req.source_reference}")
    if req.environment_name:
        label = "Environment to remove" if is_decommission else "New environment"
        lines.append(f"{label}: {req.environment_name}")
    if getattr(req, "environment_tier", None):
        lines.append(f"Environment tier: {req.environment_tier}")
    if req.target_environment:
        lines.append(f"Target environment: {req.target_environment}")

    # Governance metadata (increment 6.1) — captured for create/add/resize.
    if not is_decommission and (req.business_justification or req.priority):
        lines.append("")
        lines.append("Request details:")
        lines.append(
            f"  Priority: {req.priority or '-'}    "
            f"Criticality: {req.business_criticality or '-'}"
        )
        if req.required_delivery_date:
            lines.append(f"  Required delivery date: {req.required_delivery_date}")
        owners = "    ".join(
            f"{label}: {value}"
            for label, value in (
                ("Application owner", req.application_owner),
                ("Business owner", req.business_owner),
                ("Technical owner", req.technical_owner),
                ("Environment owner", req.environment_owner),
            )
            if value
        )
        if owners:
            lines.append(f"  {owners}")
        if req.business_justification:
            lines.append(f"  Justification: {req.business_justification}")

    adv = getattr(req, "advanced_options", None) or {}
    shown = {k: v for k, v in adv.items() if v not in (None, "", "none", False)}
    if not is_decommission and shown:
        lines.append("")
        lines.append("Advanced options:")
        for k, v in shown.items():
            lines.append(f"  {k.replace('_', ' ').title()}: {'yes' if v is True else v}")

    lines.append("")
    lines.append("Technologies to decommission:" if is_decommission else "Components:")
    for comp in req.components:
        # The approver is signing off a machine, so show the machine. Detail
        # fields are optional, and a request that carries none reads exactly as
        # it always did.
        detail = []
        if comp.version:
            detail.append(f"v{comp.version}")
        if comp.vcpu:
            detail.append(f"{comp.vcpu} vCPU")
        if comp.memory_gb:
            detail.append(f"{comp.memory_gb} GB RAM")
        if comp.storage_gb:
            detail.append(f"{comp.storage_gb} GB disk")
        suffix = f" - {', '.join(detail)}" if detail else ""
        lines.append(f"  - {comp.technology_code} ({comp.size}){suffix}")
    if any(c.vcpu or c.memory_gb or c.storage_gb for c in req.components):
        lines.append("")
        lines.append("  Shapes above are the requester's explicit choices and are "
                     "what will be built.")

    totals = estimate.get("totals", {})
    currency = estimate.get("currency", "AED")
    lines.append("")
    lines.append(
        f"Estimated cost ({currency}): "
        f"one-time {totals.get('one_time', 0):.2f}, "
        f"monthly {totals.get('monthly', 0):.2f}, "
        f"annual {totals.get('annual', 0):.2f}"
    )

    # WHAT THE MONTHLY FIGURE IS MADE OF, and licence cost is why this is here.
    #
    # The ticket showed one number. SQL Server prices at 790.59 monthly, of
    # which 700.00 is LICENCE and 90.59 is the machine — and an approver reading
    # "790.59" could not tell those apart, so they were approving a licence
    # commitment without being shown one. The requester's own form has always
    # broken this out; the person actually granting the money had not.
    by_category = estimate.get("by_category") or {}
    shown = [(name, by_category.get(key) or 0.0) for name, key in (
        ("Compute", "compute"), ("Storage", "storage"), ("Licence", "licence"),
        ("Backup", "backup"), ("Monitoring", "monitoring"), ("Support", "support"))
        if (by_category.get(key) or 0.0) > 0]
    if shown:
        lines.append("")
        lines.append(f"  Monthly cost is made up of ({currency}):")
        for name, amount in shown:
            lines.append(f"    {name}: {amount:,.2f}")
        licence = by_category.get("licence") or 0.0
        if licence > 0:
            lines.append("")
            lines.append(
                f"    NOTE: {licence:,.2f} of this is SOFTWARE LICENCE, not "
                f"infrastructure. Approving this request commits to that "
                f"licence cost for as long as the environment exists.")

    # LICENCES BILLED IN ANOTHER CURRENCY cannot honestly be added to the total
    # above, so they are stated separately rather than silently omitted.
    for lic in estimate.get("external_licences") or []:
        rate = lic.get("licence_rate")
        cur = lic.get("licence_currency") or "?"
        lines.append("")
        lines.append(
            f"  PLUS a separate {cur} licence charge for "
            f"{lic.get('technology_name', 'a component')}"
            + (f": {rate} {cur}" if rate is not None else "")
            + " — billed by the publisher and NOT included in the total above.")
    lines.append("")
    lines.append("--- Plan preview ---")
    lines.append(plan_preview)
    return "\n".join(lines)


def _next_mock_key(session: Session) -> str:
    seq = (session.scalar(select(func.max(Approval.id))) or 0) + 1
    return f"INFRA-{1000 + seq}"


def _upload_attachment(jira_key: str, filename: str, content: bytes) -> None:
    """Attach a file to an issue. Best-effort: never raise (issue already exists)."""
    try:
        httpx.post(
            f"{_base_url()}/rest/api/2/issue/{jira_key}/attachments",
            headers={
                "Authorization": f"Bearer {os.getenv('JIRA_PAT', '')}",
                # Required by Jira to accept attachment uploads.
                "X-Atlassian-Token": "no-check",
            },
            files={"file": (filename, content, "application/pdf")},
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001 — attachment is a nice-to-have, not critical
        pass


def create_issue(
    session: Session,
    req: Request,
    body: str,
    attachment: tuple[str, bytes] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> Approval:
    """Create the Jira issue and return an Approval row (not yet added).

    `attachment` (filename, bytes) and any `attachments` (a list) are uploaded
    to the issue after creation (live mode only) — e.g. the costing PDF and the
    Excel cost sheet (6.4).
    """
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
    for att in [attachment, *(attachments or [])]:
        if att is not None:
            _upload_attachment(key, att[0], att[1])
    return Approval(
        jira_key=key,
        status="pending",
        ticket_url=f"{_base_url()}/browse/{key}",
        ticket_body=body,
    )


def inprogress_status() -> str:
    return os.getenv("JIRA_INPROGRESS_STATUS", "In Progress")


def resolved_status() -> str:
    return os.getenv("JIRA_RESOLVED_STATUS", "Resolved")


def resolve_fields() -> dict:
    """Fields required by the Resolve transition (e.g. Solution, Closure Reason),
    supplied as a JSON env value."""
    raw = os.getenv("JIRA_RESOLVE_FIELDS", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JiraError(f"JIRA_RESOLVE_FIELDS is not valid JSON: {exc}") from exc


def transition_issue(jira_key: str, target_status: str, fields: dict | None = None) -> str:
    """Move a Jira issue to the workflow status matching `target_status`.

    Finds the available transition whose destination (or name) matches, sets any
    `fields` the transition requires, and performs it. No-op in mock mode.
    Raises JiraError if unavailable/failed.
    """
    if jira_mode() != "live":
        return target_status

    base, h = _base_url(), _headers()
    try:
        resp = httpx.get(f"{base}/rest/api/2/issue/{jira_key}/transitions", headers=h, timeout=10.0)
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"could not read transitions for {jira_key}: {exc}") from exc

    target = target_status.strip().lower()
    match = next(
        (t for t in transitions
         if t.get("to", {}).get("name", "").strip().lower() == target
         or t.get("name", "").strip().lower() == target),
        None,
    )
    if match is None:
        available = [t.get("to", {}).get("name") for t in transitions]
        raise JiraError(f"no transition to '{target_status}' from here; available: {available}")

    payload: dict = {"transition": {"id": match["id"]}}
    if fields:
        payload["fields"] = fields
    try:
        r = httpx.post(
            f"{base}/rest/api/2/issue/{jira_key}/transitions",
            json=payload, headers=h, timeout=10.0,
        )
        if r.status_code >= 300:
            raise JiraError(f"transition to '{target_status}' failed {r.status_code}: {r.text}")
    except JiraError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise JiraError(f"transition to '{target_status}' failed: {exc}") from exc
    return target_status


def add_comment(jira_key: str, body: str) -> None:
    """Add a comment to an issue. Best-effort: never raises (informational)."""
    if jira_mode() != "live":
        return
    try:
        httpx.post(
            f"{_base_url()}/rest/api/2/issue/{jira_key}/comment",
            json={"body": body},
            headers=_headers(),
            timeout=10.0,
        )
    except Exception:  # noqa: BLE001 — a comment is a nice-to-have, not critical
        pass


def get_approval_author(jira_key: str) -> dict | None:
    """Who approved the request in Jira (F-GOV-08): the author of the last
    transition into an approved status. Returns {name, email} or None if it
    can't be determined (live only; mock has no changelog author) — the caller
    fails open on None rather than blocking provisioning on a read error.
    """
    if jira_mode() != "live":
        return None
    try:
        response = httpx.get(
            f"{_base_url()}/rest/api/2/issue/{jira_key}",
            params={"expand": "changelog", "fields": "status"},
            headers=_headers(),
            timeout=8.0,
        )
        response.raise_for_status()
        histories = response.json().get("changelog", {}).get("histories", [])
    except Exception:  # noqa: BLE001 — unknown author; caller fails open
        return None
    approved = _status_set("JIRA_APPROVED_STATUSES", "Approved,Done")
    author = None
    for history in histories:  # chronological — keep the last approval transition
        for item in history.get("items", []):
            if item.get("field") == "status" and (item.get("toString") or "").strip() in approved:
                author = history.get("author") or {}
    if not author:
        return None
    return {
        "name": author.get("name") or author.get("key"),
        "email": (author.get("emailAddress") or "").strip() or None,
    }


def get_approvers(jira_key: str) -> list[dict]:
    """The distinct people who approved the request in Jira (F-GOV-06 quorum).

    Prefers the Jira Service Management approval API (the authoritative
    multi-approver source: each approval lists its approvers and their decision),
    falling back to the changelog authors of transitions into an approved status.
    Returns a list of {name, email} (possibly empty). Live only; on a read error
    it returns [] and the caller (quorum) fails closed — it holds rather than
    provisions, and re-checks on the next poll.
    """
    if jira_mode() != "live":
        return []
    # 1) JSM approval API — each approval carries its approvers + their decision.
    try:
        response = httpx.get(
            f"{_base_url()}/rest/servicedeskapi/request/{jira_key}/approval",
            headers=_headers(),
            timeout=8.0,
        )
        if response.status_code == 200:
            people = []
            for approval in response.json().get("values", []):
                for a in approval.get("approvers", []):
                    if (a.get("approverDecision") or "").lower() == "approved":
                        u = a.get("approver") or {}
                        people.append({
                            "name": u.get("name") or u.get("key") or u.get("accountId"),
                            "email": (u.get("emailAddress") or "").strip() or None,
                        })
            if people:
                return people
    except Exception:  # noqa: BLE001 — fall back to the changelog
        pass
    # 2) Changelog fallback — every author of a transition into an approved status.
    try:
        response = httpx.get(
            f"{_base_url()}/rest/api/2/issue/{jira_key}",
            params={"expand": "changelog", "fields": "status"},
            headers=_headers(),
            timeout=8.0,
        )
        response.raise_for_status()
        histories = response.json().get("changelog", {}).get("histories", [])
    except Exception:  # noqa: BLE001 — unknown; caller fails closed
        return []
    approved = _status_set("JIRA_APPROVED_STATUSES", "Approved,Done")
    people = []
    for history in histories:
        for item in history.get("items", []):
            if item.get("field") == "status" and (item.get("toString") or "").strip() in approved:
                author = history.get("author") or {}
                people.append({
                    "name": author.get("name") or author.get("key"),
                    "email": (author.get("emailAddress") or "").strip() or None,
                })
    return people


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
