"""How a requested technology actually gets delivered (F-CAT).

The catalogue advertises far more than the orchestrator can build. A request for
"Kafka on OCI" is validated, priced, approved and audited — but if no certified
blueprint exists for that pair, the provisioner has no recipe and a human on the
infrastructure team does the real work. Requesters could not see that distinction
anywhere, so the catalogue promised more than the platform delivers (see
GAP-ANALYSIS.md §2).

This module answers that question, and the answer now comes from ONE place: the
**blueprint registry**.

    fulfilment_for(technology, target) -> {"mode": "automated"|"manual", "reason": ...}

* **automated** — a certified blueprint exists for this (technology, target), so
  the orchestrator builds it with no human step.
* **manual** — no certified blueprint. The portal governs the request end to end
  (validation, cost, approval, audit), then the infrastructure team fulfils it.

Why the registry rather than rules in code: certifying a blueprint on the
Blueprints page is exactly the act that makes a technology automated, so the
badge must follow it. When these were two separate sources they drifted — a
blueprint was certified and the form still said "manual".
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Blueprint

# Technologies with a first-boot configuration template (GAP-ANALYSIS step 4).
# Mirrors orchestrator/configure.TEMPLATES — a test asserts the two stay in step.
# Having a template does NOT make a technology automated; it only sharpens the
# reason shown to the requester.
CONFIGURABLE_CODES = {"nginx", "apache", "redis7", "java21", "python312", "nodejs20"}

# Proven on a real VM. Mirrors orchestrator/configure.VERIFIED_CODES, where the
# evidence for each entry is recorded; a test asserts the two stay in step.
CONFIG_VERIFIED_CODES: set[str] = {"nginx", "redis7"}

_REASON_MANUAL = (
    "The portal validates, prices, approves and audits this request, then the "
    "infrastructure team provisions it. It is not built automatically."
)
_REASON_CONFIG_PENDING = (
    "A first-boot configuration exists for this technology but no certified "
    "blueprint delivers it here yet, so the infrastructure team still fulfils it. "
    "The portal validates, prices, approves and audits the request as normal."
)

_CACHE_TTL = 3.0  # seconds — brief, so certifying shows up almost immediately
_cache: dict = {"at": 0.0, "pairs": set()}


def certified_pairs(session: Session | None = None) -> set[tuple[str, str]]:
    """(technology, target) pairs that have a CERTIFIED blueprint.

    Cached briefly so building a 46-technology catalogue doesn't hit the database
    per row. Any error returns the last known set rather than an empty one: an
    empty set would silently mark the whole catalogue manual.
    """
    if session is not None:
        rows = session.scalars(
            select(Blueprint).where(Blueprint.status == "certified")
        ).all()
        return {(r.technology_code, r.deployment_target) for r in rows}

    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["pairs"]
    try:
        from db.session import SessionLocal
        own = SessionLocal()
        try:
            rows = own.scalars(
                select(Blueprint).where(Blueprint.status == "certified")
            ).all()
            pairs = {(r.technology_code, r.deployment_target) for r in rows}
        finally:
            own.close()
    except Exception:  # noqa: BLE001 — never let a lookup blank the catalogue
        _cache["at"] = now
        return _cache["pairs"]
    _cache["at"], _cache["pairs"] = now, pairs
    return pairs


def invalidate_cache() -> None:
    """Drop the cache so a just-certified blueprint shows immediately."""
    _cache["at"] = 0.0


def fulfilment_for(technology, target: str | None,
                   certified: set[tuple[str, str]] | None = None) -> dict:
    """Whether `technology` on `target` is provisioned automatically.

    `technology` is a db.models.Technology (or anything exposing `.code`).
    `certified` may be supplied to avoid a lookup per row. Returns
    {"mode", "reason"} — never raises, so it is safe while rendering a catalogue.
    """
    code = (getattr(technology, "code", "") or "").strip()
    tgt = (target or "").strip().lower()
    pairs = certified_pairs() if certified is None else certified

    if (code, tgt) in pairs:
        return {"mode": "automated",
                "reason": ("A certified blueprint builds this automatically on "
                           f"{tgt}. No infrastructure-team step is needed.")}
    if code in CONFIGURABLE_CODES:
        return {"mode": "manual", "reason": _REASON_CONFIG_PENDING}
    return {"mode": "manual", "reason": _REASON_MANUAL}


def automated_targets(technology, targets: list[str],
                      certified: set[tuple[str, str]] | None = None) -> list[str]:
    """Of `targets`, those where this technology is provisioned automatically."""
    pairs = certified_pairs() if certified is None else certified
    return [t for t in targets
            if fulfilment_for(technology, t, pairs)["mode"] == "automated"]
