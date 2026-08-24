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

from api import ai_blueprint, proof, recipe_memory


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
    # Whether a machine was BUILT, ASKED, and said no to this recipe.
    #
    # `status == "failed"` cannot answer that: a cost-cap refusal, a contract
    # skew, a profile that never wired up and a machine that reported
    # "NOT INSTALLED" all arrive here as failures, and only the last says
    # anything about the recipe. The escalation ladder needs the difference —
    # climbing to the next install method is only sensible when a machine
    # disproved this one — and so does the sentence shown to the requester,
    # which claimed machines had refuted every rung whether or not any had been
    # built. Same rule, same predicate, as recipe_memory.refutes().
    machine_refuted: bool = False
    # The proof whose machine produced the last verdict, so its report can be
    # fetched and read. Empty when nothing was ever built.
    proof_reference: str = ""

    @property
    def published(self) -> bool:
        return self.status == "published"


def _recipe_in(proposal) -> dict | None:
    """The profile a draft carries, or None. Same extraction the loop uses."""
    import json as _json
    if proposal is None:
        return None
    return next((_json.loads(body) for body in (proposal.files or {}).values()
                 if body.strip().startswith("{")), None)


def _findings_as_dicts(findings) -> list[dict]:
    return [{"severity": f.severity, "rule": f.rule, "detail": f.detail}
            for f in findings]


def build(candidate: str, session: Session, *, blueprint, run_proof, publish,
          target: str = "oci",
          shipped_codes: frozenset[str] = frozenset()) -> AutobuildResult:
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
        proposal = ai_blueprint.draft(candidate, session, target=target,
                                      shipped_codes=shipped_codes)

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
           publish, certify, withdraw=None,
           shipped_codes: frozenset[str] = frozenset(),
           reachable=None, discover=None) -> AutobuildResult:
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
      NOTHING SHIPS IT, but it is software on a machine — keycloak, kafka,
          mongodb. `oci/service-vm` already builds machines correctly; what is
          missing is a package, a unit and a port. The agent writes a PROFILE,
          which extends that blueprint rather than competing with it.
      nothing ships it and it is not a machine  draft Terraform — the full loop.

    `shipped(candidate)` returns the manifest of an existing blueprint that
    builds this candidate, or None. `withdraw(files)` removes a published draft;
    it is needed because a profile has to be in the store BEFORE the proof can
    use it, so a failed proof must take it back out again.
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

    # Nothing ships it. What kind of thing is it?
    # A CAPABILITY IS NOT AN INSTALLABLE THING, and the catalogue says so.
    #
    # "Backup & Recovery", "Centralised Logging", "Monitoring & Alerting" — these
    # are outcomes a platform team designs, with no package, no archive and no
    # cloud resource behind them. REQ-2026-0183 guessed `dnf install backup`,
    # booted a real VM and was told it does not exist: correct, honest, and five
    # minutes and a machine to learn something the catalogue could have said.
    #
    # Refused here BEFORE anything is drafted, so it costs nothing at all rather
    # than one machine and a remembered refutation.
    declared = ai_blueprint.delivery_model(candidate, session)
    if declared == "capability":
        result.status = "refused"
        result.detail = (
            ai_blueprint.delivery_note(candidate, session)
            or f"{candidate} is a capability rather than installable software.")
        result.detail += (" Nothing was built, and no machine was spent finding "
                          "that out. The infrastructure team fulfils this.")
        result.attempts.append(Attempt(1, "catalogue", "refused", result.detail[:300]))
        return result

    proposal = ai_blueprint.draft(candidate, session, target=target,
                                  shipped_codes=shipped_codes)
    if proposal.kind == "vm-service":
        # ESCALATE THROUGH THE INSTALL METHODS, learning from each machine.
        #
        # REQ-2026-0184 asked for HashiCorp Vault. The agent guessed `dnf install
        # vault`, booted a real machine, was told "package vault is not
        # installed" — and stopped. One attempt, one method, and the request went
        # to manual fulfilment with the answer sitting one method away: Vault is
        # not in Oracle's repositories, it is in HashiCorp's.
        #
        # A machine refuting ONE way of installing something says nothing about
        # the others, so the loop now tries the next one. Cheapest first: an OS
        # package grants no trust, a vendor repository trusts a publisher for
        # everything it will ever serve, an archive fetches one verified file.
        # Each attempt is a REAL machine, so the ladder is short and every rung
        # is remembered — a method already refuted is skipped without building.
        #
        # AND ONE RUNG THAT DOES NOT EXIST UNTIL A MACHINE SPEAKS (C7). The
        # methods above are what the agent knows in advance; `discovered` is what
        # a machine found out while refuting one of them. RabbitMQ has no vendor
        # repository and no archive, so its ladder was one rung long and stopped
        # — while the machine refuting it held the answer and was never asked.
        methods = list(ai_blueprint.install_methods(candidate))
        last = None
        refuted_by_a_machine: list[str] = []
        discovered: dict = {}
        # Asked once per PROOF, not once per run. A single boolean meant that
        # re-running a rung consumed the one question, so the machine it built
        # was never asked what it found — the exact silence this increment
        # exists to end.
        asked: set[str] = set()
        rerun: set[str] = set()
        while methods:
            method = methods.pop(0)
            attempt = (ai_blueprint.draft(candidate, session, target=target,
                                          shipped_codes=shipped_codes,
                                          method=method)
                       if method != "discovered"
                       else ai_blueprint.draft_from_finding(
                           candidate, discovered, target=target))
            if attempt is None:
                continue
            last = _ensure_vm_service(candidate, session, attempt, target=target,
                                      shipped=shipped, run_proof=run_proof,
                                      publish=publish, certify=certify,
                                      withdraw=withdraw, reachable=reachable,
                                      method=method,
                                      ignore_memory=method in rerun)
            if last.status == "published":
                return last
            # A RUNG ALREADY REFUTED IS SKIPPED, NOT A REASON TO STOP. The
            # memory says a machine disproved THIS method; the next one is
            # exactly what should be tried, and treating the skip as a verdict
            # would strand vault on the package guess for ever.
            if last.attempts and last.attempts[-1].stage == "remembered":
                # A REFUTATION IS ALSO A POINTER TO EVIDENCE. The machine that
                # refuted this rung may have reported what it found INSTEAD, in
                # which case the answer is already bought and the next rung
                # needs no machine at all.
                #
                # And if that machine was never asked — every refutation
                # recorded before C7 has a report with no discovery section —
                # then the memory cannot answer the question now being put, and
                # skipping on it would strand RabbitMQ in manual fulfilment for
                # thirty days holding a verdict about a different question.
                # Self-limiting: the rung runs once, and the machine it builds
                # writes a report that DOES carry an answer.
                ref = recipe_memory.refuted_proof(
                    session, candidate, target, _recipe_in(attempt))
                if discover is not None and ref and ref not in asked:
                    asked.add(ref)
                    seen = discover(ref, candidate)
                    if seen:
                        discovered = seen
                        methods.append("discovered")
                        continue
                    if seen is None and method not in rerun:
                        # That machine was never asked. Run the rung once so a
                        # current one can answer; its report will carry a
                        # discovery section, so this can never repeat.
                        rerun.add(method)
                        methods.insert(0, method)
                        continue
                continue
            # ONLY A MACHINE'S VERDICT IS A REASON TO TRY ANOTHER RECIPE.
            #
            # This used to read `if last.status == "refused"`, which was inert
            # for most of the cases its own comment named: an egress preflight
            # and a memory hit are the only two that return "refused", while a
            # cost cap, an unset sandbox tier, a contract skew, a profile that
            # never wired up and a teardown that was abandoned all come back as
            # "failed" — and the ladder climbed on every one of them, paying the
            # same non-recipe refusal again at each rung.
            #
            # `abandoned` matters most here. build() stops dead on it because
            # another attempt would build a second copy of something already
            # leaking, and the ladder had no such brake at all.
            #
            # TWO things are a verdict on the recipe in hand, and the next rung
            # is a different recipe: a machine that was built and said no, and
            # the linter refusing to publish this profile at all. An archive
            # missing its checksum is blocked while the same software's vendor
            # repository may be perfectly acceptable, so a block must not strand
            # the ladder any more than a refutation does.
            if not (last.machine_refuted or last.status == "blocked"):
                return last
            if last.machine_refuted:
                refuted_by_a_machine.append(method)
                # ASK THE MACHINE WHAT IT LEARNED — once. Its report is fetched
                # over the signed channel the API already uses, so nothing here
                # imports the orchestrator. Asking twice would buy the same kind
                # of answer for the price of another machine.
                if (discover is not None and last.proof_reference
                        and last.proof_reference not in asked):
                    asked.add(last.proof_reference)
                    finding = discover(last.proof_reference, candidate)
                    if finding:
                        discovered = finding
                        methods.append("discovered")
        if last is not None:
            if len(refuted_by_a_machine) > 1:
                # SAY ONLY WHAT HAPPENED. This sentence used to claim "a machine
                # refuted each one" whenever more than one method existed —
                # including when nothing was ever built — in text a requester and
                # an approver read as evidence.
                last.detail = (
                    f"{last.detail} Tried {len(refuted_by_a_machine)} install "
                    f"methods ({', '.join(refuted_by_a_machine)}); a machine was "
                    f"built for each and refuted it.")
            return last

    return build(candidate, session, blueprint=None, run_proof=run_proof,
                 publish=publish, target=target, shipped_codes=shipped_codes)


def _ensure_vm_service(candidate, session, proposal, *, target, shipped,
                       run_proof, publish, certify, withdraw,
                       reachable=None, method="",
                       ignore_memory: bool = False) -> AutobuildResult:
    """Teach the proven machine blueprint one more technology, and prove it.

    THE ORDER IS INVERTED HERE, deliberately. Everywhere else a draft is proved
    before it is published; a profile must be published FIRST, because the proof
    builds through the orchestrator and the orchestrator reads the profile from
    the store when it renders first-boot configuration. Proving before publishing
    would boot a machine with nothing to install on it — which succeeds, and
    proves nothing, exactly as REQ-2026-0175 did.

    So it is staged: written, proved, and TAKEN BACK OUT if the proof fails. An
    unproven profile left behind would make an uninstallable technology look
    installable to every later request.
    """
    result = AutobuildResult(candidate=candidate, status="refused")

    import json as _json

    recipe = next((_json.loads(body) for body in proposal.files.values()
                   if body.strip().startswith("{")), None)

    blockers = [f for f in proposal.findings if f.severity == "blocker"]
    if blockers:
        result.status = "failed"
        result.attempts.append(Attempt(
            1, "linted", "blocked", "; ".join(f.detail for f in blockers)[:300],
            _findings_as_dicts(blockers)))
        result.detail = (
            f"The profile drafted for {candidate} was refused before any machine "
            f"was booted: " + "; ".join(f.detail for f in blockers)[:300])
        return result

    # CAN THIS MACHINE EVEN FETCH IT? An archive install needs the public
    # internet, and a subnet with only a service gateway installs Oracle's
    # packages perfectly and then cannot reach the release host. The route table
    # already answers this, so asking it here costs nothing and spending a real
    # sandbox VM to be told the same thing costs eight minutes and a machine.
    if reachable is not None:
        import json as _json

        # AN ARCHIVE **OR** A VENDOR REPOSITORY. Both fetch from the public
        # internet — one pulls a release file, the other adds a publisher the
        # package manager then downloads from — and a subnet with only a service
        # gateway defeats each of them identically. Checking only archives would
        # send a vault request to build a machine that cannot reach
        # rpm.releases.hashicorp.com, which is the failure this check exists for.
        wants_internet = any(
            isinstance(_json.loads(body).get("archive"), dict)
            or isinstance(_json.loads(body).get("repo"), dict)
            for body in proposal.files.values()
            if body.strip().startswith("{"))
        if wants_internet and not reachable():
            result.status = "refused"
            result.detail = (
                f"{candidate} is installed from the public internet — a release "
                f"archive or the vendor's own package repository — and the build "
                f"subnet has no route there. A proof would boot a machine that "
                f"installs its dependencies and then cannot fetch the software. "
                f"Nothing was built. Add a NAT gateway to the build subnet, or "
                f"fulfil this one by hand.")
            result.attempts.append(Attempt(
                1, "preflight", "refused", "no internet egress from the build subnet"))
            return result

    # HAS A MACHINE ALREADY DISPROVED THIS EXACT RECIPE?
    #
    # REQ-2026-0183 guessed `dnf install backup`, booted a real VM, and was told
    # "backup NOT INSTALLED" — correct, and five minutes and a machine to learn
    # that a capability name is not an RPM. The evidence was recorded and nothing
    # read it, so the next request would have spent another machine on the
    # identical guess.
    #
    # Keyed on the RECIPE: keycloak was refuted as a package and then succeeded
    # as an archive, so changing the recipe must be allowed to change the answer.
    if session is not None and not ignore_memory:
        already = recipe_memory.previously_refuted(session, candidate, target, recipe)
        if already:
            result.status = "refused"
            result.detail = (f"{candidate} was not built"
                             + (f" by the {method} method" if method else "")
                             + f". {already}")
            result.attempts.append(Attempt(
                1, "remembered", "refused", already[:300]))
            return result

    written = publish(proposal.files)
    result.files = proposal.files

    def take_it_back(why: str) -> AutobuildResult:
        if withdraw:
            withdraw(proposal.files)
        result.status = "failed"
        result.detail = why
        return result

    # DID THE PROFILE ACTUALLY WIRE UP? Two gates have to accept it — the
    # blueprint's `builds` list and configure.py's templates — and missing either
    # is silent. Asking the orchestrator again is the only honest way to know:
    # it re-reads the store, so a manifest coming back that builds this candidate
    # is proof that both gates took it.
    manifest = shipped(candidate)
    if not manifest:
        result.attempts.append(Attempt(1, "published", "failed",
                                       "the profile did not reach the blueprint"))
        return take_it_back(
            f"A profile for {candidate} was written to the generated store, but the "
            f"orchestrator still reports no blueprint that builds it. It has been "
            f"withdrawn rather than left behind. Nothing was built.")

    outcome = run_proof(session, manifest)
    if outcome.status == "passed":
        certify(manifest, outcome.reference)
        result.attempts.append(Attempt(1, "proved", "passed", outcome.detail))
        result.status = "published"
        result.detail = (
            f"{candidate} is software on a machine, so no new Terraform was "
            f"written: a technology profile now extends {manifest.get('ref')}, "
            f"which already builds machines correctly. Proved on a real machine "
            f"and certified. {outcome.detail}")
        return result

    result.attempts.append(Attempt(1, "proved", "failed", outcome.detail,
                                   _findings_as_dicts(
                                       ai_blueprint.diagnose(outcome.detail,
                                                             proposal.files))))
    # A MACHINE SAID NO. Record it so the next request does not buy the same
    # answer again — but only when the machine actually reported, which
    # recipe_memory.refutes() decides: a cost-cap refusal or a failed teardown
    # is our problem, not the recipe's.
    result.machine_refuted = recipe_memory.refutes(outcome.status, outcome.detail)
    result.proof_reference = outcome.reference or ""
    if session is not None and result.machine_refuted:
        recipe_memory.remember(session, candidate, target, recipe,
                               outcome.reference, outcome.detail)
        session.commit()
    return take_it_back(
        f"The profile drafted for {candidate} did not survive a proof build, so it "
        f"has been withdrawn and {candidate} is NOT certified. {outcome.detail}")
