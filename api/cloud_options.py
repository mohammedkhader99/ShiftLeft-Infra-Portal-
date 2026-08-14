"""The hourly cloud-option cache.

Asks the orchestrator what the tenancy offers (it holds the credentials; the API
holds none) and writes the answer into `component_option` as rows tagged
`source="oci-live"`. The request form then reads the database — instantly, and
still working when OCI is unreachable, which a per-request live call would not be.

WHY CACHE AT ALL
----------------
Calling OCI whenever someone opens the form would stall the page on every
interaction, spend the tenancy's API budget on people who are only browsing, and
break the form outright during an OCI blip. A cloud's shape catalogue changes a
few times a year, so an hourly refresh is far more current than the hand-typed
list it replaces and costs one call an hour.

WHAT A REFRESH MAY AND MAY NOT TOUCH
------------------------------------
It replaces only rows it owns (`source="oci-live"`). The hand-curated `seed`
rows — notably the version lists, which no cloud API can answer — are never
touched by a fetch. That is what the `source` column is for.

A FAILED FETCH CHANGES NOTHING. The previous rows stay exactly as they were, and
`refreshed_at` stops advancing, so the console shows a stale timestamp rather
than an empty form. Deleting good options because the network blinked would be a
worse outcome than serving slightly old ones.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import ComponentOption

# Rows written by the fetch. Only these are ever deleted by a refresh.
LIVE_SOURCE = "oci-live"

# Options that apply to every technology rather than one: an OS image is a
# property of the machine, not of nginx. Stored with an empty technology_code,
# which component_options.options_for reads as "applies to all".
ALL_TECHNOLOGIES = ""


def refresh_interval_seconds() -> int:
    """How often to re-ask. Floored at 5 minutes: this calls a real cloud API, and
    a mis-set value of 1 would hammer the tenancy."""
    try:
        value = int(os.getenv("OCI_CATALOGUE_REFRESH_SECONDS", "3600"))
    except ValueError:
        return 3600
    return max(300, value)


def enabled() -> bool:
    """The cache runs only when the orchestrator is actually reading a cloud.
    In mock mode the fetch still works end to end (mock shapes and images), which
    is how the whole path stays testable without a tenancy."""
    return (os.getenv("OCI_CATALOGUE_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on"))


def _replace_live_rows(session: Session, rows: list[dict], now: datetime) -> int:
    """Swap this source's rows for the freshly fetched set, in one transaction.

    Delete-then-insert rather than a merge because the fetch is the whole truth
    for what it owns: a shape removed from the allowlist has to STOP being
    offered, and a merge would leave it behind.
    """
    session.execute(delete(ComponentOption).where(ComponentOption.source == LIVE_SOURCE))
    for order, row in enumerate(rows):
        session.add(ComponentOption(
            deployment_target=row.get("deployment_target", "oci"),
            technology_code=row.get("technology_code", ALL_TECHNOLOGIES),
            field=row["field"],
            value=row["value"],
            label=row.get("label") or row["value"],
            is_default=bool(row.get("is_default")),
            sort_order=order,
            source=LIVE_SOURCE,
            refreshed_at=now,
        ))
    session.commit()
    return len(rows)


def _rows_from(fetched: dict) -> list[dict]:
    """Turn the orchestrator's answer into option rows.

    Only IMAGES become a dropdown. Shapes are cached too, but as a validation
    input rather than a control — see component_options.shape_limits. Adding a
    shape dropdown alongside version, vCPU, memory and disk would put five
    controls on every component to serve a choice most requesters do not have an
    opinion about.
    """
    rows: list[dict] = []
    for index, image in enumerate(fetched.get("images") or []):
        if not image.get("ocid"):
            continue
        rows.append({
            "field": "image",
            "value": image["ocid"],
            # The OCID is unreadable; the display name is what a requester picks by.
            "label": image.get("name") or image["ocid"],
            "is_default": index == 0,
            "deployment_target": "oci",
            "technology_code": ALL_TECHNOLOGIES,
        })
    for shape in fetched.get("shapes") or []:
        rows.append({
            "field": "shape",
            "value": shape["name"],
            "label": (f"{shape['name']} ({shape['min_ocpus']}-{shape['max_ocpus']} OCPU, "
                      f"{shape['min_memory_gb']}-{shape['max_memory_gb']} GB)"),
            "deployment_target": "oci",
            "technology_code": ALL_TECHNOLOGIES,
        })
    return rows


def refresh(session: Session, fetcher) -> dict:
    """Fetch once and update the cache. `fetcher` returns the orchestrator's dict
    (injected so tests never need a running orchestrator).

    Never raises: a refresh failure must not take down the caller's background
    loop, and must leave the previous options in place.
    """
    try:
        fetched = fetcher()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"orchestrator unreachable: {exc}", "written": 0}

    if not fetched or not fetched.get("ok"):
        reason = (fetched or {}).get("reason") or "the orchestrator could not read the tenancy"
        return {"ok": False, "reason": reason, "written": 0}

    rows = _rows_from(fetched)
    if not rows:
        # Reachable, readable, and offering nothing — almost always an unset
        # allowlist. Say so instead of wiping the cache, which would turn a
        # configuration gap into data loss.
        return {
            "ok": False,
            "reason": ("nothing is allow-listed: set OCI_SHAPE_ALLOWLIST and "
                       "OCI_IMAGE_FILTER, which are empty by design so an "
                       "unconfigured portal cannot offer every shape in the tenancy"),
            "written": 0,
            "shapes_available": fetched.get("shapes_available", 0),
            "images_available": fetched.get("images_available", 0),
        }

    now = datetime.now(timezone.utc)
    written = _replace_live_rows(session, rows, now)
    return {
        "ok": True,
        "written": written,
        "images": len(fetched.get("images") or []),
        "shapes": len(fetched.get("shapes") or []),
        "shapes_available": fetched.get("shapes_available", 0),
        "images_available": fetched.get("images_available", 0),
        "mode": fetched.get("mode"),
        "refreshed_at": now.isoformat(),
    }


def last_refreshed(session: Session) -> datetime | None:
    """When the cache was last successfully written, or None if it never was."""
    return session.scalar(
        select(ComponentOption.refreshed_at)
        .where(ComponentOption.source == LIVE_SOURCE)
        .order_by(ComponentOption.refreshed_at.desc())
        .limit(1)
    )
