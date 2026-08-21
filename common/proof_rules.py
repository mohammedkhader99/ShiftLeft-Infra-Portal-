"""The rules a proof build must obey, shared by both services (C2).

ARCHITECTURE.md §4 gives the certification runner its own narrow authority to
provision without a Jira approval, and bounds it on every side. Those bounds live
in `common/` on purpose: the orchestrator re-verifies them ITSELF rather than
trusting the API's word, exactly as it re-verifies a Jira approval today, and two
copies of a security rule is two chances to fix only one of them.

    sandbox_tier()          refuses when unset or unrecognised
    cost_cap()              the ceiling a plan is checked against
    may_destroy()           marker-scoped teardown — the one that matters most

WHY MARKER AND NOT TIER. The sandbox is `Development`, a real tier holding real
environments people are using. "Everything the runner built" and "everything in
Development" must never become the same query, so teardown is scoped by a
reference marker the runner itself minted. A resource without that marker is
untouchable no matter which tier it is in and no matter what asked.

WHY IT IS OFF BY DEFAULT. A proof build creates real, billable infrastructure.
The same double gate the managed-database path uses applies here: an explicit
opt-in AND a configured sandbox. Absent either, the runner refuses and says so,
rather than quietly doing nothing (which reads as "no blueprints need proving").
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Every proof reference starts with this. It is the teardown scope, the audit
# breadcrumb, and how a human reading the OCI console tells a proof from
# somebody's work at a glance.
MARKER = "PROOF-"

_REFERENCE = re.compile(r"^PROOF-[A-Z0-9-]{1,30}-\d{8}T\d{6}$")


class ProofRefused(RuntimeError):
    """The runner declined to start. Never means "it failed" — means it never
    began, and nothing was created."""


def enabled() -> bool:
    """Proof builds create real, billable resources, so they are opt-in."""
    return os.getenv("CERTIFICATION_PROOF_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on")


def configured_sandbox_tier() -> str:
    """The one tier proofs may build in. Raises rather than guessing.

    ARCHITECTURE.md §4: "An unset or unrecognised sandbox tier makes the runner
    refuse to start, rather than default to somewhere." Defaulting is how a proof
    would end up in Production — the failure nobody gets to undo.

    Checking the name against the known tiers needs the tier list, which lives
    with the models; that check is api.proof.sandbox_tier(). Both services can
    still do the part that matters here — is this the tier I was configured for.
    """
    tier = (os.getenv("CERTIFICATION_SANDBOX_TIER", "") or "").strip()
    if not tier:
        raise ProofRefused(
            "No sandbox tier is configured. Set CERTIFICATION_SANDBOX_TIER to the "
            "tier proof builds may use (currently Development). Neither service "
            "will choose one for you.")
    return tier


def cost_cap() -> float:
    """Monthly ceiling for one proof build, in the portal's currency.

    A proof is meant to be the smallest thing that proves the recipe. This is the
    backstop for when it is not — and it is checked against the PLAN, so the
    runner never discovers a price by paying it.
    """
    try:
        return max(1.0, float(os.getenv("CERTIFICATION_COST_CAP_MONTHLY", "250")))
    except ValueError:
        return 250.0


def is_proof_reference(reference: str | None) -> bool:
    """Whether a reference was minted by this runner.

    Matched against the full shape, not just the prefix: a user environment
    named "proof-something" must not become destroyable by accident.
    """
    return bool(reference) and bool(_REFERENCE.match(reference.strip()))


def may_destroy(reference: str | None) -> bool:
    """THE guard. The runner may tear down only what it created.

    Deliberately not "is it in the sandbox tier" — the sandbox is full of real
    work. If this ever returns True for a reference the runner did not mint, the
    runner can delete somebody's environment.
    """
    return is_proof_reference(reference)


@dataclass(frozen=True)
class CostVerdict:
    allowed: bool
    monthly: float
    cap: float
    reason: str


def check_cost(monthly: float | None, cap: float | None = None) -> CostVerdict:
    """Price gate, applied to the PLAN and before any apply.

    An unknown price is refused rather than waved through. "We could not work out
    what this costs" is not a reason to spend money unattended.
    """
    cap = cost_cap() if cap is None else cap
    if monthly is None:
        return CostVerdict(False, 0.0, cap,
                           "The plan could not be priced, so the cost cap cannot "
                           "be checked. Refusing rather than building blind.")
    if monthly > cap:
        return CostVerdict(False, float(monthly), cap,
                           f"Plan prices at {monthly:,.2f} monthly, above the "
                           f"{cap:,.2f} proof cap. Not built.")
    return CostVerdict(True, float(monthly), cap,
                       f"{monthly:,.2f} monthly, within the {cap:,.2f} cap")


def preflight() -> str:
    """Everything that must be true before a proof may start. Returns the tier.

    Raises ProofRefused with a sentence naming what is missing, so the reason
    reaches the Admin console instead of the runner appearing idle.
    """
    if not enabled():
        raise ProofRefused(
            "Proof builds are disabled. Set CERTIFICATION_PROOF_ENABLED=true to "
            "allow them — each one creates real, billable infrastructure.")
    return configured_sandbox_tier()
