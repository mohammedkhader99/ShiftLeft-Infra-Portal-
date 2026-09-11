"""One HTTP client per process, instead of one per call.

`httpx.post(...)` and its siblings are convenience wrappers: each one builds a
whole client for a single request and throws it away. That client carries a
connection pool and an SSL context loaded from the CA bundle on disk, and it is
built whether or not the URL is https. Measured on the development machine,
constructing it cost **318ms before a single packet moved**.

The policy gate is where that hurts. Every submit asks OPA, every placement
evaluation asks OPA, the orchestrator re-asks before it executes, and the test
suite asks thousands of times. Measured against the local OPA container:

    localhost, new client each call (what we had)   804.8 ms
    127.0.0.1, new client each call                 772.8 ms
    localhost, one reused client                     47.5 ms
    127.0.0.1, one reused client                      3.1 ms

Two separate costs, and the order matters. Building the client dominates, so
swapping the hostname alone buys almost nothing. Once the client is reused, the
hostname becomes the larger remaining cost: `localhost` resolves to both `::1`
and `127.0.0.1`, and on Windows the one Docker did not publish on is tried
first. Hence both changes together — reuse the client, and give the address
rather than a name.

Reusing the client keeps the pool and the SSL context alive, so the second call
and every call after it pays for the request alone.

WHAT THIS DOES NOT CHANGE: the timeout stays with the caller. Every call site
passes its own `timeout=`, exactly as before, so a slow dependency is bounded by
the same number it was bounded by yesterday. A shared client must never become a
place where one caller's patience silently becomes another's.
"""

import atexit
import threading

import httpx

# Only a floor for a caller that passes nothing. Every call site in this
# codebase passes its own, and should: 5s is right for OPA on loopback and wrong
# for a cloud price list.
DEFAULT_TIMEOUT = 5.0

_CLIENT: httpx.Client | None = None
_LOCK = threading.Lock()


def client() -> httpx.Client:
    """The process-wide client, built on first use.

    Built lazily rather than at import time for two reasons: importing this
    module should cost nothing, and a process that forks after import must not
    inherit a pool of sockets that belong to its parent.

    Safe to share across threads — `httpx.Client` is documented as thread-safe
    for issuing requests, which is what FastAPI's sync endpoints need, since
    they run in a worker threadpool.
    """
    global _CLIENT
    if _CLIENT is None:
        with _LOCK:
            # Checked again under the lock: two threads can both find it empty.
            if _CLIENT is None:
                _CLIENT = httpx.Client(timeout=DEFAULT_TIMEOUT)
                atexit.register(close)
    return _CLIENT


def close() -> None:
    """Drop the client and close its connections.

    For shutdown, and for tests that want the next call to build a fresh one.
    """
    global _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None
