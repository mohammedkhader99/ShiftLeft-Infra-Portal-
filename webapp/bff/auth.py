"""BFF authentication (UX.1b) — mirrors the HTMX portal's Entra OIDC flow.

AUTH_MODE=live signs users in via Microsoft Entra ID (OIDC); the raw id token is
forwarded to the API, which validates it independently (§8). AUTH_MODE=mock
(default) uses a local dev identity so local work and tests run without
Microsoft. The browser never holds the token — the BFF does.

Entra id tokens live ~1 hour. We also request the standard `offline_access`
scope so Entra returns a **refresh token**, which the BFF uses to renew the id
token silently (see `refresh_tokens`) — so a working session survives past the
one-hour boundary without the user signing in again.

**Where the tokens live.** The session cookie is signed but small; an Entra id
token *plus* a refresh token together blow past the browser's ~4KB per-cookie
limit, so the browser would silently drop the whole cookie (an infinite
login redirect loop). We therefore keep the tokens in a server-side store keyed
by a short opaque session id — only that id (and the user's name/email) ride in
the cookie. Tokens never reach the browser (ARCHITECTURE.md P2). The store is
per-process and in-memory: it assumes the single BFF worker/instance this runs
as today; if the BFF restarts or is scaled out, affected users simply sign in
again (a future shared store would remove that caveat).
"""

import os
import secrets
import time

from authlib.integrations.starlette_client import OAuth

DEFAULT_DEV_USER = {"email": "mohammed.khader@emaratechg.ae", "name": "Dev User (mock)"}

# Server-side token store: sid -> {id_token, refresh_token, token_expires_at,
# stored_at}. Kept out of the cookie because Entra tokens are too large for it.
_TOKEN_STORE: dict[str, dict] = {}
# Drop abandoned entries after this long (a bit over the 8h cookie max-age) so the
# store can't grow without bound.
_STORE_TTL = 12 * 3600

# `offline_access` makes Entra issue a refresh token alongside the id token.
OIDC_SCOPE = "openid email profile offline_access"

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
            client_kwargs={"scope": OIDC_SCOPE},
        )
        _oauth = oauth
    return _oauth


def session_user(request) -> dict | None:
    return request.session.get("user")


# --- Token lifecycle (silent renewal) ----------------------------------------

def _prune_store(now: float) -> None:
    """Drop token entries older than the TTL so an in-memory store can't leak."""
    stale = [sid for sid, e in _TOKEN_STORE.items()
             if now - e.get("stored_at", now) > _STORE_TTL]
    for sid in stale:
        _TOKEN_STORE.pop(sid, None)


def _entry(request) -> dict:
    """The server-side token entry for this session, or {} if none."""
    sid = request.session.get("sid")
    return _TOKEN_STORE.get(sid, {}) if sid else {}


def store_tokens(request, token: dict) -> None:
    """Persist an Entra token response in the server-side store, keyed by a short
    session id kept in the cookie: the id token the API validates, the refresh
    token used to renew it, and when it expires. Nothing here ever reaches the
    browser (P2). A refresh response may rotate the refresh token — we keep
    whichever value the response actually carries and leave the old one otherwise."""
    now = time.time()
    _prune_store(now)
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(24)
        request.session["sid"] = sid
    entry = _TOKEN_STORE.setdefault(sid, {})
    if token.get("id_token"):
        entry["id_token"] = token["id_token"]
    if token.get("refresh_token"):
        entry["refresh_token"] = token["refresh_token"]
    # `expires_at` is unix seconds (Authlib computes it from `expires_in`).
    expires_at = token.get("expires_at")
    if not expires_at and token.get("expires_in"):
        expires_at = int(now) + int(token["expires_in"])
    if expires_at:
        entry["token_expires_at"] = int(expires_at)
    entry["stored_at"] = now


def get_id_token(request) -> str | None:
    """The current id token to forward to the API, or None if there is none."""
    return _entry(request).get("id_token")


def token_expired(request, skew: int = 60) -> bool:
    """True if the stored id token is at, or within `skew` seconds of, expiry —
    i.e. it should be renewed before the next upstream call. Returns False when no
    expiry is tracked (mock, or a session predating this feature), so those are
    left to the reactive 401 path rather than force-refreshed."""
    exp = _entry(request).get("token_expires_at")
    if not exp:
        return False
    return time.time() >= (int(exp) - skew)


async def refresh_tokens(request) -> bool:
    """Renew the id token from the stored refresh token. Returns True on success
    (store updated in place), False if there's no refresh token or Entra rejected
    it — in which case the caller ends the session and the user signs in again.
    Never raises: a failed renewal must not 500 the proxied call."""
    refresh_token = _entry(request).get("refresh_token")
    if not refresh_token:
        return False
    try:
        token = await get_oauth().entra.fetch_access_token(
            grant_type="refresh_token", refresh_token=refresh_token,
        )
    except Exception:  # noqa: BLE001 — any transport/OAuth error → treat as "can't renew"
        return False
    if not token.get("id_token"):
        return False
    store_tokens(request, token)
    return True


def end_session(request) -> None:
    """Sign the user out fully: drop the server-side tokens and the identity so the
    next page load is treated as anonymous and redirected to /login."""
    sid = request.session.pop("sid", None)
    if sid:
        _TOKEN_STORE.pop(sid, None)
    # `user` plus any legacy in-cookie token keys from before the server-side store.
    for key in ("user", "id_token", "refresh_token", "token_expires_at"):
        request.session.pop(key, None)
