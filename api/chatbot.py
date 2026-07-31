"""ChatOps approvals bot (F-INT-08).

Lets an approver query and act on requests from chat (Slack/Teams) or the
in-portal Assistant console: `pending`, `status <ref>`, `approve <ref> [note]`,
`reject <ref> [note]`.

Authority boundary (ARCHITECTURE.md §4, hard rule): the bot **holds no
authority**. Every command runs as the mapped human's identity and roles.
Approve/reject **record that person's decision in Jira** (the system of record)
by transitioning the ticket + an attributed comment; the normal re-verified
Jira → poller → orchestrator flow then proceeds. The bot never grants approval
portal-side in live mode, never provisions, enforces RBAC (approver/
platform_admin) and segregation of duties (you can't decide your own request),
and audits every state-changing action.
"""

import hashlib
import hmac
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from api import roles as roles_mod
from api.audit import append_audit
from api.jira import JiraError, add_comment, jira_mode, transition_issue
from db.models import Request

HELP = (
    "Approvals bot — commands:\n"
    "  pending                 — requests awaiting approval\n"
    "  status <REQ-ID>         — one request's status\n"
    "  approve <REQ-ID> [note] — approve (records your decision in Jira)\n"
    "  reject  <REQ-ID> [note] — reject\n"
    "  help                    — this message"
)

# Roles allowed to approve/reject from chat (Jira still holds the authority).
DECIDE_ROLES = {roles_mod.APPROVER, roles_mod.PLATFORM_ADMIN}
# Statuses a request sits in while awaiting approval.
AWAITING = ("submitted", "planned")


def _reply(text: str, ok: bool = True, **extra) -> dict:
    return {"ok": ok, "response": text, **extra}


def _approve_status() -> str:
    return (os.getenv("CHATOPS_APPROVE_STATUS", "").strip()
            or os.getenv("JIRA_APPROVED_STATUSES", "Approved,Done").split(",")[0].strip())


def _reject_status() -> str:
    return (os.getenv("CHATOPS_REJECT_STATUS", "").strip()
            or os.getenv("JIRA_REJECTED_STATUSES", "Rejected,Cancelled").split(",")[0].strip())


def handle_command(text: str, identity: str, session: Session) -> dict:
    """Run one chat command as `identity` (a portal email). Returns
    {ok, response, ...}. RBAC + SoD enforced here; approve/reject record the
    decision in Jira and audit it."""
    text = (text or "").strip()
    if not text:
        return _reply(HELP, ok=False)
    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    if cmd in ("help", "?", "commands"):
        return _reply(HELP)
    if cmd in ("pending", "queue"):
        return _pending(session, identity)
    if cmd in ("status", "show"):
        if not args:
            return _reply("Usage: status <REQ-ID>", ok=False)
        return _status(session, args[0].upper())
    if cmd in ("approve", "reject"):
        if not args:
            return _reply(f"Usage: {cmd} <REQ-ID> [note]", ok=False)
        return _decide(session, cmd, args[0].upper(), " ".join(args[1:]).strip(), identity)
    return _reply(f"Unknown command '{cmd}'.\n\n{HELP}", ok=False)


def _status(session: Session, reference: str) -> dict:
    req = session.scalar(select(Request).where(Request.reference == reference))
    if req is None:
        return _reply(f"{reference} not found.", ok=False)
    lines = [f"{req.reference} — {req.status}"]
    env = req.environment_name or req.target_environment
    if env:
        lines.append(f"  environment: {env}")
    if req.estimate:
        lines.append(f"  cost: {float(req.estimate.monthly):.0f} AED/mo")
    if req.approval:
        lines.append(f"  Jira: {req.approval.jira_key}")
    if req.status_detail:
        lines.append(f"  note: {req.status_detail}")
    return _reply("\n".join(lines), reference=reference, status=req.status)


def _pending(session: Session, identity: str) -> dict:
    rows = session.scalars(
        select(Request).where(Request.status.in_(AWAITING)).order_by(Request.id)
    ).all()
    pending = [r for r in rows if r.approval is not None]
    if not pending:
        return _reply("No requests are awaiting approval. 🎉", count=0)
    lines = [f"{len(pending)} request(s) awaiting approval:"]
    for r in pending[:20]:
        env = r.environment_name or r.target_environment or ""
        own = "  (yours)" if r.requester == identity else ""
        lines.append(f"  {r.reference} — {env} · {r.approval.jira_key}{own}")
    return _reply("\n".join(lines), count=len(pending))


def _decide(session: Session, action: str, reference: str, note: str, identity: str) -> dict:
    roles = roles_mod.resolve_roles(identity)
    if not (roles & DECIDE_ROLES):
        have = ", ".join(sorted(roles)) or "none"
        return _reply(f"Your role ({have}) can't {action} requests — you need the approver role.",
                      ok=False)

    req = session.scalar(select(Request).where(Request.reference == reference))
    if req is None:
        return _reply(f"{reference} not found.", ok=False)
    if req.approval is None:
        return _reply(f"{reference} has no Jira ticket yet — it isn't awaiting approval.", ok=False)

    # Segregation of duties (F-IAM-03): you can't decide your own request.
    if identity and identity == req.requester:
        append_audit(session, "sod.blocked", reference=reference, actor=identity,
                     detail={"action": f"chatops.{action}"})
        session.commit()
        return _reply(f"Segregation of duties: you raised {reference}, so you can't {action} it. "
                      "A different approver must act.", ok=False)

    jira_key = req.approval.jira_key
    approved = action == "approve"
    if jira_mode() == "live":
        # Record the human's decision IN JIRA — the system of record.
        target = _approve_status() if approved else _reject_status()
        try:
            transition_issue(jira_key, target)
            add_comment(
                jira_key,
                f"{'✅ Approved' if approved else '⛔ Rejected'} by {identity} via the approvals chatbot"
                + (f": {note}" if note else "."),
            )
        except JiraError as exc:
            return _reply(f"Couldn't {action} {reference} in Jira: {exc}", ok=False)
    req.approval.status = "approved" if approved else "rejected"

    append_audit(session, "chatops.approved" if approved else "chatops.rejected",
                 reference=reference, jira_key=jira_key, actor=identity,
                 detail={"note": note} if note else {})
    session.commit()

    if approved:
        tail = "Provisioning will proceed automatically once re-verified." if jira_mode() == "live" \
            else "Recorded (mock mode)."
        return _reply(f"✅ {reference} approved — recorded in Jira ({jira_key}). {tail}",
                      reference=reference, action="approved")
    return _reply(f"⛔ {reference} rejected — recorded in Jira ({jira_key}). It won't be provisioned.",
                  reference=reference, action="rejected")


# --- Slack adapter (live) -----------------------------------------------------

def slack_enabled() -> bool:
    return bool(os.getenv("SLACK_SIGNING_SECRET", "").strip())


def verify_slack(raw_body: bytes, headers) -> bool:
    """Verify a Slack slash-command request signature (HMAC-SHA256 over
    `v0:timestamp:body`), with a 5-minute replay guard. Rejects if the signing
    secret isn't configured."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        return False
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    basestring = f"v0:{ts}:{raw_body.decode('utf-8', 'ignore')}".encode()
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _slack_map() -> dict:
    """Slack user_id / user_name -> portal email, from CHATOPS_SLACK_MAP (JSON)."""
    import json
    raw = os.getenv("CHATOPS_SLACK_MAP", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def slack_identity(user_id: str, user_name: str) -> str | None:
    """Resolve a Slack user to a portal email via CHATOPS_SLACK_MAP (keyed by
    user_id or user_name). None if unmapped — the bot refuses rather than guess."""
    mapping = _slack_map()
    return mapping.get(user_id) or mapping.get(user_name)
