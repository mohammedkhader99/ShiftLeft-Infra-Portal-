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


def install(app) -> None:
    """Register the BFF hardening middleware (headers + CSRF + rate limit)."""
    headers = _headers()
    csrf_on = _bool_env("CSRF_ENABLED", True)
    buckets: dict[str, list] = {}

    def _limit() -> int:
        try:
            return int(os.getenv("RATE_LIMIT_PER_MINUTE", "600"))
        except ValueError:
            return 600

    @app.middleware("http")
    async def _harden(request, call_next):
        # CSRF: a state-changing request must carry a same-origin Origin.
        if csrf_on and request.method in _MUTATING and not _origin_allowed(request, request.headers.get("origin")):
            return JSONResponse(status_code=403, content={"error": "CSRF check failed: missing or bad Origin."})
        # Rate limit (per identity, else client host); /healthz exempt.
        limit = _limit()
        if limit > 0 and not request.url.path.startswith("/healthz"):
            key = request.headers.get("X-Requester") or (request.client.host if request.client else "?")
            now = time.monotonic()
            w = buckets.get(key)
            if w is None or now - w[0] >= 60.0:
                w = [now, 0]
                buckets[key] = w
            w[1] += 1
            if w[1] > limit:
                return JSONResponse(status_code=429, content={"error": "Rate limit exceeded — try again shortly."},
                                    headers={"Retry-After": str(max(1, int(60 - (now - w[0])) + 1))})
        response = await call_next(request)
        for key, value in headers.items():
            response.headers.setdefault(key, value)
        return response
