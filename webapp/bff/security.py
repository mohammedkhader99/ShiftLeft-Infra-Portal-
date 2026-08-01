"""BFF application hardening (F-SEC-09): security headers, an Origin-based CSRF
check, and a rate limiter. Self-contained because the webapp build context can't
reach the repo-root `common` package.
"""

import os
import time
from urllib.parse import urlparse

from starlette.responses import JSONResponse

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# CSP for the React/Carbon SPA: its own scripts, self + inline styles (React inline
# style attributes), data: fonts/images, same-origin fetch/SSE. No framing.
SPA_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _headers() -> dict:
    h = {
        "Content-Security-Policy": SPA_CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if _bool_env("HSTS_ENABLED"):
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h


def _origin_allowed(request, origin: str | None) -> bool:
    if not origin:  # browsers always send Origin on a mutating fetch — absence is suspicious
        return False
    allow = [o.strip() for o in os.getenv("PORTAL_ORIGIN", "").split(",") if o.strip()]
    if allow:
        return origin in allow
    return urlparse(origin).netloc == request.headers.get("host")  # same-origin


def install(app, *, shared_counter=None) -> None:
    """Register the BFF hardening middleware (headers + CSRF + rate limit).

    RATE_LIMIT_BACKEND=shared together with a `shared_counter(key) -> count`
    coroutine counts in the API's shared store, so the per-minute limit is global
    across BFF replicas (F-OPS-01); it FAILS OPEN if the store is unreachable.
    Default 'memory' keeps the in-process limiter — single-instance unchanged.
    """
    headers = _headers()
    csrf_on = _bool_env("CSRF_ENABLED", True)
    shared = os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower() == "shared" and shared_counter is not None
    buckets: dict[str, list] = {}

    def _limit() -> int:
        try:
            return int(os.getenv("RATE_LIMIT_PER_MINUTE", "600"))
        except ValueError:
            return 600

    def _over_limit_memory(key: str, limit: int) -> bool:
        now = time.monotonic()
        w = buckets.get(key)
        if w is None or now - w[0] >= 60.0:
            w = [now, 0]
            buckets[key] = w
        w[1] += 1
        return w[1] > limit

    @app.middleware("http")
    async def _harden(request, call_next):
        # CSRF: a state-changing request must carry a same-origin Origin.
        if csrf_on and request.method in _MUTATING and not _origin_allowed(request, request.headers.get("origin")):
            return JSONResponse(status_code=403, content={"error": "CSRF check failed: missing or bad Origin."})
        # Rate limit (per identity, else client host); /healthz exempt.
        limit = _limit()
        if limit > 0 and not request.url.path.startswith("/healthz"):
            key = request.headers.get("X-Requester") or (request.client.host if request.client else "?")
            if shared:
                try:
                    over = (await shared_counter(key)) > limit
                except Exception:  # noqa: BLE001 — fail open: the store must never block the portal
                    over = False
            else:
                over = _over_limit_memory(key, limit)
            if over:
                retry = max(1, 60 - int(time.time()) % 60)
                return JSONResponse(status_code=429, content={"error": "Rate limit exceeded — try again shortly."},
                                    headers={"Retry-After": str(retry)})
        response = await call_next(request)
        for name, value in headers.items():
            response.headers.setdefault(name, value)
        return response
