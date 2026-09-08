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


# --- Sizing from a resolved topology (P.6, F-CAT-18) -------------------------
#
# `resolve_components` above sums every component in the request. That is the
# right total for a bill and the wrong shape for a machine: it cannot tell one
# host carrying three components from three hosts carrying one each, because
# until placement existed nothing in the request said which it was.
#
# These functions size the HOSTS instead. Two rules do the work:
#
#   Co-resident components SUM. A machine carrying PostgreSQL and Node.js must
#   run both at once, so it needs both. Taking the largest of the two — the
#   obvious shortcut, and the one this is written to prevent — produces a
#   machine that runs whichever component is bigger and starves the other.
#
#   A managed service contributes NOTHING. The cloud runs it on hardware nobody
#   here provisions, so adding its shape to a host would size and bill for a
#   machine that does not exist.

import math
import os

# Kept as a percentage rather than a multiplier because it is shown to, and set
# by, a person: "20%" needs no explanation and "1.2" does.
DEFAULT_HEADROOM_PERCENT = 20


def headroom_percent() -> int:
    """Spare capacity added on top of the summed requirement.

    Components placed on one machine peak at different moments, and a host sized
    to the exact sum has nowhere to absorb that. Configurable per environment
    (PLACEMENT_HEADROOM_PERCENT) and clamped, because a negative headroom would
    silently size a machine BELOW what its own components asked for.
    """
    try:
        from api.settings import env  # local: avoids a cycle at import time
        raw = env("PLACEMENT_HEADROOM_PERCENT", str(DEFAULT_HEADROOM_PERCENT))
    except Exception:  # noqa: BLE001 — settings unavailable is not a sizing failure
        raw = os.getenv("PLACEMENT_HEADROOM_PERCENT", str(DEFAULT_HEADROOM_PERCENT))
    try:
        return max(0, min(100, int(str(raw).strip() or DEFAULT_HEADROOM_PERCENT)))
    except (TypeError, ValueError):
        return DEFAULT_HEADROOM_PERCENT


def _with_headroom(value: int, percent: int) -> int:
    """Always rounds UP. A machine that needs 4.8 vCPU gets 5, never 4 —
    rounding a requirement down is how headroom becomes a shortfall."""
    return math.ceil(value * (100 + percent) / 100)


SHAPE_FIELDS = ("vcpu", "memory_gb", "storage_gb", "iops")


def size_hosts(hosts: list[dict], requirements: dict[tuple[str, str], dict],
               percent: int | None = None) -> dict:
    """Resolve each host in a topology to the shape it must be built at.

    `hosts` is the topology from the placement resolver: dicts of `id`,
    `host_mode` and `components`. `requirements` maps (technology_code,
    host_mode) to the recommended shape from host_mode_requirement.

    Recommended is summed rather than minimum: the minimum is the floor below
    which a component cannot run at all, and building every machine at the edge
    of not working is not a sizing policy.

    A component with no requirement row leaves the host UNRESOLVED and names it.
    Treating a missing requirement as zero would quietly under-size the machine
    and produce an estimate for hardware that cannot carry the workload — the
    exact silent failure this phase exists to end.
    """
    percent = headroom_percent() if percent is None else max(0, min(100, percent))
    sized: list[dict] = []
    totals = dict.fromkeys(SHAPE_FIELDS, 0)
    machines = 0

    for host in hosts:
        components = list(host.get("components") or ())
        mode = host.get("host_mode")
        row = {
            "host_id": host.get("id"),
            "host_mode": mode,
            "components": components,
            "resolved": False,
            "base": None,
            "headroom_percent": percent,
            "missing": [],
        }
        row.update(dict.fromkeys(SHAPE_FIELDS, None))

        if mode == "managed":
            # Not a machine. Recorded so the topology stays complete, and
            # explicitly zero-contributing so nobody has to infer it.
            row["resolved"] = True
            row["note"] = ("Run by the cloud. No machine is provisioned and "
                           "nothing is added to the totals.")
            sized.append(row)
            continue

        base = dict.fromkeys(SHAPE_FIELDS, 0)
        missing = []
        for component in components:
            requirement = requirements.get((component, mode))
            if requirement is None:
                missing.append(component)
                continue
            for field in SHAPE_FIELDS:
                base[field] += int(requirement.get(f"recommended_{field}", 0))

        if missing:
            row["missing"] = missing
            row["note"] = (
                "Cannot be sized: no requirement is recorded for "
                + ", ".join(missing)
                + f" as {mode}. Sizing it anyway would build a machine too "
                  "small for what was asked for.")
            sized.append(row)
            continue

        row["base"] = base
        row["resolved"] = True
        for field in SHAPE_FIELDS:
            row[field] = _with_headroom(base[field], percent)
            totals[field] += row[field]
        machines += 1
        sized.append(row)

    return {
        "hosts": sized,
        "totals": totals,
        "machine_count": machines,
        "headroom_percent": percent,
        "resolved": all(h["resolved"] for h in sized),
    }


def load_requirements(session: Session, sizes: dict[str, str]
                      ) -> dict[tuple[str, str], dict]:
    """The current requirement per (technology, host mode), for the size each
    component was actually requested at.

    `sizes` maps technology_code -> size, because a request may well ask for a
    medium database beside a small runtime. Keying the result on (code, host
    mode) alone is only safe BECAUSE the size is pinned here: doing the lookup
    without it lets one size silently overwrite another, and the machine gets
    built to whichever row happened to be read last.

    Effective-dated like the sizing anchors: rows are read oldest first so the
    newest wins, and a superseded row stays readable so an older request can
    still be explained.
    """
    from db.models import HostModeRequirement

    if not sizes:
        return {}

    rows = session.scalars(
        select(HostModeRequirement)
        .where(HostModeRequirement.technology_code.in_(list(sizes)))
        .order_by(HostModeRequirement.effective_from.asc())
    ).all()

    current: dict[tuple[str, str], dict] = {}
    for row in rows:  # ascending, so the last write is the newest
        if row.size != sizes.get(row.technology_code):
            continue
        current[(row.technology_code, row.host_mode)] = {
            "size": row.size,
            "recommended_vcpu": row.recommended_vcpu,
            "recommended_memory_gb": row.recommended_memory_gb,
            "recommended_storage_gb": row.recommended_storage_gb,
            "recommended_iops": row.recommended_iops,
            "minimum_vcpu": row.minimum_vcpu,
            "minimum_memory_gb": row.minimum_memory_gb,
            "minimum_storage_gb": row.minimum_storage_gb,
            "minimum_iops": row.minimum_iops,
        }
    return current
