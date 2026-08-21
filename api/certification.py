"""Withdraw certification from a blueprint the evidence says is broken (C1).

WHY THIS EXISTS. Certification is a promise: the user's standing rule is that
"once you certify the component it should not fail when the user selects the
component". Nothing enforced it. `oci-oke` was certified by hand on 15 August and
then failed four consecutive real requests — REQ-2026-0148, 0149, 0150, 0151 —
while remaining on offer the whole time. `postgres16` did the same over 0154-0158.
A person had to notice and act, and nobody did.

This closes the loop with signals the portal already collects. It only ever
WITHDRAWS. Re-certifying on a success would let one lucky build undo a real
defect, and would be the same "it worked once" reasoning that certified those two
blueprints in the first place. Coming back requires a human, or the proof build
of increment C2.

ATTRIBUTION IS THE HARD PART, and it is done by resource kind rather than by
guessing:

  * a FAILURE counts only when the message names what failed —
    "Terraform apply failed for oci-oke: ..." — so REQ-2026-0149's
    "unreachable: timed out" blames nothing. That was the orchestrator timing
    out, not the blueprint, and suspending a blueprint for it would be a new
    defect rather than a fix.
  * a SUCCESS counts when the request actually recorded a resource of that kind,
    read from ProvisionedResource — the same source the teardown path trusts.

Both key on resource kind, so a two-component stack attributes to the half that
actually broke instead of blaming both.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from db.models import Blueprint, CertificationProof, ProvisionedResource, Request

# "Terraform apply failed for oci-postgres: ..." — the orchestrator writes the
# resource kind it was building. Anchored on the whole marker so a stray
# "failed for" in a provider message cannot be mistaken for attribution.
_BLAME = re.compile(r"failed for ([a-z0-9][a-z0-9-]{1,30}):")

FAILURE_STATUSES = ("apply-failed", "verify-failed")

# 'decommissioned' counts as a SUCCESS. A request that built cleanly and was
# later torn down still proves the recipe works — tearing an environment down is
# normal lifecycle, not a verdict on the blueprint.
#
# Caught on real data: REQ-2026-0153 built an OKE cluster in 15 minutes, was
# decommissioned by REQ-2026-0161, and with only 'provisioned' counted its
# success disappeared. oci-oke read as three straight failures and would have
# been suspended for a recipe that demonstrably works.
SUCCESS_STATUSES = ("provisioned", "decommissioned")

# Set on the row instead of deleting it, unlike the manual decertify endpoint.
# Deleting would discard the reason, and the reason is the entire point: an admin
# looking at the console needs to know the portal withdrew this and why.
SUSPENDED = "suspended"

# Certified once, never re-proven since. Distinct from SUSPENDED, which means the
# evidence turned against it: stale means the evidence simply ran out.
STALE = "stale"

# How many recent finished requests to look back through when attributing
# outcomes. Generous enough that a busy week of unrelated builds cannot hide a
# blueprint's own record, bounded so the sweep stays cheap forever.
SCAN_LIMIT = 200


def failure_threshold() -> int:
    """Consecutive attributable failures before certification is withdrawn.

    Three, not one: a single failure is as likely to be a bad request, a quota,
    or a cloud having a moment. Three in a row with no success between them is a
    property of the blueprint.
    """
    try:
        return max(2, int(os.getenv("CERTIFICATION_FAILURE_THRESHOLD", "3")))
    except ValueError:
        return 3


def blamed_kind(detail: str | None) -> str | None:
    """The resource kind a failure message names, or None if it names none.

    None means "this failure is not evidence about any blueprint" — a timeout, an
    unreachable orchestrator, a signature problem. Those are platform faults and
    must not count against a recipe.
    """
    match = _BLAME.search(detail or "")
    return match.group(1) if match else None


def outcomes_for_kind(session: Session, resource_kind: str, target: str,
                      limit: int) -> list[tuple[str, bool, str]]:
    """Most recent attributable outcomes for one resource kind on one cloud.

    Returns [(reference, succeeded, detail)], newest first. Only requests that
    say something about THIS kind are included; everything else is silence, not
    evidence.
    """
    if not resource_kind:
        return []

    built = {
        row.reference
        for row in session.scalars(
            select(ProvisionedResource).where(ProvisionedResource.kind == resource_kind))
    }

    # Bounded scan. This sweep runs every poll cycle for every certified
    # blueprint, so loading the entire request history each time would grow into
    # real work as the portal is used. If the last SCAN_LIMIT finished requests
    # on this cloud contain fewer than `limit` attributable outcomes, there is no
    # verdict to reach anyway.
    considered = session.scalars(
        select(Request)
        .where(Request.request_type == "create")
        .where(Request.deployment_target == target)
        .where(Request.status.in_(SUCCESS_STATUSES + FAILURE_STATUSES))
        .order_by(desc(Request.id))
        .limit(SCAN_LIMIT)
    ).all()

    outcomes: list[tuple[str, bool, str]] = []
    for req in considered:
        if req.status in SUCCESS_STATUSES:
            # The proof is the resource row, whatever its lifecycle state now.
            if req.reference in built:
                outcomes.append((req.reference, True, ""))
        elif blamed_kind(req.status_detail) == resource_kind:
            outcomes.append((req.reference, False, (req.status_detail or "")[:160]))
        if len(outcomes) >= limit:
            break
    return outcomes


def should_withdraw(outcomes: list[tuple[str, bool, str]],
                    threshold: int) -> bool:
    """True when the last `threshold` attributable outcomes were all failures.

    Fewer than `threshold` outcomes is not evidence — a blueprint certified an
    hour ago and tried once has no track record, and treating that as a verdict
    would suspend things nobody has exercised yet.
    """
    if len(outcomes) < threshold:
        return False
    return all(not ok for _ref, ok, _detail in outcomes[:threshold])


def last_passing_proof(session: Session, technology_code: str,
                       target: str) -> datetime | None:
    """When this blueprint was last proven by a build, or None."""
    row = session.scalars(
        select(CertificationProof)
        .where(CertificationProof.technology_code == technology_code)
        .where(CertificationProof.deployment_target == target)
        .where(CertificationProof.status == "passed")
        .order_by(desc(CertificationProof.finished_at))
        .limit(1)
    ).first()
    return row.finished_at if row else None


def proofs_began(session: Session) -> datetime | None:
    """When proof builds actually started running, or None if they never have.

    The staleness clock hangs off this. Gating expiry on "proofs are enabled" was
    wrong and took the whole catalogue offline the moment they were switched on:
    seven blueprints certified by hand had no proof history, so all seven aged out
    at once, retroactively, for a feature that had not yet run a single build.

    A blueprint cannot have failed to earn a proof before proofs existed.
    """
    row = session.scalars(
        select(CertificationProof).order_by(CertificationProof.started_at).limit(1)
    ).first()
    return row.started_at if row else None


def expire(session: Session, now: datetime | None = None) -> list[dict]:
    """Mark certifications whose proof has aged out (ARCHITECTURE.md P8).

    ONLY WHEN PROOF BUILDS ARE RUNNING. With them switched off there is no clock
    to age against: applying the 30-day rule anyway would take the entire
    catalogue offline on day 31 for a feature nobody enabled, which is a worse
    outcome than the staleness it was meant to prevent.
    """
    from api import proof

    if not proof.enabled():
        return []

    # NOTHING AGES OUT OF A FEATURE THAT HAS NEVER RUN. Without this the day
    # proofs were switched on, every blueprint certified by hand became stale at
    # once — punished retroactively for missing proofs that were never possible.
    started = proofs_began(session)
    if started is None:
        return []

    now = now or datetime.now(timezone.utc)
    expired: list[dict] = []
    for row in session.scalars(select(Blueprint)).all():
        if row.status != "certified":
            continue
        last = last_passing_proof(session, row.technology_code, row.deployment_target)
        # A blueprint with no proof of its own is measured from when proofs
        # BEGAN, so it gets the same validity window as everything else to earn
        # its first one, rather than being stale on arrival.
        if not proof.is_stale(last or started, now):
            continue
        row.status = STALE
        row.notes = (f"Certification expired: no passing proof build in "
                     f"{proof.validity_days()} days"
                     + (f" (last passed {last:%Y-%m-%d})" if last else
                        " — it has never been proven by a build"))[:400]
        expired.append({
            "technology": row.technology_code,
            "target": row.deployment_target,
            "last_passed_at": last.isoformat() if last else None,
            "reason": row.notes,
        })
    return expired


CERTIFIED_BY_RUNNER = "certification-runner"


def certify_from_proof(session: Session, technology_code: str, target: str,
                       blueprint_ref: str, resource_kind: str,
                       proof_reference: str, version: str = "") -> Blueprint:
    """Certify a blueprint on the strength of a proof build that passed.

    The agent certifying itself, added 2026-08-21 at the reviewer's repeated
    instruction. What makes it more than a rubber stamp is that it cannot be
    reached without a passing proof: the thing was built, verified healthy,
    priced under the cap and destroyed again.

    `certified_by` records the RUNNER, never a person. An audit trail that
    attributed this to a human would be a lie, and the one question anybody will
    ask later is which of these a person approved.
    """
    row = session.get(Blueprint, (technology_code, target))
    if row is None:
        row = Blueprint(technology_code=technology_code, deployment_target=target)
        session.add(row)
    row.blueprint_ref = blueprint_ref
    row.resource_kind = resource_kind
    row.version = version or row.version or ""
    row.status = "certified"
    row.certified_by = CERTIFIED_BY_RUNNER
    row.certified_at = datetime.now(timezone.utc)
    row.notes = (f"Certified automatically by proof build {proof_reference}: "
                 f"built, verified healthy and destroyed.")[:400]
    return row


def restore(session: Session, now: datetime | None = None) -> list[dict]:
    """Re-certify a blueprint whose proof build passed (ARCHITECTURE.md P8).

    THE WAY BACK that C1 promised. A blueprint the portal suspended or expired
    returns to service when a proof build provisions it, verifies it healthy,
    prices it within budget and destroys it again — the evidence C1 said it would
    need, rather than someone deciding it is probably fine now.

    IT WILL NOT CERTIFY SOMETHING NEVER CERTIFIED. A passing proof says a recipe
    BUILDS; it says nothing about whether it is safe, correctly scoped, or
    something this organisation wants to offer. The OKE bastion carried
    `assign_public_ip = true` and built perfectly for months. First certification
    stays a human act; only coming BACK is earned by proof.
    """
    from api import proof

    if not proof.enabled():
        return []

    now = now or datetime.now(timezone.utc)
    restored: list[dict] = []
    for row in session.scalars(select(Blueprint)).all():
        if row.status not in (SUSPENDED, STALE):
            continue
        # This used to require a human fingerprint (certified_by) before a proof
        # could bring a blueprint back — first certification stayed a human act.
        # That requirement was removed on 2026-08-21 with §7: a passing proof is
        # now sufficient in both directions.
        last = last_passing_proof(session, row.technology_code, row.deployment_target)
        if last is None or proof.is_stale(last, now):
            continue
        was = row.status
        row.status = "certified"
        row.notes = (f"Re-certified by a passing proof build on "
                     f"{last:%Y-%m-%d %H:%M} UTC, after being {was}.")[:400]
        restored.append({
            "technology": row.technology_code,
            "target": row.deployment_target,
            "was": was,
            "proved_at": last.isoformat(),
        })
    return restored


def review(session: Session, threshold: int | None = None) -> list[dict]:
    """Withdraw certification wherever the evidence has turned against it.

    Returns one dict per withdrawal, for auditing by the caller. Makes no
    decision in the other direction — see the module docstring.
    """
    threshold = threshold or failure_threshold()
    withdrawn: list[dict] = []

    for row in session.scalars(select(Blueprint)).all():
        if row.status != "certified" or not row.resource_kind:
            continue
        outcomes = outcomes_for_kind(session, row.resource_kind,
                                     row.deployment_target, threshold)
        if not should_withdraw(outcomes, threshold):
            continue

        refs = [ref for ref, _ok, _d in outcomes[:threshold]]
        reason = (f"Certification withdrawn automatically after {threshold} "
                  f"consecutive failures: {', '.join(refs)}. "
                  f"Last: {outcomes[0][2][:150]}")
        row.status = SUSPENDED
        row.notes = reason[:400]
        withdrawn.append({
            "technology": row.technology_code,
            "target": row.deployment_target,
            "resource_kind": row.resource_kind,
            "references": refs,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        })
    return withdrawn
