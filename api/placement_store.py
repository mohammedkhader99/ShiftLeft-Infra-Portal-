"""Recording a placement decision, and never losing the one it replaced (P.8).

A placement is not a UI preference. It determines how many machines get built,
what they cost, and which Terraform modules the orchestrator selects — so it is
a decision, and decisions here are recorded the way every other privileged thing
is: append-only, with who and when (ARCHITECTURE.md P4).

WHY THE ESTIMATE IS STORED BESIDE THE TOPOLOGY. Rates move. OCI's live pricing
is fetched hourly, and recomputing a six-week-old request today produces a
number nobody ever approved. The figure that reached Jira is part of the record,
not something to be derived again later — an audit trail that reconstructs a
different answer than the one that was signed off is not an audit trail.

WHY CHANGING A PLACEMENT WRITES A ROW RATHER THAN EDITING ONE. "Which layout was
this approved as?" must stay answerable after somebody changes their mind. The
superseded row keeps its topology, its sizing and its price; the new one carries
the next version number. Nothing is overwritten and nothing is deleted.

WHY A FOLLOW-UP REQUEST IS CONSTRAINED BY THE FIRST. An environment built on
virtual machines has no cluster to deploy onto. Offering "deploy to an existing
cluster" when resizing it describes something that cannot happen, and the
requester finds out only when provisioning fails.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import RequestPlacement

# Host modes a follow-up may always use, whatever the environment was built on:
# a managed service brings its own hosting, so adding one to a VM estate asks
# nothing of the machines already there.
ALWAYS_PERMITTED = frozenset({"managed"})


def current_placement(session: Session, request_id: int) -> RequestPlacement | None:
    """The placement in force for this request, or None if none was ever made.

    "In force" is the row nothing has superseded — not merely the highest
    version, because reading the newest row would silently resurrect a placement
    that had been withdrawn.
    """
    return session.scalar(
        select(RequestPlacement)
        .where(RequestPlacement.request_id == request_id,
               RequestPlacement.superseded_at.is_(None))
        .order_by(RequestPlacement.version.desc())
    )


def placement_history(session: Session, request_id: int) -> list[RequestPlacement]:
    """Every placement this request has had, oldest first."""
    return list(session.scalars(
        select(RequestPlacement)
        .where(RequestPlacement.request_id == request_id)
        .order_by(RequestPlacement.version.asc())
    ).all())


def record_placement(
    session: Session,
    request_id: int,
    option_key: str,
    topology: dict,
    sizing: dict | None = None,
    estimate: dict | None = None,
    actor: str = "",
) -> RequestPlacement:
    """Record a chosen placement, superseding whatever it replaces.

    The previous row is stamped rather than removed, so the history reads as a
    sequence of decisions rather than a single mutable field whose earlier
    values are gone.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    previous = current_placement(session, request_id)

    if previous is not None:
        previous.superseded_at = now

    placement = RequestPlacement(
        request_id=request_id,
        version=(previous.version + 1) if previous else 1,
        option_key=option_key,
        topology=topology or {},
        sizing=sizing or {},
        estimate=estimate or {},
        created_at=now,
        created_by=actor or "",
    )
    session.add(placement)
    session.flush()
    return placement


def host_modes_used(topology: dict | None) -> frozenset[str]:
    """The host modes a recorded topology actually used."""
    if not topology:
        return frozenset()
    return frozenset(
        host.get("host_mode")
        for host in topology.get("hosts", ())
        if host.get("host_mode")
    )


def permitted_host_modes(prior: RequestPlacement | None) -> frozenset[str] | None:
    """Host modes a follow-up request against this environment may use.

    None means "no constraint" — nothing has been placed yet, so every option is
    open. Otherwise a follow-up is held to what the environment is actually made
    of, plus managed services, which bring their own hosting and so can be added
    to anything.
    """
    if prior is None:
        return None
    used = host_modes_used(prior.topology)
    if not used:
        return None
    return used | ALWAYS_PERMITTED


def filter_options(options: list, prior: RequestPlacement | None) -> list:
    """Mark options a follow-up cannot have, with the reason.

    Refused rather than removed, for the reason the whole phase keeps returning
    to: an option that silently disappears is indistinguishable from a portal
    that is broken. The requester is told the environment was built on machines,
    which is a fact they can act on.
    """
    permitted = permitted_host_modes(prior)
    if permitted is None:
        return options

    built_as = ", ".join(sorted(host_modes_used(prior.topology)))
    out = []
    for option in options:
        modes = {host.host_mode for host in option.hosts}
        unavailable = modes - permitted
        if not unavailable:
            out.append(option)
            continue
        reason = (
            f"This environment was built on {built_as}. "
            f"{', '.join(sorted(unavailable))} cannot be added to it now — "
            f"changing how an environment is hosted is a rebuild, not a resize.")
        out.append(type(option)(
            key=option.key, title=option.title, summary=option.summary,
            hosts=option.hosts, eligible=False,
            reasons=tuple(option.reasons) + (reason,),
        ))
    return out
