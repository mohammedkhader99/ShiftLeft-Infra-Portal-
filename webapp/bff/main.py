"""Backend-for-Frontend for the React portal (UX.1).

Serves the built React SPA and proxies the browser's /api/* calls to the API,
adding the signed-in user's identity server-side. This keeps the token model,
RBAC and every integration on the API exactly as they are — the browser never
holds a credential (ARCHITECTURE.md P2, §14.1 decision).

Auth in this first increment forwards a dev identity (X-Requester); live Entra
OIDC + Bearer forwarding is the next step, reusing the existing portal's flow.
"""

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8081")
DEV_USER = os.getenv("DEV_USER", "mohammed.khader@emaratechg.ae")
DEV_NAME = os.getenv("DEV_NAME", "Mohammed Khader")
SPA_DIST = Path(os.getenv("SPA_DIST", "/app/dist"))

app = FastAPI(title="Infra Portal Webapp (BFF)")


def _forward_headers() -> dict:
    """Identity headers the BFF adds when proxying to the API."""
    return {"X-Requester": DEV_USER, "X-Requester-Name": DEV_NAME}


async def _proxy_upstream(method: str, url: str, params: dict, content: bytes,
                          headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, params=params, content=content,
                                    headers=headers)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()
    upstream = await _proxy_upstream(
        request.method, f"{API_BASE_URL}/api/{path}",
        dict(request.query_params), body, _forward_headers(),
    )
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))


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
