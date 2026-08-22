"""Whether the network can fetch an operating system's packages, as the API sees it.

THE API CANNOT SEE THE TENANCY. It holds no cloud credentials by design, so the
fact is fetched from the orchestrator over the signed channel and cached — the
request form asks per image, and a route-table lookup per dropdown entry would be
absurd.

WHY THE PORTAL ASKS AT ALL. REQ-2026-0134 built Apache on Oracle Linux and nginx
on Ubuntu on the same subnet. Apache installed and served on :80; nginx reported
`nginx NOT INSTALLED`. The subnet had a service gateway and no NAT gateway:
Oracle's yum mirrors are inside the Oracle Services Network and reachable, while
Ubuntu's apt repositories are on the public internet and were not. The portal
offered the image, priced it, had it approved and built a machine that could
never have worked.

UNKNOWN IS NOT PERMISSIVE, AND IT IS NOT RESTRICTIVE EITHER. `families()` returns
None when the answer could not be read, and callers must surface that rather than
silently allowing or silently blocking. The same conflation — an except branch
that returned "no opinion" — left the OS-family filter inert in production for a
fortnight while every test passed.
"""

from __future__ import annotations

import os
import threading
import time

_cache: dict | None = None
_fetched_at: float = 0.0
_lock = threading.Lock()


def ttl_seconds() -> int:
    """A route table changes when someone deliberately changes it, which is rare
    and never mid-form. Long enough to be free, short enough that adding a NAT
    gateway takes effect while you are still at your desk."""
    try:
        return max(30, int(os.getenv("NETWORK_EGRESS_TTL_SECONDS", "300")))
    except ValueError:
        return 300


def refresh(fetcher) -> bool:
    """Re-read egress from the orchestrator. True if the cache was updated."""
    global _cache, _fetched_at
    try:
        answer = fetcher()
    except Exception:  # noqa: BLE001 - a lookup must never break a page render
        return False
    if not isinstance(answer, dict):
        return False
    with _lock:
        _cache = answer
        _fetched_at = time.time()
    return True


def current(fetcher) -> dict | None:
    """The whole answer, or None if it has never been read."""
    global _cache
    if _cache is None or (time.time() - _fetched_at) > ttl_seconds():
        refresh(fetcher)
    return _cache


def families(fetcher) -> set[str] | None:
    """OS families whose packages this network can actually fetch, or None.

    None means "no opinion" — the orchestrator could not be reached, or it is not
    building real machines and so nothing constrains it. It does NOT mean "no
    family can install", which would refuse every image on the form.
    """
    answer = current(fetcher)
    if not answer or not answer.get("known"):
        return None
    return {str(f).strip().lower() for f in answer.get("families") or []}


def can_install(family: str, fetcher) -> bool:
    """Whether a machine of this family could fetch its packages here.

    Absent an answer this says yes. That is deliberate: refusing every image
    because a lookup failed would take the form down, and the machine's own boot
    report is the backstop that catches what this check misses.
    """
    family = (family or "").strip().lower()
    if not family:
        return True
    known = families(fetcher)
    if known is None:
        return True
    return family in known


def reaches_internet(fetcher) -> bool:
    """Whether the build subnet has a route to the public internet.

    Absent an answer this says YES, matching can_install: refusing everything
    because a lookup failed would take the portal down, and the machine's own
    boot report is the backstop for what this misses.
    """
    answer = current(fetcher) or {}
    if not answer.get("known"):
        return True
    return bool(answer.get("internet"))


def archive_guidance(code: str, fetcher) -> str:
    """Why software that fetches its own archive cannot be built here.

    Separate from `guidance` because the cause is different and so is the fix:
    the OS is fine and its packages install, but this technology fetches its
    software from a release host on the public internet.
    """
    answer = current(fetcher) or {}
    where = answer.get("subnet_name") or "this environment's subnet"
    return (f"{code} is not installed from a package repository — it is fetched "
            f"as a release archive from the public internet, and {where} has no "
            f"route there. Oracle's own mirrors are reachable, so package-based "
            f"software still builds here; ask for a NAT gateway to be added to "
            f"{where} if you need this one.")


def guidance(family: str, fetcher) -> str:
    """Why this OS cannot be built here and what to do — the orchestrator's own
    words, since it is the layer that can see the network."""
    answer = current(fetcher) or {}
    where = answer.get("subnet_name") or "this environment's subnet"
    family = (family or "").strip().lower()
    if family in ("debian", "suse"):
        return (f"Ubuntu and SUSE images install their software from repositories "
                f"on the public internet, and {where} has no route to it. Oracle "
                f"Linux images use Oracle's own mirrors, which this network can "
                f"reach — choose one of those, or ask for a NAT gateway to be "
                f"added to {where}.")
    if family == "rhel":
        return (f"Oracle Linux images install their software from Oracle's mirrors, "
                f"and {where} cannot reach them: there is no service gateway "
                f"covering all Oracle services, and no route to the internet.")
    return (f"{where} cannot reach the package repositories this operating system "
            f"installs from.")


def known() -> bool:
    """Whether egress has ever been read. Surfaced in the admin console so an
    inert check is visible rather than looking permissive."""
    return _cache is not None and bool(_cache.get("known"))


def reset() -> None:
    """Drop the cache (tests, and after a network change)."""
    global _cache, _fetched_at
    with _lock:
        _cache, _fetched_at = None, 0.0
