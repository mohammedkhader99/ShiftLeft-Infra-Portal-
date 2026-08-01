"""Distributed tracing (F-OPS-04): a correlation trace id propagated across the
portal, API, and orchestrator, and stamped on every audit entry, so one request
can be followed end-to-end.

The id is a W3C-style 32-hex string. A middleware reads an incoming X-Trace-Id
(or mints one), holds it in a contextvar for the request, and echoes it back in
the response. The poller mints one per request it advances autonomously.
"""

import contextvars
import secrets

HEADER = "X-Trace-Id"
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    """A fresh W3C trace-id-shaped id (32 hex chars)."""
    return secrets.token_hex(16)


def _normalise(raw: str | None) -> str | None:
    """Accept a caller-supplied id only if it looks safe (bounded, id-charset)."""
    if not raw:
        return None
    raw = raw.strip()[:64]
    return raw if raw and all(c.isalnum() or c in "-_" for c in raw) else None


def current_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def start_trace(incoming: str | None = None) -> str:
    """Set the request's trace id from an incoming header, or mint a fresh one."""
    tid = _normalise(incoming) or new_trace_id()
    _trace_id.set(tid)
    return tid


def install_tracing(app) -> None:
    """Per request: honour an incoming X-Trace-Id (or mint one), hold it for the
    request, and echo it in the response."""
    @app.middleware("http")
    async def _trace(request, call_next):
        tid = start_trace(request.headers.get(HEADER))
        response = await call_next(request)
        response.headers[HEADER] = tid
        return response
