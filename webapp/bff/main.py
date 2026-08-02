"""Backend-for-Frontend for the React portal (UX.1 / UX.1b).

Serves the built React SPA and proxies the browser's /api/* calls to the API,
adding the signed-in user's identity server-side. Live mode signs the user in
via Microsoft Entra ID and forwards the id token to the API (which validates it
itself, §8); mock mode uses a dev identity. The browser never holds a
credential — the token stays in the BFF's session (ARCHITECTURE.md P2, §14.1).
"""

import os
import secrets
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, security

API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8081")
_DEV_SESSION_SECRET = "dev-insecure-session-secret"
SESSION_SECRET = os.getenv("SESSION_SECRET", _DEV_SESSION_SECRET)
# Strict sessions (F-SEC-09): Secure cookie + a max-age, gated on SESSION_SECURE
# (off for local HTTP; set true in prod). A secure profile must not use the dev
# secret.
SESSION_SECURE = security._bool_env("SESSION_SECURE", False)
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(8 * 3600)))  # 8 hours
if SESSION_SECURE and SESSION_SECRET == _DEV_SESSION_SECRET:
    raise RuntimeError("SESSION_SECURE=true requires a real SESSION_SECRET (not the dev default).")
SPA_DIST = Path(os.getenv("SPA_DIST", "/app/dist"))

# Paths reachable without being signed in.
_PUBLIC_PREFIXES = ("/login", "/auth", "/logout", "/healthz", "/assets", "/favicon")

app = FastAPI(title="Infra Portal Webapp (BFF)")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """In live mode, send anonymous page loads to sign in. /api/* is left to the
    proxy (the API returns 401 if the token is missing/invalid), so the SPA can
    react rather than receiving an HTML redirect to a fetch()."""
    path = request.url.path
    if (auth.is_live() and not path.startswith("/api")
            and not any(path.startswith(p) for p in _PUBLIC_PREFIXES)
            and not auth.session_user(request)):
        return RedirectResponse("/login")
    return await call_next(request)


# Added AFTER the guard so it is the OUTER middleware — request.session must
# exist before the guard reads it. (Last-added middleware runs first.)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   https_only=SESSION_SECURE, max_age=SESSION_MAX_AGE)

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()


async def _shared_rate_counter(key: str) -> int:
    """Increment the API's shared rate-limit counter for `key` and return the
    running count (F-OPS-01) — so the BFF's limit is global across replicas too.
    Raises on any failure, which makes the limiter fail open."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        resp = await client.post(f"{API_BASE_URL}/internal/ratelimit/hit",
                                 json={"identity": key},
                                 headers={"X-Internal-Key": INTERNAL_API_KEY})
        resp.raise_for_status()
        return int(resp.json().get("count", 0))


# Application hardening (F-SEC-09): security headers + Origin-based CSRF + rate
# limit. Added last, so it is the OUTERMOST middleware (rejects/limits early and
# stamps headers on the way out); it doesn't need the session. With
# RATE_LIMIT_BACKEND=shared the count runs through the API's shared store so the
# limit is global across BFF replicas (F-OPS-01); default stays in-memory.
security.install(app, shared_counter=_shared_rate_counter)


def _forward_headers(request: Request) -> dict:
    """Identity the BFF adds when proxying to the API: the signed-in user's
    email/name, plus the raw Entra id token as a Bearer in live mode."""
    user = auth.session_user(request) or auth.DEFAULT_DEV_USER
    headers = {"X-Requester": user["email"], "X-Requester-Name": user.get("name", "")}
    id_token = auth.get_id_token(request)
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    # Distributed tracing (F-OPS-04): start (or forward) a trace id for this hop.
    headers["X-Trace-Id"] = request.headers.get("X-Trace-Id") or secrets.token_hex(16)
    return headers


async def _proxy_upstream(method: str, url: str, params: dict, content: bytes,
                          headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, params=params, content=content,
                                    headers=headers)


@app.get("/login")
async def login(request: Request):
    """Live: redirect to Microsoft. Mock: sign in as the dev user immediately."""
    if auth.is_live():
        redirect_uri = os.getenv("OIDC_REDIRECT_URI", str(request.url_for("auth_callback")))
        return await auth.get_oauth().entra.authorize_redirect(request, redirect_uri)
    request.session["user"] = dict(auth.DEFAULT_DEV_USER)
    return RedirectResponse("/", status_code=303)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Microsoft's redirect back: read the identity, keep the id + refresh tokens
    for the API and for silent renewal."""
    token = await auth.get_oauth().entra.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = info.get("email") or info.get("preferred_username") or ""
    request.session["user"] = {"email": email, "name": info.get("name") or email}
    auth.store_tokens(request, token)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    auth.end_session(request)
    return RedirectResponse("/login" if auth.is_live() else "/", status_code=303)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()
    url = f"{API_BASE_URL}/api/{path}"
    params = dict(request.query_params)
    # Preserve the request's Content-Type so JSON POST bodies parse upstream.
    content_type = request.headers.get("content-type")

    def _headers() -> dict:
        h = _forward_headers(request)
        if content_type:
            h["Content-Type"] = content_type
        return h

    # Silent renewal (live mode): if the id token has expired or is about to,
    # refresh it from the stored refresh token before forwarding, so a long
    # session survives the ~1-hour token lifetime without re-signing in.
    if auth.is_live() and auth.token_expired(request):
        await auth.refresh_tokens(request)  # best-effort; the 401 path below is the backstop

    upstream = await _proxy_upstream(request.method, url, params, body, _headers())

    # Backstop: the API still rejected the token (clock skew, a session predating
    # renewal, or an expired refresh token). Try one refresh + retry; if it still
    # fails, the session can't be renewed — end it and tell the SPA plainly.
    if upstream.status_code == 401 and auth.is_live():
        if await auth.refresh_tokens(request):
            upstream = await _proxy_upstream(request.method, url, params, body, _headers())
        if upstream.status_code == 401:
            auth.end_session(request)
            return JSONResponse(
                {"detail": "Your session has expired — please sign in again."},
                status_code=401,
            )

    # Forward the download filename for file responses (e.g. the Excel cost sheet).
    passthrough = {}
    disposition = upstream.headers.get("content-disposition")
    if disposition:
        passthrough["Content-Disposition"] = disposition
    trace = upstream.headers.get("x-trace-id")  # echo the trace id to the browser (F-OPS-04)
    if trace:
        passthrough["X-Trace-Id"] = trace
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                    headers=passthrough)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# The built SPA (present in the container image; absent during unit tests).
if (SPA_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=SPA_DIST / "assets"), name="assets")


@app.get("/{full_path:path}")
def spa(full_path: str):
    """Serve index.html for every non-API route (client-side routing)."""
    index = SPA_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"error": "SPA build not found"}, status_code=404)
