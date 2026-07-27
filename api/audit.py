"""Append-only, hash-chained audit trail helper (increment 1.9, F-SEC-01).

Each entry's hash is computed over the previous entry's hash plus this entry's
content, so any later edit or deletion breaks the chain and is detectable.
"""

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog


def append_audit(
    session: Session,
    event: str,
    *,
    reference: str | None = None,
    jira_key: str | None = None,
    actor: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """Add one audit entry, chained to the previous. Caller commits."""
    last = session.scalar(select(AuditLog).order_by(AuditLog.id.desc()))
    prev_hash = last.entry_hash if last else ""
    created_at = datetime.utcnow()
    detail = detail or {}

    content = json.dumps(
        {
            "event": event,
            "reference": reference,
            "jira_key": jira_key,
            "actor": actor,
            "detail": detail,
            "created_at": created_at.isoformat(),
        },
        sort_keys=True,
        default=str,
    )
    entry_hash = hashlib.sha256((prev_hash + content).encode()).hexdigest()

    row = AuditLog(
        event=event,
        reference=reference,
        jira_key=jira_key,
        actor=actor,
        detail=detail,
        prev_hash=prev_hash or None,
        entry_hash=entry_hash,
        created_at=created_at,
    )
    session.add(row)
    return row
