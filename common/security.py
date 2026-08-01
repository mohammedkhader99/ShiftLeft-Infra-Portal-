"""Application-hardening helpers (F-SEC-09): security-response headers and a
lightweight in-process rate limiter. Used by the API. (The BFF has its own copy —
its build context can't reach this package — plus a CSRF origin check.)
"""

import os
import time


def bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def build_headers(csp: str, *, hsts: bool) -> dict:
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if hsts:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def install_security_headers(app, *, csp: str, hsts_env: str = "HSTS_ENABLED") -> None:
    """Add hardening headers to every response (only if not already set)."""
    headers = build_headers(csp, hsts=bool_env(hsts_env))

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        for key, value in headers.items():
            response.headers.setdefault(key, value)
        return response


def install_rate_limit(app, *, env: str = "RATE_LIMIT_PER_MINUTE", default: int = 600,
                       exempt_prefixes: tuple = ("/health",)) -> None:
    """Per-client fixed-window rate limit. 0 disables. In-memory (single process);
    use a shared store under HA. Keys by X-Requester identity, else client host."""
    from starlette.responses import JSONResponse
    buckets: dict[str, list] = {}  # key -> [window_start, count]

    def _limit() -> int:
        try:
            return int(os.getenv(env, str(default)))
        except ValueError:
            return default

    @app.middleware("http")
    async def _rate_limit(request, call_next):
        limit = _limit()
        path = request.url.path
        if limit <= 0 or any(path.startswith(p) for p in exempt_prefixes):
            return await call_next(request)
        key = request.headers.get("X-Requester") or (request.client.host if request.client else "?")
        now = time.monotonic()
        w = buckets.get(key)
        if w is None or now - w[0] >= 60.0:
            if len(buckets) > 5000:  # opportunistic prune of stale windows
                for k in [k for k, ww in buckets.items() if now - ww[0] >= 60.0]:
                    buckets.pop(k, None)
            w = [now, 0]
            buckets[key] = w
        w[1] += 1
        if w[1] > limit:
            retry = int(60 - (now - w[0])) + 1
            return JSONResponse(status_code=429,
                                content={"error": "Rate limit exceeded — try again shortly."},
                                headers={"Retry-After": str(max(1, retry))})
        return await call_next(request)
