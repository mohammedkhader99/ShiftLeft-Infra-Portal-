"""Automatic sizing (increment 1.4).

Resolves each component's technology + size to CPU / memory / storage from the
seeded sizing anchors, and sums the environment totals. Effective-dated: the
current anchor (latest effective_from) is used (F-CAT-07 foundation).

This is authoritative server-side logic (P2); the browser only displays it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def resolve_components(components: list[dict], session: Session) -> dict:
    """Return per-component sizing plus environment totals.

    A component missing a technology/size, or with no matching anchor, is
    returned as unresolved rather than raising — the form fills in over time.
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
            "vcpu": None,
            "memory_gb": None,
            "storage_gb": None,
            "resolved": False,
        }
        if technology_code and size:
            anchor = _current_anchor(session, technology_code, size)
            if anchor is not None:
                row.update(
                    vcpu=anchor.vcpu,
                    memory_gb=anchor.memory_gb,
                    storage_gb=anchor.storage_gb,
                    resolved=True,
                )
                totals["vcpu"] += anchor.vcpu
                totals["memory_gb"] += anchor.memory_gb
                totals["storage_gb"] += anchor.storage_gb
        resolved.append(row)

    return {"components": resolved, "totals": totals}
