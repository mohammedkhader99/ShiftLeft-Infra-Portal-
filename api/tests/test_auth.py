"""Increment 2.3b checks: the API validates the Microsoft ID token (JWKS, §8).

We generate a real RS256 keypair, sign tokens with it, and point the validator
at the public key — so signature/issuer/audience/expiry are genuinely checked
without contacting Microsoft.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api import auth as api_auth

TENANT = "test-tenant"
CLIENT_ID = "test-client-id"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()


def _make_token(**overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 3600,
        "preferred_username": "alice@emaratechg.ae",
        "name": "Alice",
    }
    claims.update(overrides)
    return jwt.encode(claims, _private_key, algorithm="RS256")


@pytest.fixture(autouse=True)
def live_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "live")
    monkeypatch.setenv("OIDC_TENANT_ID", TENANT)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    # Serve our test public key instead of fetching Entra's JWKS.
    monkeypatch.setattr(api_auth, "_signing_key", lambda token: _public_key)
    yield


def test_valid_token_yields_identity():
    claims = api_auth.validate_token(_make_token())
    assert claims["preferred_username"] == "alice@emaratechg.ae"


def test_wrong_audience_rejected():
    with pytest.raises(jwt.InvalidAudienceError):
        api_auth.validate_token(_make_token(aud="someone-else"))


def test_wrong_issuer_rejected():
    with pytest.raises(jwt.InvalidIssuerError):
        api_auth.validate_token(_make_token(iss="https://evil.example/v2.0"))


def test_expired_token_rejected():
    with pytest.raises(jwt.ExpiredSignatureError):
        api_auth.validate_token(_make_token(exp=int(time.time()) - 10))


def test_get_requester_live_requires_bearer():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api_auth.get_requester(authorization=None, x_requester="spoofed@x.com")
    assert exc.value.status_code == 401


def test_get_requester_live_uses_validated_identity():
    ident = api_auth.get_requester(
        authorization=f"Bearer {_make_token()}", x_requester="spoofed@x.com"
    )
    # Identity comes from the token, NOT the spoofable header.
    assert ident == "alice@emaratechg.ae"
