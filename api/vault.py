"""Vault credential delivery for just-in-time access (F-INT-05 / F-IAM-07).

Mints TIME-BOUND credentials and returns a one-time vault LINK. The secret is
delivered directly by the vault to the grantee — the portal never holds, stores,
or logs it (P2; ARCHITECTURE.md §12: "no standing credentials; secrets from a
vault; delivered via time-bound vault links, never tickets/email"). The portal
keeps only a handle + expiry, for revoke and audit.

Modes (VAULT_MODE):
  - mock (default): a clearly-labelled PLACEHOLDER link — not a real credential.
    For demos + tests; nothing sensitive is issued.
  - live: HashiCorp Vault.

How the live path keeps the secret away from the portal
-------------------------------------------------------
It uses Vault's **response wrapping**. The portal asks Vault to read a path *on
the grantee's behalf* with an `X-Vault-Wrap-TTL` header. Vault does not return
the credential — it stores the response and returns a **single-use wrapping
token**. The portal passes that token to the grantee, who unwraps it once to get
the credential directly from Vault.

So the portal only ever sees a token that:
  * it cannot use to read the secret without consuming it (the grantee would then
    find the link already used — tamper-evident), and
  * expires on its own.

The read path is templated, which makes the adapter engine-agnostic:
  * `database/creds/<role>` — dynamic, per-grant PostgreSQL logins with a real
    lease the portal can revoke. This is what a granted database environment
    should use.
  * `kv/data/<path>` — a static shared secret, still delivered one-time.

Configuration (all via env / a vault — never committed):
  VAULT_ADDR              e.g. https://vault.internal:8200
  VAULT_TOKEN             the portal's own token (least privilege: read + wrap on
                          the creds paths, and revoke on its own leases)
  VAULT_CREDS_PATH        read-path template. Placeholders: {scope}, {reference},
                          {grantee}. Default: database/creds/{scope}
  VAULT_NAMESPACE         optional (Vault Enterprise)
  VAULT_UI_ADDR           optional public base URL for the unwrap link, when the
                          grantee reaches Vault on a different address than the
                          portal does.
  VAULT_VERIFY            "false" to skip TLS verification (dev only).
"""

import os
import secrets
from datetime import datetime, timedelta, timezone


class VaultUnavailable(RuntimeError):
    """The live vault isn't configured or the call failed."""


def mode() -> str:
    return os.getenv("VAULT_MODE", "mock").strip().lower()


def is_live() -> bool:
    return mode() == "live"


def issue_credential(reference: str, grantee: str, scope: str, ttl_hours: int) -> dict:
    """Mint a time-bound credential for `grantee` to access `reference` at `scope`.

    Returns {handle, link, expires_at, ...}. The link is one-time + time-bound;
    the portal never sees the secret itself — only stores the handle + expiry.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    if is_live():
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
    """Revoke a previously issued credential by its handle (the Vault lease id)."""
    if is_live():
        return _revoke_live(handle)
    return {"revoked": True, "handle": handle, "mock": True}


# --- HashiCorp Vault (live) ---------------------------------------------------

def _config() -> tuple[str, str, str | None]:
    """(addr, token, namespace) or VaultUnavailable with a clear message."""
    addr = (os.getenv("VAULT_ADDR") or "").strip().rstrip("/")
    token = (os.getenv("VAULT_TOKEN") or "").strip()
    missing = [n for n, v in (("VAULT_ADDR", addr), ("VAULT_TOKEN", token)) if not v]
    if missing:
        raise VaultUnavailable(
            "Live vault is not configured: set " + ", ".join(missing)
            + " (via .env or a vault, never in code)."
        )
    return addr, token, (os.getenv("VAULT_NAMESPACE") or "").strip() or None


def _headers(token: str, namespace: str | None, wrap_ttl: str | None = None) -> dict:
    headers = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    if wrap_ttl:
        # The header that makes Vault wrap the response instead of returning it.
        headers["X-Vault-Wrap-TTL"] = wrap_ttl
    return headers


def _verify() -> bool:
    return (os.getenv("VAULT_VERIFY", "true").strip().lower()
            not in ("0", "false", "no", "off"))


def creds_path(reference: str, grantee: str, scope: str) -> str:
    """The Vault read path for this grant. Templated so the same adapter serves
    dynamic database credentials and static kv secrets."""
    template = (os.getenv("VAULT_CREDS_PATH") or "database/creds/{scope}").strip()
    return template.format(scope=scope or "", reference=reference or "", grantee=grantee or "")


def _unwrap_link(addr: str, wrapping_token: str) -> str:
    """The one-time link the grantee opens. Points at Vault's own unwrap UI, so the
    secret travels from Vault to the grantee — never through the portal."""
    base = (os.getenv("VAULT_UI_ADDR") or addr).strip().rstrip("/")
    return f"{base}/ui/vault/tools/unwrap?token={wrapping_token}"


def grant_policy() -> str:
    """The Vault policy the per-grant child token carries. It should allow reading
    only the credential paths a grant may use."""
    return (os.getenv("VAULT_GRANT_POLICY") or "default").strip() or "default"


def _issue_live(reference, grantee, scope, ttl_hours, expires_at) -> dict:
    """Mint a per-grant credential the portal can genuinely revoke, without ever
    seeing the credential itself.

    Why it is done in two steps rather than one wrapped read:

    A response-wrapped read hides the credential from the portal — but it hides
    the LEASE ID too (Vault returns `lease_id: ""` on a wrapped response, because
    the lease belongs inside the wrapped payload). With no lease id the portal
    cannot revoke the credential later; a "revoke" would silently do nothing.

    So instead the portal creates a short-lived CHILD TOKEN for the grant and uses
    that token to fetch the credential. The credential's lease then belongs to the
    child token, and revoking the token by its ACCESSOR cascades to every lease it
    created — genuinely killing the database login. The portal stores only the
    accessor, which cannot be used to authenticate.

    The portal handles the child token in memory for one call and never stores or
    logs it; it never handles the database credential at all.
    """
    import httpx  # lazy: mock mode and the offline test suite need no HTTP client

    addr, token, namespace = _config()
    path = creds_path(reference, grantee, scope)
    ttl_seconds = max(1, int(ttl_hours)) * 3600
    ttl = f"{ttl_seconds}s"

    # 1. A child token scoped to this grant, expiring with it.
    try:
        created = httpx.post(
            f"{addr}/v1/auth/token/create",
            headers=_headers(token, namespace),
            json={
                "policies": [grant_policy()],
                "ttl": ttl,
                "explicit_max_ttl": ttl,
                "renewable": False,
                "display_name": f"grant-{reference}",
                "meta": {"reference": str(reference), "grantee": str(grantee),
                         "scope": str(scope)},
            },
            timeout=10.0,
            verify=_verify(),
        )
    except Exception as exc:  # noqa: BLE001 — surface any transport error cleanly
        raise VaultUnavailable(f"Could not reach the vault: {exc}") from exc
    if created.status_code >= 400:
        raise VaultUnavailable(
            f"Vault refused to create the grant token ({created.status_code}). Check "
            f"the portal's token may create child tokens with policy '{grant_policy()}'."
        )
    auth = (created.json() or {}).get("auth") or {}
    child_token, accessor = auth.get("client_token"), auth.get("accessor")
    if not child_token or not accessor:
        raise VaultUnavailable("Vault did not return a usable grant token.")

    # 2. Read the credential AS THE CHILD TOKEN, wrapped — so the lease is owned by
    #    that token and the secret goes to the grantee, never to the portal.
    try:
        resp = httpx.get(
            f"{addr}/v1/{path}",
            headers=_headers(child_token, namespace, wrap_ttl=ttl),
            timeout=10.0,
            verify=_verify(),
        )
    except Exception as exc:  # noqa: BLE001
        _revoke_accessor(addr, token, namespace, accessor)  # don't leak the token
        raise VaultUnavailable(f"Could not reach the vault: {exc}") from exc

    if resp.status_code >= 400:
        _revoke_accessor(addr, token, namespace, accessor)
        raise VaultUnavailable(
            f"Vault refused to issue the credential ({resp.status_code}) for path "
            f"'{path}'. Check the path exists and policy '{grant_policy()}' may read it."
        )
    info = (resp.json() or {}).get("wrap_info") or {}
    wrapping_token = info.get("token")
    if not wrapping_token:
        # An unwrapped response would put the raw secret in the portal's hands.
        _revoke_accessor(addr, token, namespace, accessor)
        raise VaultUnavailable(
            "Vault did not return a wrapped response, so the credential would have "
            "been exposed to the portal. Refusing. Check the server supports "
            "response wrapping."
        )
    return {
        # The token accessor: revocable, and useless as a credential.
        "handle": accessor,
        "link": _unwrap_link(addr, wrapping_token),
        "expires_at": expires_at.isoformat(),
        "mock": False,
        "note": ("One-time link — it can be opened once. The portal never received "
                 "the credential itself."),
    }


def _revoke_accessor(addr: str, token: str, namespace: str | None, accessor: str):
    """Best-effort accessor revoke used on the error paths (never raises)."""
    import httpx
    try:
        httpx.post(f"{addr}/v1/auth/token/revoke-accessor",
                   headers=_headers(token, namespace), json={"accessor": accessor},
                   timeout=10.0, verify=_verify())
    except Exception:  # noqa: BLE001 — cleanup must not mask the original failure
        pass


def _revoke_live(handle) -> dict:
    """Revoke a grant by its token accessor. Revoking the token cascades to every
    lease it created, so the database login is genuinely killed — not just marked
    revoked in the portal."""
    import httpx

    addr, token, namespace = _config()
    if not handle:
        return {"revoked": False, "handle": handle,
                "note": "No vault handle recorded; the grant is still expired in the portal."}
    try:
        resp = httpx.post(
            f"{addr}/v1/auth/token/revoke-accessor",
            headers=_headers(token, namespace),
            json={"accessor": handle},
            timeout=10.0,
            verify=_verify(),
        )
    except Exception as exc:  # noqa: BLE001
        raise VaultUnavailable(f"Could not reach the vault to revoke: {exc}") from exc
    if resp.status_code >= 400:
        raise VaultUnavailable(
            f"Vault refused to revoke the grant ({resp.status_code}). The grant is "
            "still marked revoked in the portal; check the token accessor in Vault."
        )
    return {"revoked": True, "handle": handle, "mock": False}
