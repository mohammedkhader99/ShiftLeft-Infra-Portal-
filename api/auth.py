"""API-side identity validation (increment 2.3b, Phase 2 auth step B).

In live mode the API does NOT trust a header for who the requester is: it
validates the Microsoft Entra ID token the portal forwards — verifying the
signature against Entra's public keys (JWKS), plus issuer, audience and expiry
(ARCHITECTURE.md §8). In mock mode it keeps the step-A behaviour (X-Requester
header or the default), so local work and tests are unchanged.
"""

import os

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

MOCK_REQUESTER = "mohammed.khader@emaratechg.ae"

_jwks_client: PyJWKClient | None = None


def is_live() -> bool:
    return os.getenv("AUTH_MODE", "mock").strip().lower() == "live"


def _tenant() -> str:
    return os.getenv("OIDC_TENANT_ID", "")


def _client_id() -> str:
    return os.getenv("OIDC_CLIENT_ID", "")


def _issuer() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}/v2.0"


def _signing_key(token: str):
    """Fetch the correct public key for this token from Entra's JWKS (cached)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{_tenant()}/discovery/v2.0/keys"
        )
    return _jwks_client.get_signing_key_from_jwt(token).key


def validate_token(token: str) -> dict:
    """Return the verified claims, or raise jwt exceptions if invalid."""
    return jwt.decode(
        token,
        _signing_key(token),
        algorithms=["RS256"],
        audience=_client_id(),
        issuer=_issuer(),
    )


def get_requester(
    authorization: str | None = Header(default=None),
    x_requester: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: the authenticated requester's identity.

    Live: require a valid Bearer ID token and take the identity from it.
    Mock: use the X-Requester header (from the portal session), else the default.
    """
    if not is_live():
        return x_requester or MOCK_REQUESTER

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    try:
        claims = validate_token(token)
    except Exception as exc:  # noqa: BLE001 — any validation failure is a 401
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return claims.get("preferred_username") or claims.get("email") or claims.get("sub")


def get_requester_name(
    authorization: str | None = Header(default=None),
    x_requester_name: str | None = Header(default=None),
) -> str | None:
    """FastAPI dependency: the requester's display name (full name), if known.

    Live: the 'name' claim from the validated token. Mock: the X-Requester-Name
    header the portal forwards from the session. None if unavailable.
    """
    if not is_live():
        return x_requester_name
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return validate_token(authorization.split(" ", 1)[1]).get("name")
    except Exception:  # noqa: BLE001 — name is best-effort, never fail the request
        return None
