"""Running a proof build, and deciding what it means (C2).

The RULES a proof must obey live in `common/proof_rules.py`, because the
orchestrator re-verifies them itself rather than trusting this service — the same
stance it takes on a Jira approval. What lives here is the part only the portal
can do: knowing the tier list, recording the attempt, and reading a history of
proofs as a certification.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from common.proof_rules import (MARKER, CostVerdict, ProofRefused, check_cost,
                                configured_sandbox_tier, cost_cap, enabled,
                                is_proof_reference, may_destroy)
from db.models import ENVIRONMENT_TIERS

__all__ = ["MARKER", "CostVerdict", "ProofRefused", "check_cost", "cost_cap",
           "enabled", "is_proof_reference", "may_destroy", "sandbox_tier",
           "preflight", "new_reference", "validity_days", "is_stale"]


def sandbox_tier() -> str:
    """The configured sandbox tier, checked against the tiers that exist.

    common/ can only ask "is this the tier I was configured for". Here we can
    also ask whether it is a tier at all — a typo in the setting would otherwise
    become a tier name no request ever matches, and the runner would look idle
    rather than misconfigured.
    """
    tier = configured_sandbox_tier()
    if tier not in ENVIRONMENT_TIERS:
        raise ProofRefused(
            f"CERTIFICATION_SANDBOX_TIER is {tier!r}, which is not an environment "
            f"tier. Known tiers: {', '.join(ENVIRONMENT_TIERS)}.")
    return tier


def preflight() -> str:
    """Everything that must be true before a proof may start. Returns the tier.

    Raises ProofRefused naming what is missing, so the reason reaches the Admin
    console instead of the runner appearing idle.
    """
    if not enabled():
        raise ProofRefused(
            "Proof builds are disabled. Set CERTIFICATION_PROOF_ENABLED=true to "
            "allow them — each one creates real, billable infrastructure.")
    return sandbox_tier()


def new_reference(technology_code: str, when: datetime | None = None) -> str:
    """A proof's own reference, carrying the marker that scopes its teardown."""
    when = when or datetime.now(timezone.utc)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", technology_code).strip("-").upper()[:30] or "X"
    return f"{MARKER}{slug}-{when:%Y%m%dT%H%M%S}"


def validity_days() -> int:
    """How long a passing proof keeps a blueprint certified (ARCHITECTURE.md P8).

    Thirty days, decided 2026-08-21: long enough that a monthly cycle keeps
    everything green, short enough to catch a cloud retiring a shape or version
    underneath us — which happened twice in one week.
    """
    try:
        return max(1, int(os.getenv("CERTIFICATION_VALIDITY_DAYS", "30")))
    except ValueError:
        return 30


def is_stale(last_passed_at: datetime | None, now: datetime | None = None,
             days: int | None = None) -> bool:
    """Whether a certification has aged out of its proof.

    None — never proven — is stale. A blueprint certified by hand and never
    exercised is exactly the state that let oci-oke fail four times while staying
    on offer.
    """
    if last_passed_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if last_passed_at.tzinfo is None:
        last_passed_at = last_passed_at.replace(tzinfo=timezone.utc)
    return (now - last_passed_at) > timedelta(days=days or validity_days())


# --- Running one proof -------------------------------------------------------

@dataclass
class ProofOutcome:
    reference: str
    status: str          # passed | failed | refused | abandoned
    detail: str
    monthly: float | None = None


def run_proof(session, blueprint, *, post, price, verify, now=None) -> ProofOutcome:
    """Build a blueprint, check it, tear it down, and record what happened.

    The four collaborators are injected rather than imported so a test can drive
    the whole path — including the teardown failure, which is the outcome that
    matters most and the one a real orchestrator will almost never produce on
    demand:

        post(path, payload)   -> (ok: bool, detail: str)   the signed handoff
        price(components)     -> monthly cost or None      the plan's price
        verify(reference)     -> (healthy: bool, detail)   resource_state / boot

    ORDER MATTERS. The cost cap is checked before anything is built, and the
    record is written before the build starts — a proof that dies mid-flight must
    leave a row saying so, not nothing.
    """
    from db.models import CertificationProof

    now = now or datetime.now(timezone.utc)
    reference = new_reference(blueprint.technology_code, now)

    record = CertificationProof(
        technology_code=blueprint.technology_code,
        deployment_target=blueprint.deployment_target,
        resource_kind=blueprint.resource_kind or "",
        reference=reference, status="running", started_at=now,
    )
    session.add(record)
    session.commit()

    def finish(status: str, detail: str, monthly=None) -> ProofOutcome:
        record.status = status
        record.detail = detail[:500]
        record.finished_at = datetime.now(timezone.utc)
        if monthly is not None:
            record.planned_monthly = monthly
        session.commit()
        return ProofOutcome(reference, status, detail, monthly)

    try:
        tier = preflight()
    except ProofRefused as exc:
        return finish("refused", str(exc))

    components = [{"technology_code": blueprint.technology_code, "size": "small"}]
    payload = {
        "proof": True,
        "reference": reference,
        # /provision and /apply both require this. The proof's own reference is
        # the natural key: one proof, one build, so a retried handoff cannot
        # quietly build a second copy of something already running.
        "idempotency_key": reference,
        "policy_input": {
            "deployment_target": blueprint.deployment_target,
            "environment_tier": tier,
            "environment_name": reference.lower(),
            "components": components,
        },
    }

    # THE COST CEILING, before anything is built.
    verdict = check_cost(price(components))
    record.cost_cap = verdict.cap
    if not verdict.allowed:
        return finish("refused", verdict.reason, verdict.monthly)

    ok, detail = post("/provision", payload)
    if not ok:
        return finish("failed", f"Provision refused: {detail}", verdict.monthly)

    ok, detail = post("/apply", payload)
    if not ok:
        # It may still have created something before failing, so tear down
        # regardless and let the teardown result speak for itself.
        torn, tdetail = post("/destroy", payload)
        if not torn:
            return finish("abandoned",
                          f"Apply failed ({detail}) AND teardown failed ({tdetail}). "
                          f"Something may still exist under {reference}.",
                          verdict.monthly)
        return finish("failed", f"Apply failed: {detail}", verdict.monthly)

    healthy, vdetail = verify(reference)

    # ALWAYS tear down, pass or fail. A proof that leaves a resource behind is a
    # failed proof however healthy the resource was (ARCHITECTURE.md §4).
    torn, tdetail = post("/destroy", payload)
    if not torn:
        return finish("abandoned",
                      f"Built and verified={healthy}, but teardown failed: {tdetail}. "
                      f"Something may still be running under {reference}.",
                      verdict.monthly)

    if not healthy:
        return finish("failed", f"Built, but did not verify healthy: {vdetail}",
                      verdict.monthly)
    return finish("passed", f"Built, verified, and torn down. {verdict.reason}",
                  verdict.monthly)
