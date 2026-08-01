"""Outbound webhooks (F-INT-10): deliver lifecycle events to admin-configured
endpoints, HMAC-signed, at-least-once with bounded retries.

Reads the append-only audit log as the event source (decoupled — nothing in the
request path changes), like the activity feed. Opt-in (WEBHOOKS_ENABLED, default
off). The payload is event METADATA only — sensitive keys are stripped, so no
credential / link / secret ever leaves the portal (P2; ARCHITECTURE.md §12).
"""

import json
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.audit import append_audit
from api.eventstream import LIFECYCLE_EVENTS
from common.signing import sign
from db.models import AuditLog, WebhookDelivery, WebhookState, WebhookSubscription

MAX_ATTEMPTS = 5
_BATCH = 200
_TIMEOUT = 5.0
# Never forward these detail keys (defence in depth — lifecycle details don't
# normally carry them, but a rename must never leak a secret).
_SENSITIVE_KEYS = {"link", "secret", "token", "password", "credential", "handle",
                   "private_key", "key", "signature"}


def enabled() -> bool:
    return os.getenv("WEBHOOKS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _sanitized_detail(detail: dict | None) -> dict:
    return {k: v for k, v in (detail or {}).items() if k.lower() not in _SENSITIVE_KEYS}


def event_payload(row: AuditLog) -> dict:
    return {
        "id": row.id, "event": row.event, "reference": row.reference, "actor": row.actor,
        "detail": _sanitized_detail(row.detail),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def matches(sub: WebhookSubscription, event: str) -> bool:
    """A subscription with no filter (or ['*']) gets every lifecycle event."""
    events = sub.events or []
    return not events or "*" in events or event in events


def deliver(sub: WebhookSubscription, payload: dict) -> tuple[bool, str]:
    """POST the payload to the endpoint, HMAC-signed. Returns (ok, error)."""
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {"Content-Type": "application/json",
               "X-Signature": sign(sub.secret, body),
               "X-Webhook-Event": str(payload.get("event", ""))}
    try:
        resp = httpx.post(sub.url, content=body, headers=headers, timeout=_TIMEOUT)
        if 200 <= resp.status_code < 300:
            return True, ""
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _cursor(session: Session) -> WebhookState:
    st = session.get(WebhookState, 1)
    if st is None:
        # Start at the current tip so subscriptions don't backfill all of history.
        st = WebhookState(id=1, last_event_id=session.scalar(select(func.max(AuditLog.id))) or 0)
        session.add(st)
    return st


def _payload_for(session: Session, d: WebhookDelivery) -> dict:
    row = session.get(AuditLog, d.event_id)
    return event_payload(row) if row else {"id": d.event_id, "event": d.event}


def process(session: Session) -> None:
    """Enqueue new lifecycle events for matching subscriptions, then attempt
    pending deliveries (retries across sweeps, bounded by MAX_ATTEMPTS). No-op when
    disabled or when there are no active subscriptions."""
    if not enabled():
        return
    subs = session.scalars(
        select(WebhookSubscription).where(WebhookSubscription.active.is_(True))
    ).all()
    if not subs:
        return

    st = _cursor(session)
    new_events = session.scalars(
        select(AuditLog).where(AuditLog.id > st.last_event_id, AuditLog.event.in_(LIFECYCLE_EVENTS))
        .order_by(AuditLog.id).limit(_BATCH)
    ).all()
    for ev in new_events:
        for sub in subs:
            if matches(sub, ev.event):
                session.add(WebhookDelivery(subscription_id=sub.id, event_id=ev.id, event=ev.event))
        st.last_event_id = ev.id

    sub_by_id = {s.id: s for s in subs}
    pending = session.scalars(
        select(WebhookDelivery).where(WebhookDelivery.status == "pending").limit(_BATCH)
    ).all()
    for d in pending:
        sub = sub_by_id.get(d.subscription_id) or session.get(WebhookSubscription, d.subscription_id)
        if sub is None or not sub.active:
            d.status = "failed"
            d.last_error = "subscription removed/inactive"
            continue
        ok, err = deliver(sub, _payload_for(session, d))
        d.attempts += 1
        if ok:
            d.status = "delivered"
            d.delivered_at = datetime.now(timezone.utc)
            append_audit(session, "webhook.delivered", reference=None,
                         detail={"subscription": sub.id, "event": d.event, "url": sub.url})
        else:
            d.last_error = err
            if d.attempts >= MAX_ATTEMPTS:
                d.status = "failed"
                append_audit(session, "webhook.failed", reference=None,
                             detail={"subscription": sub.id, "event": d.event, "url": sub.url,
                                     "attempts": d.attempts, "error": err})
    session.commit()
