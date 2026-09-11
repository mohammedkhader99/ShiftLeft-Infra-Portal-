"""Where a request has got to, and what says so (U.3).

Asked for as a picture: Agent, Blueprint, Terraform, Validate, Plan,
Security/Policy, Provision, Verify, Ready. Every one of those is something this
portal already does; what was missing was anywhere to see it. A requester whose
request said `in-progress` for eleven minutes had no way to tell whether the
agent was building a recipe, whether Terraform had planned, or whether it was
stuck.

EVIDENCE, NOT ASSERTION. A stage is `done` because the append-only audit log
says it happened and names when. It is `failed` because a failure event is
there. It is `current` because the request's own status puts it there. Nothing
is inferred from optimism, and where the trail is silent the stage says so
rather than claiming a time it cannot support -- a progress bar that invents its
own history is worse than none, because it is believed.

THE ORDER IS NOT A PROMISE THAT ALL OF IT WILL HAPPEN. A request whose
components are all certified never needs the agent; one that is manually
fulfilled never reaches Terraform. Those stages come back `skipped` with the
reason, because "it did not happen" and "it has not happened yet" are different
answers to the only question being asked.

NOTHING HERE DECIDES ANYTHING. It reads a status and a trail that other code
wrote. It cannot move a request, and must never learn how.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How far a status means the request actually got. Used only to decide which
#: stage is CURRENT and which are behind it -- never to claim a stage happened,
#: because a status is where something is, not a record of how it arrived.
_REACHED = {
    "draft": 0,
    "submitted": 1,
    "rejected": 1,
    "cost-changed": 1,
    "auto-building": 2,
    "manual-fulfil": 2,
    "planned": 4,
    "in-progress": 6,
    "apply-failed": 6,
    "verify-failed": 7,
    "provisioned": 8,
    "decommissioned": 8,
    "cancelled": 0,
}

DONE, CURRENT, PENDING, FAILED, SKIPPED = (
    "done", "current", "pending", "failed", "skipped")


@dataclass(frozen=True)
class Stage:
    """One step, what proves it, and what proves it went wrong."""

    key: str
    title: str
    #: What this step means, in the words a requester would use.
    blurb: str
    #: Audit events that prove it happened. The FIRST one found wins, so order
    #: these from the most specific to the most general.
    done_on: tuple[str, ...] = ()
    #: Audit events that prove it failed, or stopped here.
    failed_on: tuple[str, ...] = ()
    #: Audit events that mean it was never needed.
    skipped_on: tuple[str, ...] = ()
    #: Request statuses that mean the request is sitting on this step now.
    current_on: tuple[str, ...] = ()
    #: Statuses that mean it failed here, when no audit event says so.
    failed_status: tuple[str, ...] = ()
    position: int = 0


#: The pipeline, in order. Nine steps, each mapped to something the system
#: really records -- see the audit events in api/main.py.
STAGES: tuple[Stage, ...] = (
    Stage(key="submitted", title="Submitted", position=1,
          blurb="The request was raised, validated and priced.",
          done_on=("request.submitted", "approval.checked", "approval.approved",
                   "orchestrator.handoff"),
          current_on=("submitted",)),
    Stage(key="approved", title="Approved", position=2,
          blurb="Jira holds the approval. The portal only asks.",
          done_on=("approval.approved", "quorum.met"),
          failed_on=("approval.rejected", "four_eyes.blocked", "quorum.blocked"),
          current_on=("submitted",)),
    Stage(key="blueprint", title="Blueprint", position=3,
          blurb="A certified recipe exists for every component. The agent proves "
                "one where none did.",
          done_on=("autobuild.finished", "blueprint.certified", "blueprint.autobuilt"),
          failed_on=("catalogue.gap",),
          skipped_on=("fulfilment.manual",),
          current_on=("auto-building",)),
    Stage(key="terraform", title="Terraform selected", position=4,
          blurb="The signed handoff reaches the orchestrator, which picks the "
                "module from the certified blueprint.",
          done_on=("orchestrator.handoff",),
          failed_on=("orchestrator.refused", "orchestrator.unreachable"),
          skipped_on=("fulfilment.manual",)),
    Stage(key="plan", title="Terraform plan", position=5,
          blurb="What would be built, worked out before anything is.",
          done_on=("plan.previewed",),
          failed_on=("plan.failed",),
          current_on=("planned",)),
    Stage(key="policy", title="Security and policy", position=6,
          blurb="OPA re-checks the request and the plan is scanned, at execution "
                "rather than on the word of the form.",
          done_on=("policy.warnings", "provisioning.started"),
          failed_on=("provision.halted", "policy.blocked")),
    Stage(key="provision", title="Provision", position=7,
          blurb="Terraform applies. This is the first step that creates anything.",
          done_on=("apply.completed", "provisioned"),
          failed_on=("apply.failed", "apply.timeout"),
          current_on=("in-progress",),
          failed_status=("apply-failed",)),
    Stage(key="verify", title="Verify", position=8,
          blurb="The machines report what actually started, and are checked "
                "against what was asked for.",
          done_on=("provisioned",),
          failed_status=("verify-failed",)),
    Stage(key="ready", title="Ready", position=9,
          blurb="Built, verified, and yours.",
          done_on=("provisioned",),
          current_on=("provisioned",)),
)


@dataclass
class _Seen:
    """The first time each event appears, so a stage can name when it happened."""

    at: dict[str, str] = field(default_factory=dict)
    detail: dict[str, dict] = field(default_factory=dict)

    def first(self, events: tuple[str, ...]) -> tuple[str | None, dict | None]:
        for event in events:
            if event in self.at:
                return self.at[event], self.detail.get(event)
        return None, None


def _seen(entries) -> _Seen:
    """Index the trail oldest-first, so the time shown is when it FIRST happened.

    A retried apply writes `apply.failed` then `apply.completed`; showing the
    later timestamp for the failure would make the retry look like the original
    attempt, and somebody reading the history would draw the wrong conclusion
    about how long it took.
    """
    seen = _Seen()
    for entry in entries:
        event = getattr(entry, "event", None)
        if not event or event in seen.at:
            continue
        at = getattr(entry, "created_at", None)
        seen.at[event] = at.isoformat() if at else ""
        seen.detail[event] = getattr(entry, "detail", None) or {}
    return seen


def build(status: str, entries, status_detail: str | None = None) -> list[dict]:
    """The pipeline for one request: every stage, its state, and what says so.

    `entries` are its audit rows in any order; `status` is the request's own.
    """
    status = (status or "").strip()
    seen = _seen(entries)
    reached = _REACHED.get(status, 0)
    terminal = status in ("cancelled", "rejected")

    out = []
    for stage in STAGES:
        at, detail = seen.first(stage.done_on)
        failed_at, failed_detail = seen.first(stage.failed_on)
        skipped_at, _ = seen.first(stage.skipped_on)

        if failed_at is not None or status in stage.failed_status:
            state, when, evidence = FAILED, failed_at, failed_detail
        elif skipped_at is not None:
            state, when, evidence = SKIPPED, skipped_at, None
        elif at is not None:
            state, when, evidence = DONE, at, detail
        elif terminal:
            # A cancelled or rejected request does not sit on a step waiting.
            # Saying "in progress" about something that has stopped is the kind
            # of screen that has people waiting for a build that is not coming.
            state, when, evidence = PENDING, None, None
        elif status in stage.current_on:
            state, when, evidence = CURRENT, None, None
        elif reached > stage.position:
            # PAST IT, WITH NOTHING SAYING WHEN. The request is demonstrably
            # further on, so the step happened -- but no event recorded it, and
            # inventing a time would put a number on the screen that nothing
            # supports.
            state, when, evidence = DONE, None, None
        else:
            state, when, evidence = PENDING, None, None

        out.append({
            "key": stage.key,
            "title": stage.title,
            "blurb": stage.blurb,
            "state": state,
            "at": when,
            # Whatever the audit entry carried. Shown as supporting detail, never
            # as the reason on its own -- the reason a request stopped is in
            # `status_detail`, written for a person to read.
            "evidence": evidence or None,
        })

    # The sentence the request itself carries, attached to wherever it stopped,
    # because that is where somebody looking at a stalled pipeline will look.
    if status_detail:
        for entry in out:
            if entry["state"] in (FAILED, CURRENT):
                entry["detail"] = status_detail
                break

    return out
