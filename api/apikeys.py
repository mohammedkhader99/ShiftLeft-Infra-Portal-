"""API key credentials for programmatic access (F-INT-01, E1).

Keys are opaque random strings; only their SHA-256 hash is stored. A key
authenticates as the `identity` it was issued for, so it inherits that user's
roles (resolved the usual way) and can never exceed them.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ApiKey


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    """A fresh opaque key. Shown once at creation, never stored in plaintext."""
    return "sk-infra-" + secrets.token_urlsafe(32)


def resolve_api_key(session: Session, key: str) -> str | None:
    """The identity an active API key acts as, or None for an unknown/revoked key.
    Stamps last_used_at when it matches."""
    if not key:
        return None
    row = session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_key(key), ApiKey.active.is_(True))
    )
    if row is None:
        return None
    row.last_used_at = datetime.now(timezone.utc)
    session.commit()
    return row.identity
