"""Single-active leader election for the background poller (F-OPS-01).

The API is horizontally scalable, but its poller must run in exactly ONE replica
at a time — otherwise N replicas would advance the same request and fire the same
side effects N times, breaking the invariant that nothing gets provisioned or
changed twice.

This is a small DB-backed lease: a holder wins a time-bound lease and renews it
each tick; if it dies, the lease expires and another replica takes over. The
conditional UPDATE is atomic (row-locked), so only one holder wins per expiry
window. Works on Postgres (prod) and SQLite (tests) — no Redis/ZooKeeper.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import LeaderLease

# A stable-per-process identity. Includes the hostname (the container/replica) so
# audited leadership changes name a real instance.
INSTANCE_ID = f"{(os.getenv('HOSTNAME') or 'api')[:32]}-{uuid.uuid4().hex[:8]}"


def ttl_seconds() -> int:
    """How long a lease stays valid without renewal (default 60s, floor 5s)."""
    try:
        return max(5, int(os.getenv("LEADER_LEASE_TTL_SECONDS", "60")))
    except ValueError:
        return 60


def try_acquire(session: Session, name: str = "poller", holder: str = INSTANCE_ID,
                now: datetime | None = None) -> bool:
    """Become or renew the leader for `name`. Returns True iff `holder` owns a
    valid lease after the call. Atomic — only one holder wins per expiry window."""
    now = now or datetime.now(timezone.utc)
    new_expiry = now + timedelta(seconds=ttl_seconds())

    # Take or renew: win if we already hold it, or the current lease has expired.
    # acquired_at only resets on a genuine takeover (the holder changes).
    result = session.execute(
        update(LeaderLease)
        .where(LeaderLease.name == name)
        .where((LeaderLease.holder == holder) | (LeaderLease.expires_at <= now))
        .values(
            acquired_at=case((LeaderLease.holder == holder, LeaderLease.acquired_at), else_=now),
            holder=holder,
            heartbeat_at=now,
            expires_at=new_expiry,
        )
    )
    if result.rowcount:
        session.commit()
        return True

    # No row yet? Try to insert the first lease (racing inserts resolve on the PK).
    if session.scalar(select(LeaderLease).where(LeaderLease.name == name)) is None:
        session.add(LeaderLease(name=name, holder=holder, acquired_at=now,
                                heartbeat_at=now, expires_at=new_expiry))
        try:
            session.commit()
            return True
        except IntegrityError:
            session.rollback()

    # A valid lease is held by someone else.
    session.rollback()
    return False


def release(session: Session, name: str = "poller", holder: str = INSTANCE_ID) -> None:
    """Give up our lease immediately (on graceful shutdown) so a standby can take
    over without waiting out the TTL. No-op if we don't hold it."""
    session.execute(
        update(LeaderLease)
        .where(LeaderLease.name == name, LeaderLease.holder == holder)
        .values(expires_at=datetime.now(timezone.utc))
    )
    session.commit()
