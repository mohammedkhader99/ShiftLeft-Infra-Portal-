"""Infra Portal — the web UI a person actually looks at.

This is a SEPARATE service from the API (ARCHITECTURE.md §3). The browser talks
only to the portal; the portal talks to the API server-side. The browser never
calls the API directly and holds no credentials (principle P2).

Increment 0.3: one Jinja page with HTMX that shows a live value fetched from the
API. No request forms or database reads yet — those come in Phase 1.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent

# Where the portal reaches the API. Inside docker-compose this is the service
# name "api"; when running the portal by hand it defaults to localhost.
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8081")

app = FastAPI(title="Infra Portal")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Serve the portal page."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/panel", response_class=HTMLResponse)
def panel(request: Request) -> HTMLResponse:
    """Fetch a live value from the API and render it as an HTML fragment.

    Called by HTMX. The timestamp is generated fresh on every call so a Refresh
    visibly proves the data is live, not hard-coded.
    """
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        response = httpx.get(f"{API_BASE_URL}/health", timeout=5.0)
        response.raise_for_status()
        data = response.json()
        api_ok = bool(data.get("ok"))
        api_mock = bool(data.get("mock"))
        error = None
    except Exception as exc:  # noqa: BLE001 — show any failure plainly in the panel
        api_ok = False
        api_mock = None
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "panel.html",
        {
            "api_ok": api_ok,
            "api_mock": api_mock,
            "fetched_at": fetched_at,
            "error": error,
        },
    )
