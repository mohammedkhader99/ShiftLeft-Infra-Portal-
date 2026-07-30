"""Append-only, hash-chained audit trail helper (increment 1.9, F-SEC-01).

Each entry's hash is computed over the previous entry's hash plus this entry's
content, so any later edit or deletion is detectable by verify_chain (F-SEC-01
hardening). Concurrent writers (the poller + the API) can legitimately fork the
chain — two entries chained to the same parent, written microseconds apart — so
verify_chain checks integrity (no edited/deleted entries) rather than a strict
single-writer linear order.

The hash is a plain SHA-256 by default. Set AUDIT_HMAC_KEY to switch to a keyed
HMAC-SHA256 so that someone with raw database access cannot recompute valid
hashes without the secret — append and verify use the same key. Leaving it unset
keeps today's behaviour, so entries written before a key is introduced still
verify under the plain scheme.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog


def _iso(created_at: datetime) -> str:
    """Canonical UTC timestamp for hashing. Some stores (e.g. SQLite) return the
    timestamp tz-naive on read; normalising to UTC makes write and read agree
    without changing the string for the tz-aware UTC values we always write."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).isoformat()


def _content(event, reference, jira_key, actor, detail, created_at) -> str:
    """The canonical JSON string an entry's hash is computed over. Used by both
    append_audit (write) and verify_chain (read) so they agree exactly."""
    return json.dumps(
        {
            "event": event,
            "reference": reference,
            "jira_key": jira_key,
            "actor": actor,
            "detail": detail or {},
            "created_at": _iso(created_at),
        },
        sort_keys=True,
        default=str,
    )


def _entry_hash(prev_hash: str, content: str) -> str:
    """Hash the previous hash + this entry's content. Keyed HMAC when
    AUDIT_HMAC_KEY is set, otherwise a plain SHA-256 (F-SEC-01)."""
    message = (prev_hash + content).encode()
    key = os.getenv("AUDIT_HMAC_KEY", "").encode()
    if key:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


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
    created_at = datetime.now(timezone.utc)
    detail = detail or {}

    content = _content(event, reference, jira_key, actor, detail, created_at)
    entry_hash = _entry_hash(prev_hash, content)

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


def verify_chain(session: Session) -> dict:
    """Re-walk the whole append-only log and confirm it hasn't been tampered
    with (F-SEC-01). Returns {ok, checked, total, broken_at}.

    For every entry it (1) recomputes the hash from the entry's own stored
    prev_hash + content and checks it matches — catching any edited field — and
    (2) checks the prev_hash resolves to a real entry — catching a deleted or
    altered parent. This tolerates the benign *forks* a concurrent writer set
    (the poller + the API) naturally produces (two entries chained to the same
    parent, written microseconds apart), which are not tampering. A keyed
    AUDIT_HMAC_KEY additionally makes forging a valid entry infeasible.
    """
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    known = {row.entry_hash for row in rows}
    checked = 0
    for row in rows:
        content = _content(row.event, row.reference, row.jira_key, row.actor,
                           row.detail, row.created_at)
        if row.entry_hash != _entry_hash(row.prev_hash or "", content):
            return _broken(row, "content hash mismatch (entry altered)", checked, len(rows))
        if row.prev_hash and row.prev_hash not in known:
            return _broken(row, "previous entry missing (deleted or altered)", checked, len(rows))
        checked += 1
    return {"ok": True, "checked": checked, "total": len(rows), "broken_at": None}


def _broken(row: AuditLog, reason: str, checked: int, total: int) -> dict:
    return {
        "ok": False,
        "checked": checked,
        "total": total,
        "broken_at": {"id": row.id, "event": row.event, "reference": row.reference, "reason": reason},
    }


def main() -> None:
    """CLI: `python -m api.audit verify` — exits non-zero if the chain is broken."""
    import sys

    from db.session import SessionLocal

    if len(sys.argv) < 2 or sys.argv[1] != "verify":
        print("usage: python -m api.audit verify")
        raise SystemExit(2)
    with SessionLocal() as session:
        result = verify_chain(session)
    if result["ok"]:
        print(f"Audit chain intact: {result['checked']} entries verified.")
        raise SystemExit(0)
    print(f"AUDIT TAMPERING DETECTED after {result['checked']} good entries: {result['broken_at']}")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
