"""Shared HMAC-SHA256 webhook signing (increment 1.9).

The API signs the approval->orchestrator handoff; the orchestrator verifies it.
The signature proves authenticity (the message really came from the API), NOT
authority — the orchestrator still re-verifies approval and policy itself
(ARCHITECTURE.md §4). The secret is shared, injected from the environment, never
committed.
"""

import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 of body under secret."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time check that signature matches body under secret."""
    expected = sign(secret, body)
    return hmac.compare_digest(expected, signature or "")
