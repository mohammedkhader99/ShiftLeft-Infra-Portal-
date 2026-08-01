"""Lifecycle event stream (F-INT-02).

Publishes the portal's request/environment lifecycle events so other systems can
subscribe. The tamper-evident audit log (F-SEC-01) is already the append-only,
monotonic-id record of every lifecycle transition, so this is a curated,
read-only VIEW over it — no separate event store, no external broker.

Consumers tail the monotonic `id` cursor (`?since=`) for gap-free, at-least-once
delivery, or subscribe to the SSE stream for real-time push. It exposes only
events already recorded; it changes nothing.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog

# The curated set published by default: request + environment state changes and
# the governance / FinOps signals other systems care about. Internal noise
# (poll.error, *.sweep.error, approval.checked, apikey.*, ai.*, *.handoff,
# budget/quota .set/.deleted, subsidiaries.synced) is excluded — a consumer that
# wants everything can pass ?all=true, or a specific set via ?types=.
LIFECYCLE_EVENTS = {
    # request provisioning lifecycle
    "provisioning.started", "provisioned", "plan.failed", "apply.failed",
    "approval.approved", "approval.rejected",
    "orchestrator.refused", "orchestrator.unreachable",
    "decommissioned", "destroyed", "destroy.failed", "decommission.source_missing",
    # environment lifecycle
    "ttl.expiring", "ttl.expired", "ttl.renewed",
    "ttl.decommissioned", "ttl.decommission.failed",
    "ownership.transferred", "ownership.orphaned", "drift.detected",
    # day-2 lifecycle operations (E3)
    "refresh.performed", "restore.performed", "backup.created",
    "resource.stopped", "resource.started", "access.granted", "access.revoked",
    # governance signals
    "policy.waived", "waiver.granted", "sla.breached", "quorum.met",
    "quorum.blocked", "four_eyes.blocked", "sod.blocked", "change_window.held",
    "scan.findings",
    # FinOps signals
    "budget.warning", "budget.blocked", "quota.warning", "quota.blocked",
    "variance.alert", "actual.recorded",
}

MAX_LIMIT = 500


def _event_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "type": row.event,
        "reference": row.reference,
        "actor": row.actor,
        "detail": row.detail or {},
        "trace_id": row.trace_id,  # F-OPS-04 correlation id
        "at": row.created_at.isoformat() if row.created_at else None,
    }


def lifecycle_events(
    session: Session,
    since: int = 0,
    limit: int = 100,
    reference: str | None = None,
    types: list[str] | None = None,
    include_all: bool = False,
    tail: int | None = None,
) -> tuple[list[dict], int]:
    """Return (events, cursor) from the audit log.

    - `since`: only events with id > since (incremental tailing).
    - `tail`: instead return the most recent N events (for an initial load).
    - `reference`: scope to one request. `types`: explicit event names.
    - `include_all`: publish every audit event, not just the lifecycle subset.
    The cursor is the max id returned (or `since` if nothing new).
    """
    limit = max(1, min(int(limit or 100), MAX_LIMIT))
    conditions = []
    if reference:
        conditions.append(AuditLog.reference == reference)
    if types:
        wanted = {t.strip() for t in types if t and t.strip()}
        if wanted:
            conditions.append(AuditLog.event.in_(wanted))
    elif not include_all:
        conditions.append(AuditLog.event.in_(LIFECYCLE_EVENTS))

    if tail:
        # Most recent `tail` events, returned oldest-first for a stable feed.
        q = select(AuditLog).where(*conditions).order_by(AuditLog.id.desc()).limit(
            max(1, min(int(tail), MAX_LIMIT))
        )
        rows = list(reversed(session.scalars(q).all()))
    else:
        q = (
            select(AuditLog)
            .where(AuditLog.id > int(since or 0), *conditions)
            .order_by(AuditLog.id)
            .limit(limit)
        )
        rows = session.scalars(q).all()

    events = [_event_dict(r) for r in rows]
    cursor = events[-1]["id"] if events else int(since or 0)
    return events, cursor


def sse_frame(event: dict) -> str:
    """One Server-Sent Events frame. `id:` carries the cursor so a client resumes
    via Last-Event-ID; `event:` is the lifecycle type; `data:` is the JSON."""
    return (
        f"id: {event['id']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, default=str)}\n\n"
    )
