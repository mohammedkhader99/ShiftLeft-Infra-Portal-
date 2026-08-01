"""Shared (DB-backed) rate-limit store for HA (F-OPS-01 / F-SEC-09).

The default limiter (common/security.py) counts in-process — fast, and fine for a
single replica. Under HA the count must be shared, or N replicas each allow the
full limit (N× the intended rate). This backs a fixed-window counter with one
Postgres row per (client, minute), incremented atomically so the limit is global.

Opt-in via RATE_LIMIT_BACKEND=shared; the default stays in-memory. Works on
Postgres (prod) and SQLite (tests) — no Redis.
"""

import os
import time

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from db.models import RateLimitCounter
from db.session import SessionLocal

WINDOW_SECONDS = 60


def shared_enabled() -> bool:
    """Whether the shared (DB) backend is selected. Default: in-memory."""
    return os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower() == "shared"


def _window_epoch(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // WINDOW_SECONDS)


def hit(session, identity: str, now: float | None = None) -> int:
    """Count one request for `identity` in the current window; return the running
    count. The row is created if absent (race-safe: a concurrent insert loses the
    savepoint but the atomic UPDATE still counts it). Caller-agnostic of dialect."""
    epoch = _window_epoch(now)
    key = f"{identity}:{epoch}"
    # Ensure the row exists without racing: a duplicate insert just rolls back the
    # savepoint, leaving the outer transaction intact.
    try:
        with session.begin_nested():
            session.execute(insert(RateLimitCounter).values(key=key, window_epoch=epoch, count=0))
    except IntegrityError:
        pass
    # Atomic increment (row-locked) — concurrent hits serialise here.
    session.execute(
        update(RateLimitCounter).where(RateLimitCounter.key == key)
        .values(count=RateLimitCounter.count + 1)
    )
    count = session.scalar(select(RateLimitCounter.count).where(RateLimitCounter.key == key))
    session.commit()
    return int(count or 0)


def prune(session, keep_windows: int = 2, now: float | None = None) -> int:
    """Delete counters older than the last `keep_windows` windows. Housekeeping —
    the leader poller calls this each cycle. Returns rows removed."""
    cutoff = _window_epoch(now) - keep_windows
    result = session.execute(delete(RateLimitCounter).where(RateLimitCounter.window_epoch < cutoff))
    session.commit()
    return result.rowcount or 0


def retry_after(now: float | None = None) -> int:
    """Seconds until the current window rolls over (for the Retry-After header)."""
    t = now if now is not None else time.time()
    return max(1, WINDOW_SECONDS - int(t) % WINDOW_SECONDS)


def install_shared_rate_limit(app, *, env: str = "RATE_LIMIT_PER_MINUTE", default: int = 600,
                              exempt_prefixes: tuple = ("/health",), session_factory=None) -> None:
    """Per-client fixed-window rate limit, counted in the shared DB store so the
    limit is global across replicas (F-OPS-01). 0 disables. Same 429/Retry-After
    contract as the in-memory limiter; fails open if the store is unreachable so a
    limiter fault never takes the API down."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    factory = session_factory or SessionLocal

    def _limit() -> int:
        try:
            return int(os.getenv(env, str(default)))
        except ValueError:
            return default

    def _count(identity: str) -> int:
        with factory() as session:
            return hit(session, identity)

    @app.middleware("http")
    async def _shared_rate_limit(request, call_next):
        limit = _limit()
        path = request.url.path
        if limit <= 0 or any(path.startswith(p) for p in exempt_prefixes):
            return await call_next(request)
        identity = request.headers.get("X-Requester") or (request.client.host if request.client else "?")
        try:
            count = await run_in_threadpool(_count, identity)
        except Exception:  # noqa: BLE001 — fail open: never let the store fault the API
            return await call_next(request)
        if count > limit:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded — try again shortly."},
                headers={"Retry-After": str(retry_after())},
            )
        return await call_next(request)
