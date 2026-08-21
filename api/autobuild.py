"""The closed loop: draft, check, prove, diagnose, redraft (C5c).

The reviewer's requirement, stated as mandatory on 2026-08-21: the portal's agent
writes its own Terraform. This is that loop, and everything it must survive on
the way.

    draft ──▶ linter ──▶ security gate ──▶ terraform validate ──▶ proof build
      ▲                                                              │
      └──────────────── diagnose ◀── failed ──────────────────────────┘
                        (bounded: MAX_ATTEMPTS)

WHY THE GATES ARE THE FEATURE, NOT THE MODEL. Four of the eight defects found on
2026-08-17 were written by an AI with the provider documentation open, and each
one looked completely reasonable. A model that has read this codebase will
reproduce its habits, so what makes this safe is the chain a draft has to survive
— not the wording of the prompt.

Each gate refuses for a different reason, and they are ordered cheapest-first so
an obviously wrong draft never reaches a cloud:

  linter          defects that already cost this project builds
  security gate   what a green build is silent about (§8), in STRICT mode —
                  an unreviewed resource type is a gap, not a pass
  proof build     it actually provisions, verifies healthy, and destroys itself

THE LOOP IS BOUNDED. An agent that can redraft forever is an agent that can spend
forever: every attempt is a real build in a real tenancy. MAX_ATTEMPTS caps it,
and giving up is a recorded outcome rather than silence.

WHAT THIS DOES NOT CLAIM. A novel security mistake — an exposure no rule
anticipates — passes every gate here. The strict scanner narrows that by refusing
resource types nobody has written a rule for, and C1 withdraws a blueprint that
fails in production, but neither is prevention. That residual risk was accepted
deliberately (ARCHITECTURE.md §7, and the reviewer's decision of 2026-08-21).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from api import ai_blueprint, proof


def max_attempts() -> int:
    """How many times the agent may redraft before giving up.

    Bounded because every attempt is a real build: three is enough for the
    failures that carry their own diagnosis (a wrong version, a public IP, a
    shape that does not exist) and short enough that a recipe the agent cannot
    get right stops costing money.
    """
    try:
        return max(1, int(os.getenv("AUTOBUILD_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


def enabled() -> bool:
    """Off by default. This writes infrastructure code and then builds it."""
    return os.getenv("AUTOBUILD_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on")


@dataclass
class Attempt:
    number: int
    stage: str            # linted | scanned | proved
    outcome: str          # blocked | failed | passed
    detail: str
    findings: list[dict] = field(default_factory=list)


@dataclass
class AutobuildResult:
    candidate: str
    status: str           # published | blocked | failed | refused | exhausted
    attempts: list[Attempt] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    detail: str = ""

    @property
    def published(self) -> bool:
        return self.status == "published"


def _findings_as_dicts(findings) -> list[dict]:
    return [{"severity": f.severity, "rule": f.rule, "detail": f.detail}
            for f in findings]


def build(candidate: str, session: Session, *, blueprint, run_proof, publish,
          target: str = "oci") -> AutobuildResult:
    """Draft a recipe for `candidate`, prove it, and publish it if it survives.

    The three collaborators are injected so the whole loop — including the
    failure paths, which a real orchestrator will not produce on demand — can be
    driven by a test:

        run_proof(session, blueprint) -> ProofOutcome
        publish(files)                -> writes to the generated store

    `publish` is called ONLY after every gate has passed. Nothing reaches the
    store on the strength of a draft alone.
    """
    result = AutobuildResult(candidate=candidate, status="refused")

    if not enabled():
        result.detail = (
            "Automatic blueprint building is disabled. Set AUTOBUILD_ENABLED=true "
            "to allow it — it writes infrastructure code and then builds it for "
            "real.")
        return result

    limit = max_attempts()
    failure_detail = ""

    for attempt_no in range(1, limit + 1):
        proposal = ai_blueprint.draft(candidate, session, target=target)

        # A previous failure is CONTEXT for this draft, not a verdict on it.
        # Overwriting the draft's own findings with the diagnosis blocked the
        # very redraft meant to fix the problem: "the version you pinned was
        # retired" explains the LAST attempt, it is not a defect in this one.
        # The diagnosis is recorded so a human can follow the reasoning; the
        # gate below still judges this draft on its own code.
        diagnosis = (ai_blueprint.diagnose(failure_detail, proposal.files)
                     if failure_detail else [])

        # GATE 1 — defects this project has already paid for.
        blockers = [f for f in proposal.findings if f.severity == "blocker"]
        if blockers:
            result.attempts.append(Attempt(
                attempt_no, "linted", "blocked",
                f"{len(blockers)} blocking finding(s) in the draft",
                _findings_as_dicts(blockers + diagnosis)))
            failure_detail = "; ".join(f.detail for f in blockers)
            continue

        # GATE 2 and 3 — the security scan and the real build both live inside
        # the proof, because both need a plan, and a plan needs the module on
        # disk. The proof destroys whatever it creates either way.
        outcome = run_proof(session, blueprint)
        if outcome.status == "passed":
            publish(proposal.files)
            result.attempts.append(Attempt(
                attempt_no, "proved", "passed", outcome.detail))
            result.status = "published"
            result.files = proposal.files
            result.detail = (
                f"Built, verified and destroyed on attempt {attempt_no}. "
                f"{outcome.detail}")
            return result

        if outcome.status == "abandoned":
            # It could not tear down what it built. Stopping immediately: another
            # attempt would build a second copy of something already leaking.
            result.attempts.append(Attempt(
                attempt_no, "proved", "failed", outcome.detail))
            result.status = "failed"
            result.detail = (
                f"Stopped after attempt {attempt_no}: the proof could not be torn "
                f"down, so something may still be running. {outcome.detail}")
            return result

        result.attempts.append(Attempt(
            attempt_no, "proved", "failed", outcome.detail,
            _findings_as_dicts(ai_blueprint.diagnose(outcome.detail, proposal.files))))
        failure_detail = outcome.detail

    result.status = "exhausted"
    result.detail = (
        f"Gave up after {limit} attempts. The agent could not produce a recipe "
        f"that passed. Last failure: {failure_detail[:300]}")
    return result


def summarise(result: AutobuildResult) -> dict:
    """A record for the audit log and the console."""
    return {
        "candidate": result.candidate,
        "status": result.status,
        "attempts": [
            {"number": a.number, "stage": a.stage, "outcome": a.outcome,
             "detail": a.detail[:300], "findings": a.findings}
            for a in result.attempts
        ],
        "files": sorted(result.files),
        "detail": result.detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


# --- Making a component buildable, whatever it takes -------------------------

def ensure(candidate: str, session: Session, *, target, shipped, run_proof,
           publish, certify) -> AutobuildResult:
    """Make `candidate` provisionable, and certify it — no human involved.

    The reviewer's requirement of 2026-08-21, in full: if the agent cannot find a
    Terraform module or blueprint for a selected component, it writes one,
    certifies it itself, and the request proceeds.

    THREE CASES, and only the last one needs a model. Reaching for the drafter
    when a perfectly good recipe already exists would be the expensive way to be
    wrong:

      already certified   nothing to do.
      SHIPPED BUT UNCERTIFIED  the orchestrator already has a recipe that builds
          this — nginx is exactly here, sharing oci/service-vm with four
          technologies that ARE certified. Nothing needs writing; it needs
          proving. Prove it, certify it.
      nothing ships it    draft, gate, prove, publish, certify — the full loop.

    `shipped(candidate)` returns the manifest of an existing blueprint that
    builds this candidate, or None.
    """
    result = AutobuildResult(candidate=candidate, status="refused")

    if not enabled():
        result.detail = (
            "Automatic blueprint building is disabled. Set AUTOBUILD_ENABLED=true "
            "to allow it — it writes infrastructure code and then builds it for "
            "real.")
        return result

    manifest = shipped(candidate)
    if manifest:
        # A recipe already exists and somebody simply never certified it. Prove
        # it and certify it: writing a second recipe for the same thing would be
        # worse than useless, because two blueprints claiming one resource kind
        # is the collision the registry refuses.
        outcome = run_proof(session, manifest)
        if outcome.status == "passed":
            certify(manifest, outcome.reference)
            result.attempts.append(Attempt(1, "proved", "passed", outcome.detail))
            result.status = "published"
            result.detail = (
                f"{candidate} already had a recipe ({manifest.get('ref')}) that "
                f"nobody had certified. Proved and certified it; nothing was "
                f"written. {outcome.detail}")
            return result
        result.attempts.append(Attempt(1, "proved", "failed", outcome.detail))
        result.status = "failed"
        result.detail = (
            f"{candidate} has a recipe ({manifest.get('ref')}), but it did not "
            f"pass a proof build, so it has NOT been certified. {outcome.detail}")
        return result

    return build(candidate, session, blueprint=None, run_proof=run_proof,
                 publish=publish, target=target)
