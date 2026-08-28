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

from api import ai_blueprint, certification, proof, recipe_memory


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


def _carry(prior, result):
    """Carry attempts made BEFORE the ladder onto whatever the ladder returned.

    `ensure` builds a fresh result for each path it can take, so anything
    recorded before it chose a path was dropped from the object the caller reads
    and audits. That was invisible until a stale recipe could be retired
    mid-run: a machine had been built, a recipe had been withdrawn, and the
    audit trail showed neither — which is precisely the question somebody asks
    when a request took three machines instead of one.
    """
    if not prior:
        return result
    result.attempts = list(prior) + list(result.attempts)
    for number, attempt in enumerate(result.attempts, start=1):
        attempt.number = number
    return result


def _manifest_of(result, shipped, candidate):
    """The manifest the orchestrator says builds this technology, for a late
    certification. Asked rather than remembered: the wire-up check already
    proved it answers, and carrying a copy would be a second source of truth."""
    return shipped(candidate) or {}


def _recipe_in(proposal) -> dict | None:
    """The profile a draft carries, or None. Same extraction the loop uses."""
    import json as _json
    if proposal is None:
        return None
    return next((_json.loads(body) for body in (proposal.files or {}).values()
                 if body.strip().startswith("{")), None)


def _swap_packages(proposal, replacements: dict[str, str]):
    """A copy of `proposal` with package names replaced, or None if nothing changed.

    Mechanical on purpose. The judgement — WHICH name to use — was already made
    by the ranker that has always made it (`discovery.rank_matches`); this only
    carries the answer into the recipe.
    """
    import copy
    import json as _json

    changed = False
    files: dict[str, str] = {}
    for path, body in (proposal.files or {}).items():
        if not (body or "").strip().startswith("{"):
            files[path] = body
            continue
        doc = _json.loads(body)
        for block in doc.values():
            if isinstance(block, dict) and isinstance(block.get("packages"), list):
                swapped = [replacements.get(pkg, pkg) for pkg in block["packages"]]
                if swapped != block["packages"]:
                    block["packages"] = swapped
                    changed = True
        files[path] = _json.dumps(doc, indent=2)

    if not changed:
        return None
    revised = copy.copy(proposal)
    revised.files = files
    return revised


#: REFUSALS THAT ARE ABOUT ONE RUNG'S RECIPE, NOT ABOUT THE REQUEST.
#:
#: Named as a SET rather than checked one at a time, because checking them one
#: at a time is how this keeps recurring. The loop already knew `remembered`
#: must skip; `repository` was added on 2026-08-26 after REQ-2026-0206 stranded
#: mongodb on the package guess; and `linted` was added on 2026-08-27 after
#: REQ-2026-0214 and REQ-2026-0216 stranded it again — the linter correctly
#: refused the vendor-repo recipe, and that refusal ended the whole ladder
#: before the container rung it should have fallen through to.
#:
#: Three instances of one mistake. A new stage that judges a RECIPE belongs
#: here; a stage that judges the REQUEST (a cost cap, a capability, no egress)
#: does not, and there is a test that makes the distinction explicit.
#:
#: `remembered` is DELIBERATELY ABSENT even though it is a per-rung refusal. It
#: has its own branch below that does more than skip — it reads the report of
#: the machine that refuted the rung, because that machine may already hold the
#: answer the next rung needs. Adding it here made this generic skip fire first
#: and bypass that work, and three tests said so immediately.
PER_RUNG_REFUSALS = frozenset({"repository", "linted"})

#: How many times a rung may revise its own recipe from what the repository
#: said. Two is enough to correct one or two package names and short enough that
#: a recipe the agent cannot get right stops costing anything — the same
#: reasoning MAX_ATTEMPTS uses for the Terraform loop, at a tenth of the cost
#: because none of this touches a machine.
MAX_RECIPE_REVISIONS = 2


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
            result.proof_reference = outcome.reference or ""
            result.files = proposal.files
            result.detail = (
                f"Built, verified and destroyed on attempt {attempt_no}. "
                f"{outcome.detail}")
            return result

        if outcome.status == "refused":
            # NOT THE RECIPE'S FAULT, so it does not cost an attempt.
            #
            # A refusal comes from the preflight or the cost gate, and neither
            # says anything about the draft. REQ-2026-0222 asked for SQL Server
            # while the LIVE pricing API was not answering; `make_price` returned
            # None, the cost gate refused — correctly — and the loop treated it
            # as a failed draft and redrafted. Three attempts were consumed in
            # 114 MILLISECONDS against the same momentary outage, and SQL Server
            # was written off as "the agent could not produce a recipe that
            # passed". The recipe was never the problem, and every redraft was
            # refused by the same unanswering API.
            #
            # Stopping here means the NEXT request tries again with a full
            # budget, which is exactly right for a transient external failure.
            result.attempts.append(Attempt(
                attempt_no, "priced", "refused", outcome.detail))
            result.status = "refused"
            result.detail = outcome.detail
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
           reachable=None, ask_repository=None, discover=None, find_image=None,
           search=None, stored=None,
           observe_ports=None) -> AutobuildResult:
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
        outcome = run_proof(session, manifest)  # noqa: F841 - result used below
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

        # A RECIPE THAT FAILED ITS PROOF MUST STOP CLAIMING IT WORKS.
        #
        # THE FOURTH TIME I have written this fix, and the first time at the
        # level that generalises. `make_withdraw` states the invariant in its
        # own docstring — "a proof that fails has to take it back out. Left
        # behind, an unproven profile makes an uninstallable technology look
        # installable to every later request" — and this branch, the one place
        # that proves a recipe it did not itself write, never called it.
        #
        # SQL SERVER IS THE BILL. A run guessed `dnf install mssql`, wrote
        # generated/profiles/mssql.json, proved it on a machine and failed. The
        # profile stayed. It made oci/service-vm advertise that it builds mssql,
        # so the NEXT request found an existing recipe, arrived here, proved the
        # same guess, failed, and returned — without ever reaching the ladder
        # that knows about vendor repositories and containers. REQ-2026-0217,
        # 0218, 0222, 0223 and 0224: five requests, five machines, one stale file
        # and a branch that could not get past it.
        #
        # WITHDRAWING IS ALSO THE TEST OF OWNERSHIP. `withdraw` is confined to
        # the generated store and reports what it actually deleted, so a recipe
        # a PERSON reviewed removes nothing and the run ends exactly as it did
        # before. We retire our own mistakes and nobody else's.
        # READ IT BEFORE DELETING IT, so the machine's verdict outlives the file.
        #
        # Retiring a failed recipe stops it advertising itself, but the ladder
        # may draft the very same recipe back — RabbitMQ's stored recipe and the
        # only rung its ladder offers are both the same container image — and
        # then a second machine is spent proving what the first one just
        # disproved. The refutation memory already prevents that; it is keyed on
        # the RECIPE, so it needs the recipe, and after `withdraw` there is no
        # recipe left to read.
        failed_recipe = stored(candidate) if stored else None
        retired = list(withdraw({f"{candidate}.json": ""}) or []) if withdraw else []
        if retired and failed_recipe is not None and session is not None:
            recipe_memory.remember(session, candidate, target, failed_recipe,
                                   outcome.reference, outcome.detail)
        if not retired:
            result.status = "failed"
            result.detail = (
                f"{candidate} has a recipe ({manifest.get('ref')}), but it did "
                f"not pass a proof build, so it has NOT been certified. "
                f"{outcome.detail}")
            return result

        result.attempts.append(Attempt(
            len(result.attempts) + 1, "retired", "withdrawn",
            (f"The recipe that failed was written by the agent, not reviewed by "
             f"a person, so it has been withdrawn and no longer advertises "
             f"{candidate} as installable. Trying the remaining install methods "
             f"rather than stopping. {outcome.detail}")[:300]))
        # ... and fall through to the ladder below.

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
        # THE LADDER'S OWN COLLABORATORS, handed to the resolver.
        #
        # INJECTED, like post, price, verify, reachable, discover, find_image
        # and observe_ports before it. The first version reached for
        # `repo_facts.search_packages` here, so every caller — the entire test
        # suite included — did a live repository search, and the ladder's shape
        # depended on what a public service answered that minute.
        methods = list(ai_blueprint.install_methods(
            candidate, find_image=find_image, search=search))
        last = None
        refuted_by_a_machine: list[str] = []
        discovered: dict = {}
        published_image: dict = {}
        looked_for_an_image = False
        narrowed = False
        # Asked once per PROOF, not once per run. A single boolean meant that
        # re-running a rung consumed the one question, so the machine it built
        # was never asked what it found — the exact silence this increment
        # exists to end.
        asked: set[str] = set()
        rerun: set[str] = set()
        while methods:
            method = methods.pop(0)
            # THE CONTAINER RUNG NO LONGER NEEDS A MACHINE TO HAVE SPOKEN (D1).
            #
            # `published_image` used to be filled only by the discovery path —
            # after a machine had been spent and reported nothing — so a rung
            # the resolver offers from the start had no image to draft from.
            #
            # ASSIGNED, not resolved inline in the draft expression. The first
            # version did the lookup inside the ternary below, so
            # `published_image` stayed empty; the narrowing pass then rebuilt it
            # as `{"listening": seen}` with the image REFERENCE LOST, and C8's
            # second pass never ran. 26 tests said so.
            if (method == "container" and not published_image
                    and find_image is not None):
                published_image = find_image(candidate) or {}

            attempt = (ai_blueprint.draft(candidate, session, target=target,
                                          shipped_codes=shipped_codes,
                                          method=method)
                       if method not in ("discovered", "container")
                       else ai_blueprint.draft_from_finding(
                           candidate, discovered, target=target)
                       if method == "discovered"
                       else ai_blueprint.draft_from_image(
                           candidate, published_image, target=target))
            if attempt is None:
                continue
            last = _ensure_vm_service(candidate, session, attempt, target=target,
                                      shipped=shipped, run_proof=run_proof,
                                      publish=publish, certify=certify,
                                      withdraw=withdraw, reachable=reachable,
                                      ask_repository=ask_repository,
                                      method=method,
                                      ignore_memory=method in rerun,
                                      # A container's first pass is a QUESTION
                                      # (what do you bind?), not a candidate for
                                      # the catalogue.
                                      may_certify=not (method == "container"
                                                       and not narrowed))
            if last.status == "published":
                # THE NARROWING PASS (C8). A container's first attempt publishes
                # no ports and asks the container what it actually bound. Now
                # that a machine has answered, the profile is redrafted with
                # exactly those ports and PROVED AGAIN — because a changed
                # recipe is never certified on the old recipe's proof, and until
                # the ports are published nothing outside the machine can reach
                # the service at all.
                #
                # Once. The second draft publishes what the first observed, so
                # there is nothing left to narrow.
                if (method == "container" and not narrowed
                        and observe_ports is not None and last.proof_reference):
                    narrowed = True
                    seen = observe_ports(last.proof_reference, candidate)

                    # DID IT COME UP AT ALL?
                    #
                    # This pass exists to narrow six declared ports down to the
                    # two a container really binds. It took "whatever it bound"
                    # as the answer — which makes it a specification when the
                    # container came up, and a fiction when it did not.
                    #
                    # REQ-2026-0225 is the fiction. SQL Server was started with
                    # no MSSQL_SA_PASSWORD, so it printed its complaint and
                    # exited. The image DECLARES 1433. The machine saw 135 —
                    # MSSQL_RPC_PORT, which is not the database. The narrowed
                    # recipe published 135, the second proof confirmed 135 was
                    # bound, and mssql was CERTIFIED on a container that had
                    # never served a query. A firewall would have been opened on
                    # the wrong port for a database nobody could reach.
                    #
                    # AT LEAST ONE, NOT ALL. `library/rabbitmq` declares six —
                    # AMQP, AMQPS, epmd, clustering and two Prometheus endpoints
                    # — and a default container binds fewer. Demanding the whole
                    # declared set would refuse healthy containers; demanding
                    # none of it certified a dead one. The publisher's list is
                    # what the service is FOR, so overlapping it by one port is
                    # the evidence that the thing inside actually started.
                    #
                    # An image that declares nothing is exempt: there is no
                    # claim to check it against, and plenty of legitimate images
                    # declare nothing at all.
                    declared = {int(p) for p in (published_image.get("ports") or [])}
                    if declared and not (declared & {int(p) for p in (seen or [])}):
                        if withdraw:
                            withdraw(last.files)
                        last.status = "failed"
                        last.detail = (
                            f"{candidate} was not certified: its image declares "
                            f"{sorted(declared)} and the machine found it listening on "
                            f"{sorted(int(p) for p in (seen or [])) or 'nothing'}. A "
                            f"container that never bound its own service port did not "
                            f"start, so the proof established nothing. {last.detail}")
                        last.attempts.append(Attempt(
                            len(last.attempts) + 1, "listening", "failed",
                            last.detail[:300]))
                        return _carry(result.attempts, last)

                    if seen and seen != list(published_image.get("listening") or []):
                        published_image = {**published_image, "listening": seen}
                        if withdraw:
                            withdraw(last.files)
                        methods.append("container")
                        continue
                    # NOTHING TO NARROW TO — the container bound nothing, so
                    # publishing no ports is the right recipe after all and it
                    # has already been proved. Certify it now: the deferral
                    # above exists to avoid claiming a recipe we are about to
                    # replace, not to leave a proved one unclaimed.
                    certify(_manifest_of(last, shipped, candidate),
                            last.proof_reference)
                return _carry(result.attempts, last)
            # A REPOSITORY OBJECTION IS ABOUT THIS RUNG, NOT THIS REQUEST.
            #
            # The guess rung asks for `mongodb`; the repositories say there is
            # no such package, and they are right. But the vendor-repo rung
            # below asks for `mongodb-org` from MongoDB's own repository, which
            # is a different question entirely — and the discovered rung asks a
            # third. Ending the ladder on the first rung's answer strands the
            # technology on the guess for ever.
            #
            # Which is precisely what the comment immediately below warns about,
            # written for `remembered` and walked into by C10 on 2026-08-26:
            # REQ-2026-0206 was refused in seconds with no machine spent — an
            # improvement — but never reached the corrected repository URL that
            # might have worked.
            if last.attempts and last.attempts[-1].stage in PER_RUNG_REFUSALS:
                continue

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
                    if seen == {} and not looked_for_an_image:
                        # SOUND, AND NOTHING THERE. Not "we could not tell" —
                        # the machine searched repositories it named and this
                        # software is not in them. That is precisely the
                        # question a registry can answer next.
                        looked_for_an_image = True
                        if find_image is not None:
                            found = find_image(candidate)
                            if found:
                                published_image = found
                                methods.append("container")
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
                return _carry(result.attempts, last)
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
                    elif finding == {} and not looked_for_an_image:
                        looked_for_an_image = True
                        if find_image is not None:
                            found = find_image(candidate)
                            if found:
                                published_image = found
                                methods.append("container")
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
            return _carry(result.attempts, last)

    built = build(candidate, session, blueprint=None, run_proof=run_proof,
                  publish=publish, target=target, shipped_codes=shipped_codes)
    if built.status != "published":
        return _carry(result.attempts, built)

    # PROVED IS NOT CERTIFIED, and nothing here was doing the second half.
    #
    # `build` drafts a recipe, proves it on a REAL machine, publishes it — and
    # was never given `certify`. So REQ-2026-0223 built SQL Server successfully,
    # reported "Built, verified and destroyed on attempt 1", and the request
    # then fell to manual fulfilment because no certified blueprint existed.
    # Every other path certifies: the existing-manifest branch above calls it,
    # and `_ensure_vm_service` is handed it. This one branch simply did not.
    #
    # ASKED OF THE ORCHESTRATOR FIRST, the same guard `_ensure_vm_service` uses:
    # a recipe in the generated store is not a recipe the executing layer can
    # build, and certifying one it cannot build is how a request reaches apply
    # with nothing behind it.
    manifest = shipped(candidate)
    if not manifest:
        built.attempts.append(Attempt(
            len(built.attempts) + 1, "published", "failed",
            "the recipe did not reach the orchestrator"))
        built.status = "failed"
        built.detail = (
            f"A recipe for {candidate} was written and proved, but the "
            f"orchestrator still reports no blueprint that builds it, so it has "
            f"NOT been certified. {built.detail}")
        return _carry(result.attempts, built)

    certify(manifest, built.proof_reference)
    built.detail = f"{built.detail} Certified against {manifest.get('ref')}."
    return _carry(result.attempts, built)


def _ensure_vm_service(candidate, session, proposal, *, target, shipped,
                       run_proof, publish, certify, withdraw,
                       reachable=None, method="",
                       ask_repository=None,
                       ignore_memory: bool = False,
                       may_certify: bool = True) -> AutobuildResult:
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
        # A VERDICT ON THIS RECIPE, NOT ON THE REQUEST.
        #
        # The stage is `linted`, which `ensure` skips — so the ladder tries the
        # next rung instead of ending here. Before 2026-08-27 this returned and
        # the run was over: the linter correctly refused mongodb's vendor-repo
        # recipe (a baseurl grants dnf no gpgcheck, so every package would
        # install unverified) and the CONTAINER rung it should have fallen
        # through to was never reached. REQ-2026-0214 and REQ-2026-0216 both
        # ended in manual fulfilment for that reason.
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

    # DOES THE REPOSITORY EVEN OFFER THIS? (C10)
    #
    # Asked of the repository, before a machine. Two failures on 2026-08-25 cost
    # five machines between them and were both knowable from here in under a
    # second: `dotnet8.0` is a SOURCE package, which repoquery lists and dnf
    # cannot install; and mongodb's vendor repository URL was a 404.
    #
    # REFUSES, NEVER APPROVES. Anything it cannot settle — a slow repository, a
    # proxy, a listing it could not read — comes back as no objection, and the
    # ladder proceeds exactly as it did before this existed.
    if recipe is not None and ask_repository is not None:
        for revision in range(MAX_RECIPE_REVISIONS + 1):
            try:
                objection = ask_repository(recipe)
            except Exception:  # noqa: BLE001 - a check is never load-bearing
                objection = None
            if objection is None:
                break

            # THE ANSWER WAS ALREADY IN OUR HANDS. This used to report the
            # objection and stop, so a refusal that NAMED the right package
            # ("dotnet8.0 is not published — did you mean dotnet-sdk-8.0?") was
            # read by a person and acted on by a person. That is the manual
            # fulfilment this platform exists to remove, wearing a different
            # face. The ladder now takes the suggestion itself.
            #
            # Costs nothing: no machine, no cloud call beyond the repository
            # metadata already cached, and the revised recipe is re-checked
            # before anything is built.
            best = {bad: names[0]
                    for bad, names in (getattr(objection, "suggestions", {}) or {}).items()
                    if names}
            revised = (_swap_packages(proposal, best)
                       if best and revision < MAX_RECIPE_REVISIONS else None)
            if revised is None:
                result.status = "refused"
                result.detail = f"{candidate} was not built. {objection}"
                result.attempts.append(
                    Attempt(revision + 1, "repository", "refused",
                            str(objection)[:300]))
                return result

            result.attempts.append(Attempt(
                revision + 1, "repository", "revised",
                "the repository named a better package: "
                + ", ".join(f"{bad} -> {good}" for bad, good in sorted(best.items()))))
            proposal, recipe = revised, _recipe_in(revised)

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
        # AND THE CLAIM THAT RESTED ON IT. A profile can be withdrawn AFTER an
        # earlier proof of the same technology certified it — the container
        # rung's narrowing pass does exactly that — and a certification with no
        # recipe behind it makes the portal build a machine and configure
        # nothing. REQ-2026-0193 is what that looks like from the outside: a
        # bare VM, billing, recorded as provisioned.
        if session is not None:
            certification.withdraw_for_missing_recipe(
                session, candidate, target, why[:300])
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
    # RECORDED FOR BOTH OUTCOMES. It was set only on the failure path, so a
    # PASSING proof reported no reference — and the narrowing pass, which reads
    # the report of a proof that succeeded, silently never ran.
    result.proof_reference = outcome.reference or ""
    if outcome.status == "passed":
        # PROVED IS NOT ALWAYS CERTIFIED. A container's first pass publishes no
        # ports on purpose and asks the machine what it bound; certifying it
        # would put a recipe on the catalogue that reaches nothing, and — as
        # REQ-2026-0193 showed — that claim then survives the narrowing proof
        # failing and the profile being withdrawn. The caller certifies once it
        # knows this is the recipe it means to keep.
        if may_certify:
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
    if session is not None and result.machine_refuted:
        recipe_memory.remember(session, candidate, target, recipe,
                               outcome.reference, outcome.detail)
        session.commit()
    return take_it_back(
        f"The profile drafted for {candidate} did not survive a proof build, so it "
        f"has been withdrawn and {candidate} is NOT certified. {outcome.detail}")
