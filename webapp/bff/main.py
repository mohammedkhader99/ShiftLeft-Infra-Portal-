"""Backend-for-Frontend for the React portal (UX.1 / UX.1b).

Serves the built React SPA and proxies the browser's /api/* calls to the API,
adding the signed-in user's identity server-side. Live mode signs the user in
via Microsoft Entra ID and forwards the id token to the API (which validates it
itself, §8); mock mode uses a dev identity. The browser never holds a
credential — the token stays in the BFF's session (ARCHITECTURE.md P2, §14.1).
"""

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth

API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8081")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-session-secret")
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
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


def _forward_headers(request: Request) -> dict:
    """Identity the BFF adds when proxying to the API: the signed-in user's
    email/name, plus the raw Entra id token as a Bearer in live mode."""
    user = auth.session_user(request) or auth.DEFAULT_DEV_USER
    headers = {"X-Requester": user["email"], "X-Requester-Name": user.get("name", "")}
    id_token = request.session.get("id_token")
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
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
    """Microsoft's redirect back: read the identity, keep the id token for the API."""
    token = await auth.get_oauth().entra.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = info.get("email") or info.get("preferred_username") or ""
    request.session["user"] = {"email": email, "name": info.get("name") or email}
    request.session["id_token"] = token.get("id_token")
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    request.session.pop("id_token", None)
    return RedirectResponse("/login" if auth.is_live() else "/", status_code=303)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()
    headers = _forward_headers(request)
    # Preserve the request's Content-Type so JSON POST bodies parse upstream.
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    upstream = await _proxy_upstream(
        request.method, f"{API_BASE_URL}/api/{path}",
        dict(request.query_params), body, headers,
    )
    # Forward the download filename for file responses (e.g. the Excel cost sheet).
    passthrough = {}
    disposition = upstream.headers.get("content-disposition")
    if disposition:
        passthrough["Content-Disposition"] = disposition
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
