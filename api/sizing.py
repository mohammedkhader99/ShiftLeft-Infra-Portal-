"""Automatic sizing (increment 1.4).

Resolves each component's technology + size to CPU / memory / storage from the
seeded sizing anchors, and sums the environment totals. Effective-dated: the
current anchor (latest effective_from) is used (F-CAT-07 foundation).

This is authoritative server-side logic (P2); the browser only displays it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api import component_options
from db.models import SizingAnchor, Technology


def _current_anchor(session: Session, technology_code: str, size: str) -> SizingAnchor | None:
    tech = session.scalar(select(Technology).where(Technology.code == technology_code))
    if tech is None:
        return None
    return session.scalar(
        select(SizingAnchor)
        .where(SizingAnchor.technology_id == tech.id, SizingAnchor.size == size)
        .order_by(SizingAnchor.effective_from.desc())
    )


def _explicit(component: dict, field: str) -> int | None:
    """An explicitly chosen number from the component detail form, or None.

    Blank strings arrive from form fields the requester never touched and mean
    "not chosen", not zero.
    """
    raw = component.get(field)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_components(components: list[dict], session: Session) -> dict:
    """Return per-component sizing plus environment totals.

    The size anchor supplies the shape, and an explicitly chosen vCPU / memory /
    disk from the component detail form OVERRIDES it. That order matters in both
    directions: a request raised before the form existed carries no explicit
    values and resolves exactly as it always did, while a request that names its
    own shape is priced on the shape it names — so the figure the approver signs
    off is the figure the machine gets built to.
    """
    resolved: list[dict] = []
    totals = {"vcpu": 0, "memory_gb": 0, "storage_gb": 0}

    for component in components:
        technology_code = (component.get("technology_code") or "").strip()
        size = (component.get("size") or "").strip()
        tech = (
            session.scalar(select(Technology).where(Technology.code == technology_code))
            if technology_code
            else None
        )
        row = {
            "technology_code": technology_code or None,
            "technology_name": tech.name if tech else technology_code or None,
            "size": size or None,
            "version": (component.get("version") or "").strip() or None,
            "image": (component.get("image") or "").strip() or None,
            "vcpu": None,
            "memory_gb": None,
            "storage_gb": None,
            "resolved": False,
            # True when the shape is not the size's anchor — surfaced to the
            # approver so a non-standard machine is visible before it is approved.
            "custom_shape": False,
        }
        anchor = (
            _current_anchor(session, technology_code, size)
            if technology_code and size
            else None
        )
        if anchor is not None:
            row.update(vcpu=anchor.vcpu, memory_gb=anchor.memory_gb,
                       storage_gb=anchor.storage_gb)
        for field in ("vcpu", "memory_gb", "storage_gb"):
            chosen = _explicit(component, field)
            if chosen is not None:
                row[field] = chosen

        if all(row[f] is not None for f in ("vcpu", "memory_gb", "storage_gb")):
            row["resolved"] = True
            # One definition of "non-standard", shared with the form's own
            # helper — see component_options.is_custom_shape.
            row["custom_shape"] = component_options.is_custom_shape(
                session, technology_code, row)
            for field in ("vcpu", "memory_gb", "storage_gb"):
                totals[field] += row[field]
        resolved.append(row)

    return {"components": resolved, "totals": totals}
