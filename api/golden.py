"""Golden images, from captured to actually used (G2).

G1 kept the machine that passed. On its own that is a storage bill: every row it
writes says `capturing`, and nothing reads them. This module is the other half —
the two questions that turn a captured image into a faster provisioning.

    IS IT READY?     A custom image takes ten to twenty minutes to build, and
                     OCI is the only thing that knows when it is done. `promote`
                     asks, once per poll cycle, and moves the row.

    MAY I USE IT?    `usable_for` answers with an OCID or with nothing, and
                     "nothing" is an ordinary answer, not an error.

THE RULE THIS FILE EXISTS TO KEEP: a golden image is a FAST PATH, NEVER A
DEPENDENCY. Missing, still building, failed, unreachable — every one of those
falls back to installing from repositories at first boot, which is exactly what
the platform did before any of this existed. A capture that cannot be confirmed
must cost a request nothing but the speed-up it did not get.

And the machine still has to prove itself. Skipping the INSTALL does not skip
the VERIFICATION: the boot report runs the same version command against the same
ports, so an image that was captured wrong fails visibly on first use instead of
quietly shipping a broken runtime to everyone who asks for it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import GoldenImage

#: What OCI calls an image that is finished and usable.
_READY = "AVAILABLE"

#: Terminal failures. Anything else (PROVISIONING, IMPORTING) is "ask again".
_DEAD = {"DELETED", "DISABLED", "FAILED"}

#: A capture that has been building for longer than this is not coming back.
#: Ten to twenty minutes is normal; two hours is a stuck row that would
#: otherwise be re-asked about forever.
STUCK_AFTER_HOURS = 2


def promote(session, ask, *, now: datetime | None = None) -> list[dict]:
    """Move `capturing` rows to `available` or `failed`. Returns what changed.

    `ask(ocids) -> {ocid: lifecycle_state}` is injected for the same reason the
    proof's collaborators are: the interesting branches here are the ones a real
    cloud will not produce on demand.

    Never raises on a partial answer. An OCID the orchestrator could not report
    on simply stays `capturing` and is asked about again next cycle — the sweep
    is allowed to make no progress, and is not allowed to lose a row.
    """
    now = now or datetime.now(timezone.utc)
    rows = list(session.scalars(
        select(GoldenImage).where(GoldenImage.state == "capturing")))
    if not rows:
        return []

    states = ask([r.image_ocid for r in rows if r.image_ocid]) or {}
    changed: list[dict] = []

    for row in rows:
        state = (states.get(row.image_ocid) or "").upper()

        if state == _READY:
            row.state, row.detail = "available", f"Ready. OCI reports {state}."
        elif state in _DEAD:
            row.state, row.detail = "failed", f"OCI reports {state}."
        elif _stuck(row, now):
            # Not a cloud verdict — ours. A row nobody can get an answer about
            # is worse than a failed one, because it is asked about forever.
            row.detail = (f"Never became {_READY}: OCI said {state or 'nothing'} "
                          f"after {STUCK_AFTER_HOURS}h, so we stopped asking.")
            row.state = "failed"
        else:
            continue  # still building, or nobody could say. Ask again later.

        changed.append({"technology_code": row.technology_code,
                        "deployment_target": row.deployment_target,
                        "image_ocid": row.image_ocid, "state": row.state,
                        "detail": row.detail})
    return changed


def _stuck(row, now: datetime) -> bool:
    started = row.created_at
    if started is None:
        return False
    if started.tzinfo is None:                      # SQLite hands back naive
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() > STUCK_AFTER_HOURS * 3600


def usable_for(session, technology_codes, target: str) -> dict[str, str]:
    """`{technology_code: image_ocid}` for the codes that have a ready image.

    A code with no entry is the normal case and means "install it at boot", so
    callers must treat an empty mapping as ordinary rather than as a failure.

    Newest first, because the most recent proof is the one whose recipe is
    current. G3 adds expiry and supersession; until then the newest available
    image wins and older ones are simply never chosen.
    """
    codes = [c for c in {str(c) for c in technology_codes} if c]
    if not codes:
        return {}

    rows = session.scalars(
        select(GoldenImage)
        .where(GoldenImage.state == "available",
               GoldenImage.deployment_target == target,
               GoldenImage.technology_code.in_(codes))
        .order_by(GoldenImage.created_at.desc(), GoldenImage.id.desc()))

    chosen: dict[str, str] = {}
    for row in rows:
        if row.image_ocid and row.technology_code not in chosen:
            chosen[row.technology_code] = row.image_ocid
    return chosen


def one_image_per_machine(chosen: dict[str, str], components) -> dict[str, str]:
    """Refuse to pretend two golden images can boot one machine.

    A machine boots exactly ONE image. A stack asking for two technologies that
    each have a golden image cannot have both, and quietly taking the first
    would deliver a machine missing the software somebody asked for while the
    boot report — told that both were preinstalled — checked neither install.

    So: one component with an image is a fast path, more than one is not, and
    the whole request falls back to installing everything at boot. Slower, and
    correct.
    """
    codes = [str(c.get("technology_code") or "") for c in (components or [])]
    with_images = [c for c in codes if c in chosen]
    if len(with_images) != 1:
        return {}
    return {with_images[0]: chosen[with_images[0]]}


# --- G3: an image stops being offered, then stops existing --------------------
#
# G2 made images usable. Nothing made them stop. Left alone, every
# re-certification adds another ~50 GB to the tenancy and nothing ever removes
# the one it replaced.
#
# Three separate steps, deliberately not one:
#
#   SUPERSEDE  a newer proven image exists, so stop offering this one
#   EXPIRE     the proof behind it is older than a certification lasts
#   REAP       nothing can select it any more, and the grace period has passed
#
# Retiring is instant and free; deleting is destructive and cannot be undone.
# Keeping them apart is what makes the grace period possible, and the grace
# period is the whole reason a request already holding this OCID does not have
# its image deleted out from under a running apply.


def retain_days() -> int:
    """How long a retired image survives before it is deleted.

    Not zero, and not configurable to zero. A request that resolved this OCID a
    moment ago may still be mid-apply with it, and Terraform asking OCI for an
    image that was deleted between plan and apply fails a provisioning that had
    already been approved.
    """
    try:
        return max(1, int(os.getenv("GOLDEN_IMAGE_RETAIN_DAYS", "7")))
    except ValueError:
        return 7


def _retire(row, state: str, detail: str, now: datetime) -> dict:
    row.state, row.detail, row.retired_at = state, detail[:500], now
    return {"technology_code": row.technology_code,
            "deployment_target": row.deployment_target,
            "image_ocid": row.image_ocid, "state": state, "detail": row.detail}


def supersede(session, *, now: datetime | None = None) -> list[dict]:
    """Keep only the newest available image per technology and cloud.

    `usable_for` already picks the newest, so an older one is not chosen — but
    "not chosen" and "not costing anything" are different, and only this makes
    the second true.
    """
    now = now or datetime.now(timezone.utc)
    rows = list(session.scalars(
        select(GoldenImage)
        .where(GoldenImage.state == "available")
        .order_by(GoldenImage.created_at.desc(), GoldenImage.id.desc())))

    seen: set[tuple[str, str]] = set()
    changed: list[dict] = []
    for row in rows:
        key = (row.technology_code, row.deployment_target)
        if key not in seen:
            seen.add(key)          # the newest survives
            continue
        changed.append(_retire(row, "superseded",
                               "A newer proven image exists for this technology.",
                               now))
    return changed


def expire(session, *, now: datetime | None = None) -> list[dict]:
    """Retire an image whose proof is older than a certification lasts.

    THE SAME RULE THE CERTIFICATION USES, asked of the same function rather than
    re-stated here. An image is only trustworthy because a proof build vouched
    for it, so it cannot outlive the vouching — and if this file carried its own
    thirty, the two would drift the first time one of them was tuned.
    """
    from api import proof

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=proof.validity_days())
    changed: list[dict] = []
    for row in session.scalars(
            select(GoldenImage).where(GoldenImage.state == "available")):
        created = row.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            changed.append(_retire(
                row, "expired",
                f"The proof behind it is older than {proof.validity_days()} days, "
                f"so the certification it stands on has lapsed.", now))
    return changed


#: States a reap may consider. `available` and `capturing` are absent on
#: purpose — this list is the whole safety of the delete path.
REAPABLE = frozenset({"superseded", "expired", "failed"})


def reap(session, delete, *, now: datetime | None = None) -> list[dict]:
    """Delete retired images whose grace period has passed. Never raises.

    `delete(ocid) -> (ok, detail)` is injected, like every other cloud-touching
    collaborator in this codebase.

    WHAT THIS WILL NOT DO, in order of how bad it would be:

      * it never considers an `available` row, so an image a request could still
        be handed is never a candidate;
      * it only ever names an OCID THIS TABLE RECORDS. It does not enumerate the
        tenancy and delete what looks like ours — a rule that reads images by a
        tag would, one typo later, be a rule that deletes somebody else's;
      * a failed delete leaves the row exactly as it was, to be retried next
        cycle. A row marked `deleted` for an image still in the tenancy is a
        cost nobody can find again.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retain_days())

    done: list[dict] = []
    for row in session.scalars(
            select(GoldenImage).where(GoldenImage.state.in_(sorted(REAPABLE)))):
        retired = row.retired_at
        if retired is None:
            # Retired before this column existed, or a failure recorded without
            # one. Start its clock now rather than deleting it immediately.
            row.retired_at = now
            continue
        if retired.tzinfo is None:
            retired = retired.replace(tzinfo=timezone.utc)
        if retired > cutoff:
            continue                                   # still inside the grace

        if not row.image_ocid:
            # Nothing was ever created in the cloud — a capture that failed
            # before OCI accepted it. There is nothing to delete.
            row.state = "deleted"
            row.detail = "No image was ever created; nothing to delete."
            done.append({"image_ocid": "", "state": "deleted",
                         "technology_code": row.technology_code})
            continue

        try:
            ok, detail = delete(row.image_ocid)
        except Exception as exc:  # noqa: BLE001 - a reap never breaks a sweep
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            row.detail = f"Delete failed, will retry: {detail}"[:500]
            continue

        row.state = "deleted"
        row.detail = (detail or "Deleted from the cloud.")[:500]
        done.append({"image_ocid": row.image_ocid, "state": "deleted",
                     "technology_code": row.technology_code,
                     "deployment_target": row.deployment_target})
    return done
