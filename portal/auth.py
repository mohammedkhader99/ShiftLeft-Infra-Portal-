"""Portal authentication (increment 2.3a, Phase 2 auth step A).

AUTH_MODE=live signs users in via Microsoft Entra ID (Azure AD) using the
standard OIDC flow — the portal never sees the password or 2FA, Microsoft does.
AUTH_MODE=mock (default) uses a simple local dev login so local work and tests
run without Microsoft.

Step A trusts the identity the portal establishes here; step B hardens the API
to independently validate the Microsoft token (§8).
"""

import os

from authlib.integrations.starlette_client import OAuth

# A default identity used in mock mode when nobody has signed in — matches the
# requester the API falls back to, so behaviour is unchanged locally.
DEFAULT_DEV_USER = {"email": "mohammed.khader@emaratechg.ae", "name": "Dev User (mock)"}

_oauth: OAuth | None = None


def auth_mode() -> str:
    return os.getenv("AUTH_MODE", "mock").strip().lower()


def is_live() -> bool:
    return auth_mode() == "live"


def get_oauth() -> OAuth:
    """Lazily build the Authlib client for Microsoft Entra ID."""
    global _oauth
    if _oauth is None:
        tenant = os.getenv("OIDC_TENANT_ID", "")
        oauth = OAuth()
        oauth.register(
            name="entra",
            server_metadata_url=(
                f"https://login.microsoftonline.com/{tenant}/v2.0/"
                ".well-known/openid-configuration"
            ),
            client_id=os.getenv("OIDC_CLIENT_ID", ""),
            client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth = oauth
    return _oauth


def session_user(request) -> dict | None:
    """The signed-in user stored in the session, if any."""
    return request.session.get("user")


def requester_email(request) -> str:
    """Email to stamp on requests: the signed-in user, else the mock default."""
    user = session_user(request)
    if user:
        return user["email"]
    return DEFAULT_DEV_USER["email"]
