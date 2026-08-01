"""Vault credential delivery for just-in-time access (F-INT-05 / F-IAM-07).

Mints TIME-BOUND credentials and returns a one-time vault LINK. The secret is
delivered directly by the vault to the grantee — the portal never holds, stores,
or logs it (P2; ARCHITECTURE.md §12: "no standing credentials; secrets from a
vault; delivered via time-bound vault links, never tickets/email"). The portal
keeps only a handle + expiry, for revoke and audit.

Modes (VAULT_MODE):
  - mock (default): a clearly-labelled PLACEHOLDER link — not a real credential.
    For demos + tests; nothing sensitive is issued.
  - live: a real vault (HashiCorp Vault / OCI Vault dynamic secrets) — an
    extension point needing the vault's address + auth (supplied via env / a
    vault, never committed). Raises VaultUnavailable until implemented.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone


class VaultUnavailable(RuntimeError):
    """The live vault isn't configured (no address/auth, or not implemented)."""


def mode() -> str:
    return os.getenv("VAULT_MODE", "mock").strip().lower()


def issue_credential(reference: str, grantee: str, scope: str, ttl_hours: int) -> dict:
    """Mint a time-bound credential for `grantee` to access `reference` at `scope`.

    Returns {handle, link, expires_at, ...}. The link is one-time + time-bound;
    the portal never sees the secret itself — only stores the handle + expiry.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    if mode() == "live":
        return _issue_live(reference, grantee, scope, ttl_hours, expires_at)
    handle = "mock-" + secrets.token_hex(8)
    return {
        "handle": handle,
        # A PLACEHOLDER — not a real secret. A live vault returns a one-time link
        # the grantee opens to retrieve a short-lived credential directly.
        "link": f"https://vault.mock.local/one-time/{handle}",
        "expires_at": expires_at.isoformat(),
        "mock": True,
        "note": "Mock credential — not a real secret. Set VAULT_MODE=live and wire a vault.",
    }


def revoke(handle: str | None) -> dict:
    """Revoke a previously issued credential by its handle."""
    if mode() == "live":
        return _revoke_live(handle)
    return {"revoked": True, "handle": handle, "mock": True}


def _issue_live(reference, grantee, scope, ttl_hours, expires_at):
    # Extension point. A real implementation authenticates to the vault (address +
    # auth via env / a vault, never committed) and mints a time-bound, one-time
    # credential — e.g. HashiCorp Vault dynamic secrets or OCI Vault — returning a
    # one-time link. It never returns the raw secret to the portal.
    raise VaultUnavailable(
        "Live vault not configured. Provide the vault address + auth and implement "
        "api/vault._issue_live (e.g. HashiCorp Vault dynamic secrets / OCI Vault) to "
        "mint a time-bound one-time link.")


def _revoke_live(handle):
    raise VaultUnavailable("Live vault not configured; cannot revoke.")
