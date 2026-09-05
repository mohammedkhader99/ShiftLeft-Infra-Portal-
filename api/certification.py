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

from dataclasses import dataclass

import os
import re
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from db.models import (
    Blueprint,
    CertificationProof,
    ProvisionedResource,
    RecipeRefutation,
    Request,
)

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
# The recipe this certification rested on is GONE from the generated store --
# taken back by the ladder, or withdrawn by an operator. Distinct from both
# of the above on purpose: a suspended or stale blueprint has a recipe that a
# new proof can vouch for, so the sweep may bring it back. A withdrawn one has
# nothing to vouch for. Only `certify_from_proof` -- a proof of a recipe --
# ends a withdrawal, and `restore()` never touches it.
#
# Audit 12766, 2026-09-04 04:53:49: the sweep re-certified `mysql` thirty
# seconds after a draft of the same withdrawn recipe reappeared in the store
# to be proved again, on the strength of the proof the withdrawal had
# rejected. The catalogue offered a command-line client as MySQL for
# nineteen minutes. With this state the sweep had nothing to do.
WITHDRAWN = "withdrawn"

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
    """When this blueprint was last proven by a build, or None.

    A PROOF THE GATE REFUSED IS NOT A PROOF. `recipe_memory` records a
    refutation against the proof's reference when a machine passed and the
    door then refused the recipe -- the command-line client installed cleanly
    and ran nothing. That proof says the recipe installs files. It is not
    evidence for anything the catalogue may offer, and until this it was the
    "last passing proof" the sweep restored blueprints on.
    """
    refuted = select(RecipeRefutation.proof_reference).where(
        RecipeRefutation.technology_code == technology_code,
        RecipeRefutation.deployment_target == target,
        RecipeRefutation.proof_reference != "")
    row = session.scalars(
        select(CertificationProof)
        .where(CertificationProof.technology_code == technology_code)
        .where(CertificationProof.deployment_target == target)
        .where(CertificationProof.status == "passed")
        .where(~CertificationProof.reference.in_(refuted))
        .order_by(desc(CertificationProof.finished_at))
        .limit(1)
    ).first()
    return row.finished_at if row else None


def proof_in_flight(session: Session, technology_code: str, target: str) -> bool:
    """Is a machine being built right now to answer for this blueprint?

    While it is, the answer is that machine's. The sweep racing it is how a
    draft under proof was offered to requesters (audit 12766).
    """
    return session.scalar(
        select(CertificationProof.id)
        .where(CertificationProof.technology_code == technology_code)
        .where(CertificationProof.deployment_target == target)
        .where(CertificationProof.status == "running")
        .limit(1)
    ) is not None


def newest_failure_at(session: Session, resource_kind: str,
                      target: str) -> datetime | None:
    """When a request last failed and blamed this resource kind, or None.

    The same attribution as `outcomes_for_kind`, asked for a time instead of
    a verdict: a proof that passed BEFORE the failures that suspended a
    blueprint is not the evidence that answers them.
    """
    if not resource_kind:
        return None
    considered = session.scalars(
        select(Request)
        .where(Request.request_type == "create")
        .where(Request.deployment_target == target)
        .where(Request.status.in_(FAILURE_STATUSES))
        .order_by(desc(Request.id))
        .limit(SCAN_LIMIT)
    ).all()
    for req in considered:
        if blamed_kind(req.status_detail) == resource_kind:
            return req.updated_at or req.created_at
    return None


def _utc(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres aware ones. Compare alike."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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


@dataclass
class Retirement:
    """What a retirement did, and whether it actually took."""

    technology_code: str
    deployment_target: str
    removed: list          # files actually deleted from the generated store
    suspended: bool        # did the certification change state
    complete: bool         # does the orchestrator agree it is gone
    detail: str


def retire(session: Session, technology_code: str, target: str, why: str, *,
           withdraw, builds=None) -> Retirement:
    """Withdraw a recipe AND its certification, together, and check it held.

    THE SHARP EDGE THIS REMOVES, walked into on 2026-08-29. `oci-functions` was
    certified on a module that built nothing. Suspending it by hand looked like
    it worked — and `restore()` re-certified it within one poll, because the
    generated BLUEPRINT MANIFEST was still in the store, so the orchestrator went
    on answering "yes, I build oci-functions". `restore()` was behaving exactly
    as designed; the withdrawal was half done.

    A recipe is more than one file. A profile is `<code>.json`; a drafted
    blueprint's manifest is `<resource_kind>.yaml`. Removing one and not the
    other leaves a manifest with no module — which is worse than either alone,
    because it advertises a recipe that would hand Terraform an empty directory.

    AND IT VERIFIES. `builds` is what the ORCHESTRATOR says it can build, asked
    over the signed channel — the same source `restore()` consults. If the
    technology is still in that list, the retirement did NOT take, and this says
    so rather than reporting a success that a sweep will quietly undo. That is
    the whole reason this exists as one operation instead of two calls a person
    has to remember to pair.

    PASS A CALLABLE, and this asks it AFTER the files are gone. A plain set can
    only have been computed by the caller BEFORE this function removed anything,
    and a snapshot of the state before a removal cannot describe the state after
    it — so for any technology genuinely being built it reported "NOT RETIRED"
    every single time, and the only way to get a success was to retire something
    that was not there. Retiring MySQL for a re-proof on 2026-09-05 hit exactly
    that: the files were gone, the blueprint read `withdrawn`, asking again
    showed the orchestrator no longer built it, and this said the sweep was
    about to undo the lot. The tests passed because they injected `set()` — the
    one answer a real caller never holds at that moment.

    A value is still accepted, and is still worth something in one direction: a
    technology absent from a snapshot taken BEFORE the removal is certainly
    absent after it. Present in one, though, settles nothing, and this now says
    so instead of raising an alarm.

    Suspending is not deleting: the proof that earned the certification really
    happened and the history is worth keeping.
    """
    row = session.get(Blueprint, (technology_code, target))
    if row is None:
        return Retirement(technology_code, target, [], False, True,
                          f"No blueprint for {technology_code} on {target}.")

    # BOTH FILES, derived from the row rather than guessed. A caller naming them
    # by hand is how the manifest was left behind.
    wanted = {f"{technology_code}.json": ""}
    if row.resource_kind:
        wanted[f"{row.resource_kind}.yaml"] = ""
    removed = list(withdraw(wanted) or []) if withdraw else []

    suspended = withdraw_for_missing_recipe(session, technology_code, target, why)

    # DID IT HOLD? Asked HERE, after the removal, whenever the caller handed over
    # the means to ask. Absent means we could not ask, which is not the same as
    # gone; a bare value means we were told about a moment we did not choose.
    answered_after = callable(builds)
    if answered_after:
        try:
            builds = builds()
        except Exception as exc:  # noqa: BLE001 - an unreachable orchestrator is "could not ask"
            builds, answered_after = None, False
            _ = exc

    if builds is None:
        complete, detail = False, (
            f"Withdrew {len(removed)} file(s) and "
            f"{'suspended' if suspended else 'left'} the certification, but the "
            f"orchestrator could not be asked whether it still builds "
            f"{technology_code}. Check before trusting this.")
    elif technology_code in builds and not answered_after:
        # STALE BY CONSTRUCTION, so this is not evidence of failure. Saying
        # NOT RETIRED here is the false alarm that sent an operator looking for
        # a sweep that was never coming.
        complete, detail = False, (
            f"Withdrew {removed or 'no files'} and "
            f"{'suspended' if suspended else 'left'} the certification for "
            f"{technology_code}, but could not verify it: the `builds` list was "
            f"a value, which the caller must have taken BEFORE this removed "
            f"anything, so it says nothing about the state now. Pass a callable "
            f"and it will be asked afterwards.")
    elif technology_code in builds:
        complete, detail = False, (
            f"NOT RETIRED. The orchestrator still reports that it builds "
            f"{technology_code}, so the certification sweep will restore it "
            f"within one poll. Something of the recipe is still in the store; "
            f"removed so far: {removed or 'nothing'}.")
    else:
        complete, detail = True, (
            f"Retired {technology_code} on {target}: removed {removed or 'no files'} "
            f"and the orchestrator no longer builds it.")

    return Retirement(technology_code, target, removed, bool(suspended),
                      complete, detail[:500])


def withdraw_for_missing_recipe(session: Session, technology_code: str,
                                target: str, why: str) -> bool:
    """Suspend a certification whose recipe has just been withdrawn.

    A CERTIFICATION MUST NOT OUTLIVE THE RECIPE IT CERTIFIED, and on 2026-08-24
    one did. REQ-2026-0193's container proof passed and certified; the narrowing
    proof that followed failed, so the profile was withdrawn from the generated
    store — and the certification stayed. The catalogue went on claiming the
    portal could build RabbitMQ, the request passed the certification gate, and
    the orchestrator built the only thing it could with no recipe to render:

        technologies=
        --- packages ---
        --- services ---
        PORTAL: first-boot configuration finished

    A bare machine, billing, recorded as provisioned and resolved in Jira. That
    is the record and the reality disagreeing with the record believed — the
    exact failure this whole certification design exists to prevent.

    SUSPENDED, not deleted: the proof that earned it really happened and the
    history is worth keeping. What changes is that the catalogue stops offering
    it until a recipe exists and a machine proves it again.
    """
    row = session.get(Blueprint, (technology_code, target))
    if row is None or row.status == WITHDRAWN:
        return False
    # WITHDRAWN, whatever it was before. A suspension (the evidence turned
    # against it) followed by a withdrawal (the recipe is gone) ends in the
    # stronger state -- the one the sweep never brings back on its own.
    row.status = WITHDRAWN
    row.notes = (f"Withdrawn: the recipe this certification rested on was "
                 f"withdrawn. {why}")[:400]
    return True


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


def restore(session: Session, now: datetime | None = None,
            builds: set[str] | None = None) -> list[dict]:
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

    AND IT WILL NOT BRING BACK A WITHDRAWN RECIPE. Suspended and stale mean the
    recipe is there and the evidence for it ran out or turned; withdrawn means
    the recipe is gone. Nothing a sweep can see vouches for a recipe that does
    not exist -- and on 2026-09-04 (audit 12766) the sweep saw exactly enough
    to be wrong: a passing proof from the day before, and a draft of the same
    withdrawn recipe just published to the store to be proved AGAIN. It
    certified the draft. Only `certify_from_proof`, called by the ladder on a
    proof of a recipe, ends a withdrawal.

    WHAT COUNTS AS EVIDENCE, each a rule the same audit row taught:

      * not a proof a machine is still running -- that machine decides;
      * not a proof the gate refused -- excluded by `last_passing_proof`;
      * not the proof that granted the certification now suspended: a proof is
        spent by being used, and a restoration stamps `certified_at` so the
        same proof cannot restore twice;
      * not a proof older than the failures that suspended it.
    """
    from api import proof

    if not proof.enabled():
        return []

    now = now or datetime.now(timezone.utc)
    restored: list[dict] = []
    for row in session.scalars(select(Blueprint)).all():
        if row.status not in (SUSPENDED, STALE):
            continue
        code, target = row.technology_code, row.deployment_target
        if proof_in_flight(session, code, target):
            continue
        last = last_passing_proof(session, code, target)
        if last is None or proof.is_stale(last, now):
            continue
        if row.certified_at is not None and _utc(last) <= _utc(row.certified_at):
            continue
        failed_at = newest_failure_at(session, row.resource_kind, target)
        if failed_at is not None and _utc(last) <= _utc(failed_at):
            continue
        # A PASSING PROOF IS NOT A RECIPE. This looked only at proofs, so it
        # re-certified a technology whose recipe had been withdrawn from the
        # generated store minutes earlier — undoing, within one poll, the
        # suspension that take_it_back had just applied. The catalogue went back
        # to claiming RabbitMQ with nothing to build it, which is how
        # REQ-2026-0193 got a bare machine reported as provisioned.
        #
        # `builds` is what the ORCHESTRATOR says it can build, asked over the
        # signed channel — never by importing it. Absent means we could not ask,
        # and the safe direction is not to restore: a delayed restoration costs
        # a request its automatic path, while a wrong one costs a machine and
        # tells nobody.
        if builds is None or code not in builds:
            continue
        was = row.status
        row.status = "certified"
        row.certified_at = now
        row.notes = (f"Re-certified by a passing proof build on "
                     f"{last:%Y-%m-%d %H:%M} UTC, after being {was}.")[:400]
        restored.append({
            "technology": code,
            "target": target,
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
