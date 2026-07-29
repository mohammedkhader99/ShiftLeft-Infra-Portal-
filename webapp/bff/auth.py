"""BFF authentication (UX.1b) — mirrors the HTMX portal's Entra OIDC flow.

AUTH_MODE=live signs users in via Microsoft Entra ID (OIDC); the raw id token is
kept in the session and forwarded to the API, which validates it independently
(§8). AUTH_MODE=mock (default) uses a local dev identity so local work and tests
run without Microsoft. The browser never holds the token — the BFF does.
"""

import os

from authlib.integrations.starlette_client import OAuth

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
    return request.session.get("user")
