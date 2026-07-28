"""Infra Portal — the web UI a person actually looks at.

This is a SEPARATE service from the API (ARCHITECTURE.md §3). The browser talks
only to the portal; the portal talks to the API server-side. The browser never
calls the API directly and holds no credentials (principle P2).

Increment 0.3: one Jinja page with HTMX that shows a live value fetched from the
API. No request forms or database reads yet — those come in Phase 1.
"""

import os
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from portal import auth

BASE_DIR = Path(__file__).resolve().parent

# Where the portal reaches the API. Inside docker-compose this is the service
# name "api"; when running the portal by hand it defaults to localhost.
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8081")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-session-secret")

app = FastAPI(title="Infra Portal")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Paths reachable without being signed in.
_PUBLIC_PREFIXES = ("/login", "/auth", "/logout", "/static", "/health")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """In live mode, redirect anonymous users to sign in (mock mode is open)."""
    if auth.is_live() and not any(
        request.url.path.startswith(p) for p in _PUBLIC_PREFIXES
    ):
        if not auth.session_user(request):
            return RedirectResponse("/login")
    return await call_next(request)


# Session middleware is added AFTER the guard so it is the OUTER middleware —
# request.session must be set up before the guard reads it. (Order matters:
# the last-added middleware runs first.)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


def _requester_headers(request: Request) -> dict:
    """Headers identifying the signed-in user to the API.

    X-Requester is used in mock mode (2.3a); in live mode we also forward the
    Microsoft ID token as a Bearer, which the API validates itself (2.3b).
    """
    headers = {"X-Requester": auth.requester_email(request)}
    id_token = request.session.get("id_token")
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    return headers


def _reauth_redirect(request: Request) -> RedirectResponse:
    """The Microsoft token expired: drop it and send the user to sign in again,
    returning them to the request page afterwards."""
    request.session.pop("id_token", None)
    request.session.pop("user", None)
    request.session["post_login"] = "/request/new"
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    """Live: redirect to Microsoft. Mock: show a simple dev login form."""
    if auth.is_live():
        redirect_uri = os.getenv("OIDC_REDIRECT_URI", str(request.url_for("auth_callback")))
        return await auth.get_oauth().entra.authorize_redirect(request, redirect_uri)
    return templates.TemplateResponse(
        request, "login.html", {"default_email": auth.DEFAULT_DEV_USER["email"]}
    )


@app.post("/login")
def login_mock(request: Request, email: str = Form(...), name: str = Form("")):
    """Mock-mode dev login: trust the entered identity (local only)."""
    request.session["user"] = {"email": email.strip(), "name": name.strip() or email.strip()}
    return RedirectResponse("/", status_code=303)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Handle Microsoft's redirect back: read identity, start the session."""
    token = await auth.get_oauth().entra.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = info.get("email") or info.get("preferred_username") or ""
    request.session["user"] = {"email": email, "name": info.get("name") or email}
    # Keep the raw ID token so we can forward it to the API, which validates it
    # independently (step B, §8).
    request.session["id_token"] = token.get("id_token")
    # Return to wherever the user was when their token expired, if we saved it.
    dest = request.session.pop("post_login", "/")
    return RedirectResponse(dest, status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login" if auth.is_live() else "/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Serve the portal page."""
    return templates.TemplateResponse(
        request, "index.html", {"user": auth.session_user(request) or auth.DEFAULT_DEV_USER}
    )


@app.get("/requests", response_class=HTMLResponse)
def my_requests(request: Request) -> HTMLResponse:
    """The 'My requests' dashboard: every request the signed-in user has made,
    newest first, with its live status and a link to its Jira ticket."""
    email = auth.requester_email(request)
    try:
        response = httpx.get(
            f"{API_BASE_URL}/api/requests",
            params={"requester": email},
            headers=_requester_headers(request),
            timeout=5.0,
        )
        response.raise_for_status()
        rows, error = response.json(), None
    except Exception as exc:  # noqa: BLE001 — surface the failure on the page
        rows, error = [], str(exc)
    return templates.TemplateResponse(
        request,
        "my_requests.html",
        {
            "user": auth.session_user(request) or auth.DEFAULT_DEV_USER,
            "requests": rows,
            "error": error,
        },
    )


# --- Guided request: dropdowns, drafts, validation (increments 1.2 + 1.3) ----

EMPTY_LOOKUPS = {"projects": [], "cost_centres": [], "technologies": [], "environments": []}
FORM_FIELDS = (
    "request_type",
    "project_code",
    "cost_centre_code",
    "deployment_target",
    "environment_name",
    "target_environment",
    "source_reference",
    "data_classification",
)


def _components_from_lists(techs: list[str], sizes: list[str]) -> list[dict]:
    """Turn the repeated component_technology/component_size form values into rows.

    Fully-blank rows are dropped so a stray empty row doesn't get saved.
    """
    rows: list[dict] = []
    for tech, size in zip_longest(techs or [], sizes or [], fillvalue=""):
        tech = (tech or "").strip()
        size = (size or "").strip()
        if tech or size:
            rows.append({"technology_code": tech or None, "size": size or None})
    return rows


def _fetch_lookups() -> tuple[dict, str | None]:
    """Get the dropdown reference data from the API (server-side, per P2)."""
    try:
        response = httpx.get(f"{API_BASE_URL}/api/lookups", timeout=5.0)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:  # noqa: BLE001 — surface the failure on the page
        return EMPTY_LOOKUPS, str(exc)


def _fetch_provisioned(request: Request) -> list[dict]:
    """The signed-in user's currently provisioned requests — the pick-list for a
    decommission. Best-effort: an empty list if the API can't be reached."""
    try:
        response = httpx.get(
            f"{API_BASE_URL}/api/requests",
            params={"requester": auth.requester_email(request), "status": "provisioned"},
            headers=_requester_headers(request),
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — the dropdown just shows empty if this fails
        return []


def _decommission_components(decommission_tech: list[str]) -> list[dict]:
    """Turn the decommission checkboxes (value 'technology:size') into component
    rows. Unchecked boxes simply aren't submitted."""
    rows: list[dict] = []
    for value in decommission_tech or []:
        tech, _, size = (value or "").partition(":")
        tech = tech.strip()
        if tech:
            rows.append({"technology_code": tech, "size": size.strip() or None})
    return rows


def _render_form(
    request: Request,
    form: dict,
    *,
    reference: str | None = None,
    errors: dict | None = None,
    policy_violations: list | None = None,
    saved: bool = False,
    submitted: bool = False,
    provisioned: bool = False,
    planned: bool = False,
    plan_summary: str | None = None,
    in_progress: bool = False,
    decommissioned: bool = False,
    audit: list | None = None,
    banner_error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    lookups, lookup_error = _fetch_lookups()
    # The decommission source dropdown + which of its technologies are ticked.
    provisioned_requests = _fetch_provisioned(request)
    selected_techs = ",".join(
        (c.get("technology_code") or "") for c in (form.get("components") or [])
        if c.get("technology_code")
    )
    return templates.TemplateResponse(
        request,
        "request_new.html",
        {
            "lookups": lookups,
            "provisioned_requests": provisioned_requests,
            "decommission_selected": selected_techs,
            "error": banner_error or lookup_error,
            "notice": notice,
            "form": form,
            "reference": reference,
            "errors": errors or {},
            "policy_violations": policy_violations or [],
            "saved": saved,
            "submitted": submitted,
            "provisioned": provisioned,
            "planned": planned,
            "plan_summary": plan_summary,
            "in_progress": in_progress,
            "decommissioned": decommissioned,
            "audit": audit or [],
            "user": auth.session_user(request) or auth.DEFAULT_DEV_USER,
        },
    )


def _form_payload(reference: str, values: dict, components: list[dict]) -> dict:
    payload = {field: (values.get(field) or None) for field in FORM_FIELDS}
    payload["components"] = components
    if reference:
        payload["reference"] = reference
    return payload


@app.get("/request/new", response_class=HTMLResponse)
def request_new(request: Request) -> HTMLResponse:
    """Blank guided-request form with dropdowns filled live from the API."""
    return _render_form(request, form={})


@app.get("/request/decommission-technologies", response_class=HTMLResponse)
def decommission_technologies(
    request: Request, source_reference: str = "", selected: str = ""
) -> HTMLResponse:
    """HTMX fragment: the technology stack of the chosen provisioned request,
    rendered as checkboxes so the user picks what to decommission."""
    components: list[dict] = []
    if source_reference:
        try:
            resp = httpx.get(
                f"{API_BASE_URL}/api/requests/{source_reference}", timeout=5.0
            )
            if resp.status_code == 200:
                components = resp.json().get("components", [])
        except Exception:  # noqa: BLE001 — show an empty list if the API is down
            components = []
    selected_set = {s for s in (selected or "").split(",") if s}
    return templates.TemplateResponse(
        request,
        "decommission_techs.html",
        {"components": components, "selected": selected_set,
         "source_reference": source_reference},
    )


@app.get("/request/{reference}", response_class=HTMLResponse)
def request_resume(request: Request, reference: str) -> HTMLResponse:
    """Resume a saved draft, pre-filled from the API (F-UX-01)."""
    try:
        response = httpx.get(f"{API_BASE_URL}/api/requests/{reference}", timeout=5.0)
        if response.status_code == 404:
            return _render_form(
                request, form={}, banner_error=f"No request found with reference {reference}."
            )
        response.raise_for_status()
        saved_request = response.json()
    except Exception as exc:  # noqa: BLE001
        return _render_form(request, form={}, banner_error=str(exc))

    return _render_form(
        request,
        form=saved_request,
        reference=saved_request.get("reference"),
        submitted=(saved_request.get("status") == "submitted"),
    )


def _values(scalars: dict, components: list[dict]) -> dict:
    """A form dict for re-rendering after an error, mirroring the API shape."""
    return {**{field: scalars.get(field, "") for field in FORM_FIELDS}, "components": components}


@app.post("/request/save", response_class=HTMLResponse)
def request_save(
    request: Request,
    reference: str = Form(""),
    request_type: str = Form(""),
    project_code: str = Form(""),
    cost_centre_code: str = Form(""),
    deployment_target: str = Form(""),
    environment_name: str = Form(""),
    target_environment: str = Form(""),
    source_reference: str = Form(""),
    data_classification: str = Form(""),
    component_technology: list[str] = Form(default=[]),
    component_size: list[str] = Form(default=[]),
    decommission_tech: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Save the current form as a draft (partial data allowed)."""
    scalars = {field: locals()[field] for field in FORM_FIELDS}
    components = (
        _decommission_components(decommission_tech)
        if request_type == "decommission"
        else _components_from_lists(component_technology, component_size)
    )
    try:
        response = httpx.post(
            f"{API_BASE_URL}/api/requests/draft",
            json=_form_payload(reference, scalars, components),
            headers=_requester_headers(request),
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return _render_form(
            request, form=_values(scalars, components), reference=reference or None,
            banner_error=str(exc),
        )
    if response.status_code == 401:  # token expired -> re-authenticate
        return _reauth_redirect(request)
    try:
        response.raise_for_status()
        saved_request = response.json()
    except Exception as exc:  # noqa: BLE001
        return _render_form(
            request, form=_values(scalars, components), reference=reference or None,
            banner_error=str(exc),
        )

    return _render_form(
        request, form=saved_request, reference=saved_request["reference"], saved=True
    )


@app.post("/request/submit", response_class=HTMLResponse)
def request_submit(
    request: Request,
    reference: str = Form(""),
    request_type: str = Form(""),
    project_code: str = Form(""),
    cost_centre_code: str = Form(""),
    deployment_target: str = Form(""),
    environment_name: str = Form(""),
    target_environment: str = Form(""),
    source_reference: str = Form(""),
    data_classification: str = Form(""),
    component_technology: list[str] = Form(default=[]),
    component_size: list[str] = Form(default=[]),
    decommission_tech: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Persist the current form, then run authoritative validation on submit."""
    scalars = {field: locals()[field] for field in FORM_FIELDS}
    components = (
        _decommission_components(decommission_tech)
        if request_type == "decommission"
        else _components_from_lists(component_technology, component_size)
    )
    try:
        # Save the current form first so submission validates exactly what's shown.
        draft = httpx.post(
            f"{API_BASE_URL}/api/requests/draft",
            json=_form_payload(reference, scalars, components),
            headers=_requester_headers(request),
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return _render_form(
            request, form=_values(scalars, components), reference=reference or None,
            banner_error=str(exc),
        )
    if draft.status_code == 401:  # token expired -> re-authenticate
        return _reauth_redirect(request)
    try:
        draft.raise_for_status()
        saved_request = draft.json()
        ref = saved_request["reference"]

        # Submit does live work (create the Jira ticket + attach the PDF), so
        # it needs a generous timeout.
        submit = httpx.post(f"{API_BASE_URL}/api/requests/{ref}/submit", timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return _render_form(
            request, form=_values(scalars, components), reference=reference or None,
            banner_error=str(exc),
        )

    if submit.status_code == 422:
        payload = submit.json()
        return _render_form(
            request,
            form=saved_request,
            reference=ref,
            errors=payload.get("errors", {}),
            policy_violations=payload.get("policy_violations", []),
        )
    if submit.status_code == 503:
        return _render_form(
            request, form=saved_request, reference=ref,
            banner_error=submit.json().get("policy_error", "Policy service unavailable."),
        )
    if submit.status_code == 502:
        return _render_form(
            request, form=saved_request, reference=ref,
            banner_error=submit.json().get("error", "Could not raise the Jira ticket."),
        )

    submit.raise_for_status()
    return _render_form(request, form=submit.json(), reference=ref, submitted=True)


@app.post("/request/{reference}/approve", response_class=HTMLResponse)
def request_approve(
    request: Request, reference: str, jira_key: str = Form("")
) -> HTMLResponse:
    """Simulate the Jira approval and show the provisioning result + audit.

    Clearly a mock/demo affordance: it stands in for the approver acting in
    Jira. The portal never holds approval authority (P1) — it just calls the
    API, which records the approval and fires the signed orchestrator handoff.
    """
    try:
        # Approving fires the handoff, which in live mode runs a real terraform
        # plan against OCI — allow plenty of time.
        approve_resp = httpx.post(
            f"{API_BASE_URL}/api/approvals/{jira_key}/approve", timeout=310.0
        )
        saved_request = httpx.get(
            f"{API_BASE_URL}/api/requests/{reference}", timeout=5.0
        ).json()
        audit = httpx.get(
            f"{API_BASE_URL}/api/requests/{reference}/audit", timeout=5.0
        ).json().get("entries", [])
    except Exception as exc:  # noqa: BLE001
        return _render_form(request, form={}, reference=reference, banner_error=str(exc))

    data = approve_resp.json() if approve_resp.headers.get("content-type", "").startswith("application/json") else {}
    provisioned = bool(data.get("provisioned"))
    planned = bool(data.get("planned"))
    decommissioned = bool(data.get("decommissioned"))
    plan_summary = (data.get("result") or {}).get("plan_summary")
    if approve_resp.status_code != 200:
        banner_error, notice = data.get("error", "Approval failed."), None
    elif decommissioned:
        banner_error, notice = None, data.get("message")
    else:
        # 200 but nothing done = not yet approved in Jira: an informational notice.
        banner_error, notice = None, (None if provisioned or planned else data.get("message"))
    return _render_form(
        request, form=saved_request, reference=reference, submitted=True,
        provisioned=provisioned, planned=planned, plan_summary=plan_summary,
        decommissioned=decommissioned, audit=audit, banner_error=banner_error, notice=notice,
    )


def _action_and_render(request: Request, reference: str, path: str, *, provisioned: bool = False,
                       decommissioned: bool = False) -> HTMLResponse:
    """Call an API request-action (apply/destroy) and re-render with the result."""
    try:
        resp = httpx.post(f"{API_BASE_URL}/api/requests/{reference}/{path}", timeout=310.0)
        saved_request = httpx.get(f"{API_BASE_URL}/api/requests/{reference}", timeout=5.0).json()
        audit = httpx.get(
            f"{API_BASE_URL}/api/requests/{reference}/audit", timeout=5.0
        ).json().get("entries", [])
    except Exception as exc:  # noqa: BLE001
        return _render_form(request, form={}, reference=reference, banner_error=str(exc))
    ok = resp.status_code == 200
    error = None if ok else (resp.json().get("error", f"{path} failed.")
                             if resp.headers.get("content-type", "").startswith("application/json")
                             else f"{path} failed.")
    return _render_form(
        request, form=saved_request, reference=reference, submitted=True, audit=audit,
        provisioned=provisioned and ok, decommissioned=decommissioned and ok, banner_error=error,
    )


@app.post("/request/{reference}/apply", response_class=HTMLResponse)
def request_apply(request: Request, reference: str) -> HTMLResponse:
    """Start provisioning; the page then polls for live progress -> resolved."""
    try:
        resp = httpx.post(f"{API_BASE_URL}/api/requests/{reference}/apply", timeout=30.0)
        saved_request = httpx.get(f"{API_BASE_URL}/api/requests/{reference}", timeout=5.0).json()
        audit = httpx.get(
            f"{API_BASE_URL}/api/requests/{reference}/audit", timeout=5.0
        ).json().get("entries", [])
    except Exception as exc:  # noqa: BLE001
        return _render_form(request, form={}, reference=reference, banner_error=str(exc))
    if resp.status_code != 200:
        error = resp.json().get("error", "Apply failed.") if \
            resp.headers.get("content-type", "").startswith("application/json") else "Apply failed."
        return _render_form(request, form=saved_request, reference=reference, submitted=True,
                            audit=audit, banner_error=error)
    return _render_form(request, form=saved_request, reference=reference, submitted=True,
                        in_progress=True, audit=audit)


@app.get("/request/{reference}/progress", response_class=HTMLResponse)
def request_progress(request: Request, reference: str) -> HTMLResponse:
    """Live progress fragment (HTMX polls this until provisioning finishes)."""
    try:
        status = httpx.get(
            f"{API_BASE_URL}/api/requests/{reference}", timeout=5.0
        ).json().get("status")
    except Exception:  # noqa: BLE001
        status = None
    return templates.TemplateResponse(
        request, "progress_panel.html", {"status": status, "reference": reference}
    )


@app.post("/request/{reference}/destroy", response_class=HTMLResponse)
def request_destroy(request: Request, reference: str) -> HTMLResponse:
    """Destroy the resources created for a request."""
    return _action_and_render(request, reference, "destroy", decommissioned=True)


@app.post("/request/sizing", response_class=HTMLResponse)
def request_sizing(
    request: Request,
    component_technology: list[str] = Form(default=[]),
    component_size: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Resolve sizing for the current components and render the live panel.

    Called by HTMX whenever a component changes. The numbers come from the API
    (server-side, per P2) — the browser only displays them.
    """
    components = _components_from_lists(component_technology, component_size)
    empty = {"components": [], "totals": {"vcpu": 0, "memory_gb": 0, "storage_gb": 0}}
    try:
        response = httpx.post(
            f"{API_BASE_URL}/api/sizing", json={"components": components}, timeout=5.0
        )
        response.raise_for_status()
        sizing = response.json()
        error = None
    except Exception as exc:  # noqa: BLE001
        sizing = empty
        error = str(exc)

    return templates.TemplateResponse(
        request, "sizing_panel.html", {"sizing": sizing, "error": error}
    )


@app.post("/request/cost", response_class=HTMLResponse)
def request_cost(
    request: Request,
    deployment_target: str = Form(""),
    component_technology: list[str] = Form(default=[]),
    component_size: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Estimate cost for the current components + target and render the panel.

    Called by HTMX when a component or the target changes. Numbers come from the
    API (server-side, per P2).
    """
    components = _components_from_lists(component_technology, component_size)
    empty = {
        "currency": "AED",
        "lines": [],
        "known_target": False,
        "totals": {"one_time": 0, "monthly": 0, "annual": 0},
    }
    try:
        response = httpx.post(
            f"{API_BASE_URL}/api/cost",
            json={"deployment_target": deployment_target, "components": components},
            timeout=5.0,
        )
        response.raise_for_status()
        cost = response.json()
        error = None
    except Exception as exc:  # noqa: BLE001
        cost = empty
        error = str(exc)

    return templates.TemplateResponse(
        request, "cost_panel.html", {"cost": cost, "error": error}
    )


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
