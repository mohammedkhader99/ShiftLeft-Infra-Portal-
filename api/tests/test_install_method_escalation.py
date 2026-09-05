"""When a machine refutes one way of installing something, try the next.

REQ-2026-0184 asked for HashiCorp Vault. The agent guessed `dnf install vault`,
booted a real machine, and was told:

    package vault is not installed
    PORTAL FAILURE: package install did not complete

Correct, honest, and the wrong question. Vault is not in Oracle Linux's
repositories and never will be — it is in HashiCorp's, and adding that repository
is a third way to install software the agent had no vocabulary for. It made one
attempt, with one method, and the request went to manual fulfilment with the
answer sitting one rung away.

Measured on 2026-08-23, not recalled: HashiCorp's RHEL repo definition, its
RHEL/9/x86_64 repodata and its GPG key all return 200.

THE LADDER IS ORDERED BY COST, NOT BY CONFIDENCE. An OS package is one command
and grants no trust. A vendor repository tells the package manager to trust a
publisher for everything it offers, now and at every future update — which is
why its rules are stricter than an archive's, not looser. An archive fetches and
executes one specific verified file. Cheapest first means the cheapest correct
answer is found first.

Every rung is a REAL machine, so the ladder is short and each refutation is
remembered: a method a machine has already disproved is skipped without building.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_blueprint as ab
from api import autobuild, recipe_memory
from api.proof import ProofOutcome
from common import profile_rules
from db.seed import seed
from db.session import Base

MACHINE_SAID = ("Built, but did not verify healthy: oci-service-vm: "
                "vault NOT INSTALLED; vault is inactive")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AI_MODE", "mock")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


SERVICE_VM = {"ref": "oci/service-vm", "target": "oci",
              "resource_kind": "oci-service-vm"}


# --- the ladder ---------------------------------------------------------------

def test_vault_has_somewhere_to_go_after_the_package_guess():
    """The whole defect of REQ-2026-0184: one method, one attempt, no second
    idea."""
    assert ab.install_methods("vault") == ["package", "repo"]


def test_keycloak_still_escalates_to_its_archive():
    assert ab.install_methods("keycloak") == ["package", "archive"]


def test_software_with_no_alternative_still_gets_the_cheap_guess():
    """The ladder must not become a gate: nothing is known about haproxy beyond
    the obvious, and the obvious is right surprisingly often."""
    assert ab.install_methods("haproxy") == ["package"]


def test_the_vendor_repo_profile_is_acceptable_to_the_rules():
    """It reaches a real machine, so it faces the same allow-lists as everything
    else that root executes."""
    profile = ab.profile_for_method("vault", "repo")
    assert profile_rules.profile_problems(profile) == [], profile_rules.profile_problems(profile)
    assert profile["repo"]["url"].endswith(".repo")
    assert profile["repo"]["gpg_key"].startswith("https://")


def test_the_repo_profile_installs_the_package_the_repo_provides():
    profile = ab.profile_for_method("vault", "repo")
    assert profile["rhel"]["packages"] == ["vault"]
    assert profile["rhel"]["services"] == ["vault"]


def test_a_rolling_repository_promises_no_version_and_must_not_invent_one():
    """CORRECTED by REQ-2026-0185. This file used to assert expects == "1", with
    the reason "no version to check means no evidence" — conflating two different
    things. `version_command` is the EVIDENCE; `expects` is the PROMISE. The
    catalogue entry says "HashiCorp Vault" and promises no version, the
    repository serves whatever is current, and the "1" was recalled rather than
    measured. A real machine installed Vault 2.0.4 perfectly and was failed for
    not being 1.

    The machine is still asked and still reports what it got — as UNPROMISED.
    """
    profile = ab.profile_for_method("vault", "repo")
    assert not profile.get("expects"), (
        "a rolling vendor repository cannot promise a major version, and "
        "pinning one here re-breaks the certification at every release")
    assert profile["version_command"], "the machine must still be asked"


def test_a_repository_that_pins_a_version_still_checks_it():
    """The other half, so the lesson does not overshoot into never checking:
    mongodb's repo URL names 7.0, so the RECIPE itself makes the promise and the
    machine is held to it."""
    profile = ab.profile_for_method("mongodb", "repo")
    # ASSERTED AS A RELATIONSHIP, not as the number 7. This pinned "7.0" and so
    # broke the day the recipe was corrected — the old URL was a 404 and cost
    # REQ-2026-0205 a machine. What matters is that the version the URL pins and
    # the version the recipe promises are the SAME version; which version that
    # is, is MongoDB's business and will change again.
    import re as _re
    pinned = _re.search(r"/(\d+)\.\d+/", profile["repo"]["url"])
    assert pinned, f"the repository URL pins no version: {profile['repo']['url']}"
    assert profile["expects"] == pinned.group(1), (
        f"the recipe promises {profile['expects']} but its repository serves "
        f"{pinned.group(1)}")


# --- what the loop does with it -----------------------------------------------

class Machine:
    """A machine that refutes some install methods and accepts others.

    `shipped` returns None until a profile has been published, which is how
    production behaves: nothing builds vault until the agent teaches the service-vm
    blueprint about it. Returning the manifest unconditionally would make
    `ensure` take its existing-recipe branch and never reach the ladder at all.
    """

    def __init__(self, accepts):
        self.accepts = accepts
        self.tried: list[str] = []
        self.published: list[dict] = []

    def publish(self, files):
        self.published.append(files)
        return list(files)

    def withdraw(self, files):
        return list(files)

    def shipped(self, candidate):
        return SERVICE_VM if self.published else None

    def run_proof(self, session, manifest):
        recipe = json.loads(next(iter(self.published[-1].values())))
        method = ("repo" if recipe.get("repo") else
                  "archive" if recipe.get("archive") else "package")
        self.tried.append(method)
        if method in self.accepts:
            return ProofOutcome("PROOF-X", "passed", "built, verified, destroyed")
        # Withdrawn on failure, so the next rung starts from nothing published.
        self.published.clear()
        return ProofOutcome("PROOF-X", "failed", MACHINE_SAID)


def run(db, machine, candidate="vault"):
    return autobuild.ensure(
        candidate, db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw), machine.published


def test_a_refuted_package_guess_escalates_to_the_vendor_repository(db):
    """THE fix for REQ-2026-0184. The machine said vault is not a package; that
    says nothing about whether it is in HashiCorp's repository."""
    machine = Machine(accepts={"repo"})
    result, _ = run(db, machine)

    assert machine.tried == ["package", "repo"], (
        f"it did not escalate: {machine.tried}")
    assert result.status == "published", result.detail


def test_the_cheapest_method_wins_when_it_works(db):
    """A vendor repository is a standing grant of trust. If the OS already
    carries the software, taking it is both cheaper and safer."""
    machine = Machine(accepts={"package", "repo"})
    result, _ = run(db, machine)

    assert machine.tried == ["package"], "it granted trust it did not need"
    assert result.status == "published"


def test_every_method_refuted_ends_honestly_and_says_how_many(db):
    machine = Machine(accepts=set())
    result, _ = run(db, machine)

    assert machine.tried == ["package", "repo"]
    assert result.status == "failed"
    assert "2 install methods" in result.detail
    assert "package, repo" in result.detail


def test_a_method_a_machine_already_refuted_is_not_rebuilt(db):
    """Each rung is a real machine. Remembering is what keeps the ladder cheap —
    a second request must not re-buy the first request's answers."""
    recipe_memory.remember(db, "vault", "oci",
                           ab.profile_for_method("vault", "package"),
                           "PROOF-EARLIER", MACHINE_SAID)
    db.commit()

    machine = Machine(accepts={"repo"})
    result, _ = run(db, machine)

    assert machine.tried == ["repo"], (
        f"it rebuilt a method a machine had already disproved: {machine.tried}")
    assert result.status == "published"


def test_the_ladder_stops_on_a_refusal_that_is_not_the_recipes_fault(db):
    """No egress, a cost cap, a capability — none say anything about the recipe,
    so climbing on them would spend a machine per rung to be told the same
    non-recipe thing.

    Note WHICH rung is refused. An OS package comes from Oracle's own mirrors
    over the service gateway and needs no internet at all, so that rung is tried
    normally; the vendor-repository rung is the one that cannot work without a
    route to rpm.releases.hashicorp.com, and it is refused before a machine is
    built rather than after.
    """
    machine = Machine(accepts=set())

    result = autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        reachable=lambda: False)

    assert machine.tried == ["package"], (
        f"the repo rung was built despite having no route out: {machine.tried}")
    assert result.status == "refused"
    assert "no route there" in result.detail


# --- the ladder must say only what actually happened --------------------------
#
# Found QA'ing REQ-2026-0185, in this file's own feature. Three faults, one root:
# the loop decided whether to climb, and what to tell the requester, without ever
# asking whether a machine had spoken.

class Silent(Machine):
    """A machine that is never built: every proof comes back without a verdict."""

    def __init__(self, status, detail):
        super().__init__(accepts=set())
        self._status, self._detail = status, detail

    def run_proof(self, session, manifest):
        self.tried.append("proof-attempted")
        self.published.clear()
        return ProofOutcome("PROOF-X", self._status, self._detail)


def test_it_does_not_claim_machines_that_were_never_built(db):
    """The sentence said "a machine refuted each one" whenever more than one
    method existed — in text a requester and an approver read as evidence. A
    cost cap stops the proof before anything boots."""
    machine = Silent("refused", "Plan prices at 1240.00, above the 250.00 cap.")
    result = autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw)

    assert "a machine" not in result.detail, (
        f"it claimed evidence it does not have: {result.detail}")


def test_a_cost_refusal_is_not_paid_twice(db):
    """Nothing about the cost cap changes between rungs, so climbing buys the
    identical refusal again. The old guard tested `status == "refused"`, which a
    refused PROOF never reaches — it is withdrawn and returned as "failed"."""
    machine = Silent("refused", "Plan prices at 1240.00, above the 250.00 cap.")
    autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw)

    assert machine.tried == ["proof-attempted"], (
        f"it paid the same non-recipe refusal on every rung: {machine.tried}")


def test_an_abandoned_teardown_stops_the_ladder(db):
    """THE one with a running machine attached to it. `abandoned` means it built
    and could not be destroyed, so the next rung would build a second copy of
    something already leaking — which is exactly why build() stops dead on it.
    The ladder had no such brake."""
    machine = Silent("abandoned", "Built, but teardown failed; resources may remain.")
    autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw)

    assert machine.tried == ["proof-attempted"], (
        "it built another machine while the first may still be running")


def test_an_abandoned_teardown_is_not_remembered_as_a_refutation(db):
    """It says nothing about the recipe, and remembering it would take a working
    component off the menu for thirty days on the strength of our own bug."""
    machine = Silent("abandoned", "Built, but teardown failed; resources may remain.")
    autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw)

    assert recipe_memory.previously_refuted(
        db, "vault", "oci", ab.profile_for_method("vault", "package")) == ""


def test_a_machine_that_really_did_refute_is_still_reported_as_such(db):
    """The correction must not overshoot into never claiming anything."""
    machine = Machine(accepts=set())
    result, _ = run(db, machine)

    assert machine.tried == ["package", "repo"]
    assert "a machine was built for each and refuted it" in result.detail


# --- the rung that does not exist until a machine speaks (C7) -----------------

class Discovering(Machine):
    """A machine that refutes the guess and reports what it found instead.

    RabbitMQ's real shape: no vendor repository, no archive, so the ladder was
    one rung long and stopped — while this machine held the answer.
    """

    def __init__(self, finding, accepts=("discovered",)):
        super().__init__(accepts=set(accepts))
        self.finding = finding

    def run_proof(self, session, manifest):
        recipe = json.loads(next(iter(self.published[-1].values())))
        method = ("discovered" if recipe["rhel"]["packages"] == ["rabbitmq-server"]
                  else "package")
        self.tried.append(method)
        if method in self.accepts:
            return ProofOutcome("PROOF-X", "passed", "built, verified, destroyed")
        self.published.clear()
        return ProofOutcome("PROOF-X", "failed",
                            "Built, but did not verify healthy: rabbitmq NOT INSTALLED")


FOUND = {"package": "rabbitmq-server", "repo_id": "ol9_developer_EPEL",
         "ports": [5672, 15672]}


def test_rabbitmq_certifies_without_anyone_writing_it_down(db):
    """THE ACCEPTANCE CHECK for C7. `install_methods("rabbitmq")` is `["package"]`
    and nothing in this codebase knows the word `rabbitmq-server`. If this passes
    only because a dictionary was edited, the increment failed."""
    assert ab.install_methods("rabbitmq") == ["package"], (
        "rabbitmq was added to a hand-written table, which is the thing C7 exists "
        "to stop")

    machine = Discovering(FOUND)
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: FOUND)

    assert machine.tried == ["package", "discovered"], machine.tried
    assert result.status == "published", result.detail


def test_the_discovered_rung_installs_what_the_machine_named(db):
    machine = Discovering(FOUND)
    autobuild.ensure("rabbitmq", db, target="oci", shipped=machine.shipped,
                     run_proof=machine.run_proof, publish=machine.publish,
                     certify=lambda m, r: None, withdraw=machine.withdraw,
                     discover=lambda ref, code: FOUND)

    profile = json.loads(next(iter(machine.published[-1].values())))
    assert profile["rhel"]["packages"] == ["rabbitmq-server"]
    assert profile["ports"] == [5672, 15672], "the measured ports were dropped"
    assert profile["repo"]["release_package"] in profile_rules.RELEASE_PACKAGES


def test_a_machine_is_asked_only_once(db):
    """Each answer costs a machine. A second discovery would buy the same kind
    of answer for the price of another one."""
    asked = []

    def discover(ref, code):
        asked.append(ref)
        return None

    machine = Machine(accepts=set())
    autobuild.ensure("vault", db, target="oci", shipped=machine.shipped,
                     run_proof=machine.run_proof, publish=machine.publish,
                     certify=lambda m, r: None, withdraw=machine.withdraw,
                     discover=discover)
    assert len(asked) == 1, f"asked {len(asked)} times"


def test_a_machine_that_found_nothing_adds_no_rung(db):
    """Software in no repository at all — Keycloak ships a tarball — cannot be
    discovered. The ladder must end honestly rather than build for nothing."""
    machine = Machine(accepts=set())
    result = autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: None)

    assert machine.tried == ["package", "repo"], machine.tried
    assert result.status == "failed"


def test_discovery_is_optional_and_nothing_regresses_without_it(db):
    """Every existing caller passes no `discover`, and the portal must behave
    exactly as it did before C7 when it is absent."""
    machine = Machine(accepts={"repo"})
    result, _ = run(db, machine)
    assert machine.tried == ["package", "repo"]
    assert result.status == "published"


# --- the machine must be able to say WHICH part failed ------------------------

def test_the_report_asks_whether_the_repository_arrived(db, monkeypatch, tmp_path):
    """"The repo was not added" and "the package is not in it" need different
    fixes, and one failure line cannot say which."""
    from orchestrator import configure

    profile = ab.profile_for_method("vault", "repo")
    (tmp_path / "vault.json").write_text(json.dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    monkeypatch.setenv("CONFIG_ENABLED", "true")

    rendered = configure.render([{"technology_code": "vault"}], "rhel",
                                "https://example/report")
    assert "repo_vault=" in rendered, "the machine cannot say if the repo arrived"
    assert "rpm --import" in rendered, "the signing key is never imported"
    # SCOPED TO runcmd. The report script is written out earlier in the same
    # document and, since C7, contains a `dnf install` of its own for the EPEL
    # discovery step — so searching the whole document found text inside a
    # written file and called it a command ordering. The code was never wrong;
    # the assertion was measuring the wrong thing.
    run = rendered[rendered.index("runcmd:"):]
    assert run.index("config-manager") < run.index("dnf install -y vault"), (
        "the repository is added after the install that needs it")


# --- a refutation is also a POINTER TO EVIDENCE (C7) --------------------------
#
# Found in a pre-flight check minutes after deploying C7, before the user spent
# anything. RabbitMQ's only rung is the package guess, and REQ-2026-0188 had
# already refuted it — correctly. So the ladder would skip that rung, never
# build a machine, never discover anything, and go to manual fulfilment for
# thirty days holding a verdict about a QUESTION THAT WAS NEVER PUT: the report
# behind that refutation predates the discovery step entirely.

class Remembering(Discovering):
    """A machine whose predecessor refuted the guess before C7 existed."""

    def __init__(self, finding, report_was_searched):
        super().__init__(finding)
        self.report_was_searched = report_was_searched
        self.asked: list[str] = []

    def discover(self, reference, code):
        """Only the OLD proof can be unsearched.

        Any machine built now runs the discovery step, so its report always
        carries an answer. An earlier version of this mock returned "never
        asked" for every reference, including machines built seconds ago — which
        made the re-run pointless and looked like a bug in the ladder.
        """
        self.asked.append(reference)
        if reference == "PROOF-BEFORE-C7" and not self.report_was_searched:
            return None            # nobody ever put the question to that machine
        return self.finding or {}  # asked; found something, or found nothing


def _remember_the_guess(db, code="rabbitmq"):
    recipe_memory.remember(db, code, "oci",
                           ab.profile_for_method(code, "package"),
                           "PROOF-BEFORE-C7", "rabbitmq NOT INSTALLED")
    db.commit()


def test_a_refutation_whose_machine_was_never_asked_does_not_strand_the_rung(db):
    """THE pre-flight catch. Skipping on it would fail the user's test for a
    reason that has nothing to do with whether discovery works."""
    _remember_the_guess(db)
    machine = Remembering(FOUND, report_was_searched=False)

    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=machine.discover)

    assert machine.tried == ["package", "discovered"], (
        f"the remembered rung stranded the ladder: {machine.tried}")
    assert result.status == "published", result.detail


def test_a_machine_that_looked_and_found_nothing_is_still_believed(db):
    """The other half, and the one that keeps the memory worth having. A settled
    'there is no such package' must not be re-bought every request.

    NOT `backup`, which this test used until a plant walked past it: backup is a
    CAPABILITY, refused from the catalogue before the ladder is ever reached, so
    the assertion held for a reason that had nothing to do with the memory.
    haproxy is genuinely software on a machine with exactly one rung.
    """
    _remember_the_guess(db, "haproxy")
    machine = Remembering(None, report_was_searched=True)

    result = autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=machine.discover)

    assert machine.tried == [], "it re-bought an answer a machine had given"
    assert result.status == "refused"


def test_a_remembered_rung_that_already_holds_the_answer_builds_nothing_extra(db):
    """Best case: the machine that refuted the guess also reported what it found,
    so the next rung needs no machine of its own to work it out."""
    _remember_the_guess(db)
    machine = Remembering(FOUND, report_was_searched=True)

    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=machine.discover)

    assert machine.tried == ["discovered"], (
        f"it rebuilt a rung whose answer was already on the record: {machine.tried}")
    assert result.status == "published"


def test_the_rerun_cannot_repeat(db):
    """Self-limiting by construction: the machine the re-run builds writes a
    report that DOES carry a discovery section, so the pre-C7 branch is reached
    at most once per recipe. Asserted by counting proofs, not by trusting it."""
    _remember_the_guess(db)
    machine = Remembering(FOUND, report_was_searched=False)

    autobuild.ensure("rabbitmq", db, target="oci", shipped=machine.shipped,
                     run_proof=machine.run_proof, publish=machine.publish,
                     certify=lambda m, r: None, withdraw=machine.withdraw,
                     discover=machine.discover)

    assert machine.tried.count("package") == 1, (
        f"the same rung was rebuilt more than once: {machine.tried}")


class NeverAnswers(Machine):
    """A machine whose package INSTALLS, so no report ever carries a discovery
    section -- for the OLD proof or for the one built seconds ago.

    THE ASSUMPTION THIS BREAKS is written into `Remembering.discover` above:
    "Any machine built now runs the discovery step, so its report always carries
    an answer." It does not. configure.py writes that section ONLY for packages
    that are MISSING -- "a working install stays quiet and costs nothing" -- so
    a package that installs and is refused for running nothing produces a report
    with no section at all, every single time.

    MySQL is that shape, and it cost a real machine on every attempt:
    PROOF-MYSQL-20260904T192226-7E3F14 and PROOF-MYSQL-20260904T224500-93D545
    are two runs rebuilding the identical command-line-client recipe, 26 minutes
    apiece, each ending in the refutation the run before it had already recorded.
    """

    def __init__(self):
        super().__init__(accepts=set())
        self.asked: list[str] = []

    def discover(self, reference, code):
        self.asked.append(reference)
        return None


def _ensure_haproxy(db, machine):
    return autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=machine.discover)


def test_the_rerun_is_not_bought_again_by_the_next_request(db):
    """THE COST DEFECT. `rerun` is a fresh set on every call, so "run the rung
    once" meant once PER REQUEST, not once per recipe -- and for a package that
    installs, the re-run can never learn anything new, because the report it
    produces carries no discovery section either.

    The bound has to outlive the call, and the evidence already does: every
    re-run records a refutation of its own, so a recipe with more than one
    refutation has already been re-run once and must not be again."""
    _remember_the_guess(db, "haproxy")

    first = NeverAnswers()
    _ensure_haproxy(db, first)
    assert first.tried.count("package") == 1, (
        f"the one permitted re-run did not happen: {first.tried}")

    second = NeverAnswers()
    _ensure_haproxy(db, second)
    assert second.tried.count("package") == 0, (
        f"a second request rebuilt the same refuted recipe: {second.tried}. "
        f"On MySQL that was 26 minutes and a real machine, every attempt.")


def test_a_settled_refutation_still_reaches_the_vendors_image(db):
    """THE DEAD END THE BOUND CREATED, found on a real run 2026-09-05.

    MySQL's package rung is refuted three times over, so the bound correctly
    declines to re-run it -- and the ladder then STOPPED, with:

        mysql was not built by the package method. This exact recipe was
        already disproved ... so building another one would spend a machine
        to be told the same thing.

    Before the bound, the re-run rebuilt the client, the certification gate
    refused it holding the vendor's image, and the ladder climbed on that. Take
    the wasted machine away and the climb went with it -- while the refutation
    being read says, in its own words, "its image says it listens on 3306,
    33060". The evidence was in hand and the ladder stopped anyway.

    A refutation that is settled is a reason to try the NEXT method, never a
    reason to stop: that is the sentence the ladder has printed since C7."""
    _remember_the_guess(db, "haproxy")
    # Refuted twice, so the bound will not re-run it -- the state MySQL was in.
    recipe_memory.remember(db, "haproxy", "oci",
                           ab.profile_for_method("haproxy", "package"),
                           "PROOF-SECOND", "installs but runs nothing")
    db.commit()

    machine = Publishing(IMAGE, accepts=("container",))
    result = autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: None,      # the package installed; no section
        find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])

    # Twice is correct: a container's first pass publishes no ports and asks the
    # machine what it bound, and the narrowed recipe is proved again (C8). What
    # matters here is that the rung was reached at all, and that the settled
    # package rung was not rebuilt to get there.
    assert "container" in machine.tried, (
        f"the ladder stopped at a settled refutation instead of reaching the "
        f"vendor's image, and spent {machine.tried} doing it")
    assert machine.tried.count("package") == 0, machine.tried
    assert result.status == "published", result.detail


def test_no_machine_is_spent_reaching_it(db):
    """And the point of the bound survives: the package rung is NOT rebuilt on
    the way to the container. One machine, not two."""
    _remember_the_guess(db, "haproxy")
    recipe_memory.remember(db, "haproxy", "oci",
                           ab.profile_for_method("haproxy", "package"),
                           "PROOF-SECOND", "installs but runs nothing")
    db.commit()

    machine = Publishing(IMAGE, accepts=("container",))
    autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: None, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])

    assert machine.tried.count("package") == 0, machine.tried


def test_a_rung_that_will_be_re_run_does_not_also_queue_the_container(db):
    """FOUND BY A PLANT, 2026-09-05. Dropping the `continue` after the re-run is
    queued let the SAME pass fall through and queue the container rung too.

    Both plants and both tests above stayed green, because the only test with a
    registry to call had a settled refutation and never reached that line.

    It matters because the ladder is ordered by COST. A rung about to be
    re-run may answer the question outright; queueing a container beside it
    buys a registry call and a rung on the strength of an answer nobody has
    waited for -- and the whole point of `continue` is that the cheaper rung
    goes first."""
    _remember_the_guess(db, "haproxy")          # ONE refutation: it will re-run
    asked = []

    machine = NeverAnswers()
    autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=machine.discover,
        find_image=lambda code: asked.append(code) or IMAGE)

    assert machine.tried == ["package"], (
        f"the container rung was queued beside the re-run instead of after it: "
        f"{machine.tried}")
    assert asked == [], (
        "the registry was called while a cheaper rung was still unanswered")


def test_the_registry_is_not_asked_once_per_remembered_rung(db):
    """FOUND BY THE SAME PLANT RUN: dropping `not looked_for_an_image` from the
    fall-through let every remembered rung buy its own registry call.

    `vault` has two rungs before the container, so it is the shape that shows
    it; `haproxy` has one and never could."""
    # A DIFFERENT PROOF PER RUNG, which is what a real ladder leaves behind and
    # is load-bearing here: `ensure` consults any one proof only once, so
    # seeding both recipes with the same reference made the second rung skip
    # the branch entirely and the plant walked past this test.
    for method, references in (("repo", ("PROOF-R1", "PROOF-R2")),
                               ("package", ("PROOF-P1", "PROOF-P2"))):
        recipe = ab.profile_for_method("vault", method)
        for reference in references:                # two each: settled, no re-run
            recipe_memory.remember(db, "vault", "oci", recipe, reference, "no")
    db.commit()
    asked = []

    machine = Machine(accepts=set())
    autobuild.ensure(
        "vault", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: None,
        find_image=lambda code: asked.append(code) or IMAGE)

    assert len(asked) <= 1, (
        f"the registry was asked {len(asked)} times — once per remembered rung, "
        f"instead of once for the technology")


def test_another_technologys_refutations_do_not_bound_this_one(db):
    """FOUND BY A PLANT, 2026-09-05: a counter with its filters removed passed
    every test here, because this database held refutations for nothing else.

    On a real one it holds many. Counting them all would mean the busier the
    portal gets, the sooner every technology stops being re-run -- a bound that
    tightens with unrelated history is not a bound, it is a leak."""
    for other in ("rabbitmq", "vault", "nginx"):
        for reference in ("PROOF-A", "PROOF-B", "PROOF-C"):
            recipe_memory.remember(db, other, "oci",
                                   ab.profile_for_method(other, "package"),
                                   reference, "not installed")
    # And the same technology on another cloud, which is a different question.
    recipe_memory.remember(db, "haproxy", "aws",
                           ab.profile_for_method("haproxy", "package"),
                           "PROOF-AWS", "not installed")
    db.commit()
    _remember_the_guess(db, "haproxy")

    machine = NeverAnswers()
    _ensure_haproxy(db, machine)

    assert machine.tried.count("package") == 1, (
        f"someone else's refutations stopped this rung being re-run: "
        f"{machine.tried}")


def test_a_different_recipe_for_the_same_technology_is_counted_apart(db):
    """The bound is per RECIPE. Refutations of the package guess say nothing
    about the vendor-repository recipe, which is a different attempt."""
    _remember_the_guess(db, "haproxy")
    other_recipe = {**ab.profile_for_method("haproxy", "package"),
                    "repo": {"url": "https://example/repo", "gpg_key": "k"}}
    for reference in ("PROOF-R1", "PROOF-R2", "PROOF-R3"):
        recipe_memory.remember(db, "haproxy", "oci", other_recipe, reference, "no")
    db.commit()

    machine = NeverAnswers()
    _ensure_haproxy(db, machine)

    assert machine.tried.count("package") == 1, machine.tried


def test_the_rerun_still_happens_once_for_a_refutation_that_predates_C7(db):
    """And the migration it exists for is preserved: a refutation recorded
    before the discovery step shipped gets exactly one machine to answer the
    question that could not have been put to it."""
    _remember_the_guess(db, "haproxy")

    machine = NeverAnswers()
    _ensure_haproxy(db, machine)

    assert machine.asked, "the remembered machine was never consulted at all"
    assert machine.tried == ["package"], machine.tried


# --- the container rung, last on the ladder (C8) -------------------------------
#
# THE RABBITMQ STORY, END TO END. REQ-2026-0188/0189/0190 spent three real
# machines establishing that RabbitMQ is not installable from Oracle Linux 9's
# repositories or from EPEL 9 — repology confirms it independently, and
# RabbitMQ's own documentation says to add their repository instead. Their
# official image has been pulled nearly four billion times.
#
# The rung is LAST because the ladder is ordered by cost, and a container's cost
# is not trust — it is that patching, backup and monitoring all change. The
# conventional routes get tried first.

IMAGE = {"image": "docker.io/library/rabbitmq", "tag": "latest",
         "digest": "sha256:" + "9d392587" * 8, "ports": [5672, 15672]}


class Publishing(Discovering):
    """A machine that refutes the package guess; the software ships an image."""

    def run_proof(self, session, manifest):
        recipe = json.loads(next(iter(self.published[-1].values())))
        method = "container" if recipe.get("container") else "package"
        self.tried.append(method)
        if method in self.accepts:
            return ProofOutcome("PROOF-X", "passed", "built, verified, destroyed")
        self.published.clear()
        return ProofOutcome("PROOF-X", "failed",
                            "Built, but did not verify healthy: rabbitmq NOT INSTALLED")


def test_a_sound_search_finding_nothing_reaches_for_the_image(db):
    """THE trigger, and it depends on C7 being right: `{}` means the machine
    searched repositories it named and this software is not in them. That is a
    different fact from "we could not tell", and only one of them justifies
    pulling an image."""
    machine = Publishing(IMAGE, accepts=("container",))
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {},          # searched soundly, found nothing
        find_image=lambda code: IMAGE)

    assert machine.tried == ["package", "container"], machine.tried
    assert result.status == "published", result.detail


def test_an_unsound_search_does_NOT_reach_for_an_image(db):
    """`None` means the machine could not answer. Pulling an image on the
    strength of a broken search would install a container for software that is
    packaged perfectly well — and change how it is patched and backed up."""
    asked = []
    machine = Publishing(IMAGE, accepts=("container",))
    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: None,
        find_image=lambda code: asked.append(code) or IMAGE)

    assert asked == [], "it reached for an image without knowing anything"


def test_a_package_that_installs_and_runs_never_reaches_the_container_rung(db):
    """The cheapest correct answer still wins. A container is the last resort,
    not the first — it changes how the service is patched, backed up and
    monitored, and that cost is only worth paying when nothing else works.

    REWRITTEN 2026-09-03. This used to be "a package that INSTALLS never
    reaches the container rung", with a machine that reported nothing running
    and a claim that the registry was never asked. That is the conflation the
    MySQL client exposed: installed is not running. A package draft is silent
    by design -- the drafter invents no unit, the machine's report supplies it
    -- so "installed fine" can only mean the machine found something running.

    Here it does. The silent draft passes its proof, the certification gate
    declines to keep a recipe that runs nothing, the machine's report is
    consulted, and the `discovered` rung redrafts the SAME package with the
    unit and port the machine actually started. That is the cheapest correct
    answer, and the container is never reached.

    THE REGISTRY IS ASKED EXACTLY ONCE, and the reason is precise: a silent
    draft that passed looks identical whether it is a runtime (dotnet8, files
    you invoke) or a service that installed its client. Only the vendor's
    declaration separates them, and the gate fetches it at that one moment.
    Asking is not the same as using: what this test still holds is that no
    CONTAINER is built while a package can be made to run.
    """
    asked = []
    machine = Publishing(IMAGE, accepts=("package",))
    result = autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        # What the machine reported: the package installed, and started this.
        discover=lambda ref, code: {"package": "haproxy",
                                    "repo_id": "ol9_appstream", "ports": [80]},
        find_image=lambda code: (asked.append(code), IMAGE)[1])

    assert "container" not in machine.tried, machine.tried
    assert result.status == "published", result.detail
    assert asked == ["haproxy"], (
        f"asked {asked}: once, to tell a runtime from a service; never again "
        f"once the machine's report answered")


def test_a_package_that_installs_but_runs_nothing_is_not_fine(db):
    """THE OTHER HALF, and the belief the old test encoded. A package whose
    proof passes -- installed, version answers -- while the machine reports
    nothing running is the MySQL client: files, not a service. With the
    vendor's image declaring ports it is refused, not certified, and the ladder
    climbs. This machine accepts nothing but the package, so the climb ends in
    exhaustion -- and that is the honest answer, because nothing here ran."""
    machine = Publishing(IMAGE, accepts=("package",))
    withdrawn = []
    result = autobuild.ensure(
        "haproxy", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None,
        withdraw=lambda files: withdrawn.append(files) or machine.withdraw(files),
        # WHAT THE MACHINE REALLY ANSWERS. The discovery section is written
        # only for packages that are missing; this one installed, so the
        # report has none and the ladder is told "never asked". The first
        # version of this test said `{}` here -- "looked, found nothing" --
        # which a machine whose package installed never says, and the
        # ladder that stranded PROOF-MYSQL-20260904T005319 passed it.
        discover=lambda ref, code: None,
        find_image=lambda code: IMAGE)

    assert result.status != "published", (
        "a package that installed and ran nothing was certified as the service")
    assert withdrawn, "the silent recipe was left in the store"
    assert machine.tried == ["package", "container"], (
        f"the ladder did not climb to the vendor's image on the gate's own "
        f"evidence: {machine.tried}")


def test_no_image_is_looked_up_before_a_machine_has_spoken(db):
    """A registry lookup is a network round trip to a rate-limited public
    service, and reaching for one before any evidence exists would make every
    request pay for it."""
    order = []
    machine = Publishing(IMAGE, accepts=("container",))

    def watching_proof(session, manifest):
        order.append("machine")
        return machine.run_proof(session, manifest)

    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=watching_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {},
        find_image=lambda code: (order.append("registry"), IMAGE)[1])

    assert order and order[0] == "machine", (
        f"the registry was asked before anything was proved: {order}")


def test_software_that_publishes_no_image_ends_the_ladder_honestly(db):
    """Internal and licensed software will always need a recipe someone writes."""
    machine = Publishing(IMAGE, accepts=set())
    result = autobuild.ensure(
        "some-internal-thing", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: None)

    assert machine.tried == ["package"]
    assert result.status == "failed"


def test_a_registry_outage_never_refuses_a_request(db):
    """A public service being down must mean "no container rung this time", not
    a failed request."""
    def broken(code):
        raise OSError("registry unreachable")

    machine = Publishing(IMAGE, accepts=set())
    with pytest.raises(OSError):
        broken("x")                      # the collaborator really does raise
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {},
        find_image=lambda code: None)    # api.main swallows the exception
    assert result.status == "failed"


def test_the_image_rung_is_asked_for_only_once(db):
    """Each registry lookup is a network round trip against a rate-limited
    public service."""
    asked = []
    machine = Publishing(IMAGE, accepts=("container",))
    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {},
        find_image=lambda code: (asked.append(code), IMAGE)[1])
    assert len(asked) == 1, f"asked {len(asked)} times"


def test_the_drafted_profile_pins_and_confines_what_it_runs(db):
    machine = Publishing(IMAGE, accepts=("container",))
    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE)

    profile = json.loads(next(iter(machine.published[-1].values())))
    assert profile["container"]["digest"] == IMAGE["digest"]
    assert profile["container"]["data_dir"] == "/var/lib/rabbitmq"
    # NO PORTS ON THE FIRST ATTEMPT. Corrected when the narrowing pass was
    # added: the image's declaration is the vendor's whole interface, and
    # publishing it opens every one of those in a real machine's firewall. The
    # first attempt asks the container what it actually bound; the narrowed
    # profile publishes that, and is proved again.
    assert profile["ports"] == [], "it opened ports on a guess"
    assert profile_rules.profile_problems(profile) == []


# --- the narrowing pass (C8) ---------------------------------------------------
#
# `library/rabbitmq` DECLARES six ports: AMQP, AMQPS, epmd, clustering and two
# Prometheus endpoints. A default container listens on far fewer, and every
# declared port would be opened in a real machine's firewall.
#
# A published port binds on the HOST whether or not the container listens, so
# podman's proxy answers either way and a host-side socket diff can never narrow
# anything. The container has to be asked from the inside.

class Narrowing(Publishing):
    """A container that binds fewer ports than its image declares."""

    def __init__(self, image, listening):
        super().__init__(image, accepts=("container",))
        self.listening = listening
        self.published_ports: list[list[int]] = []

    def run_proof(self, session, manifest):
        recipe = json.loads(next(iter(self.published[-1].values())))
        if recipe.get("container"):
            self.published_ports.append(list(recipe.get("ports") or []))
        return super().run_proof(session, manifest)


# `image` defaulted, so every existing caller is unchanged; without it a
# test cannot describe an image that declares no ports at all.
def _narrow(db, machine, listening=None, image=None):
    return autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None, withdraw=machine.withdraw,
        discover=lambda ref, code: {},
        find_image=lambda code: (IMAGE if image is None else image),
        observe_ports=lambda ref, code: (
            machine.listening if listening is None else listening))


def test_the_first_container_attempt_publishes_no_ports(db):
    """Nothing is opened until a machine has SEEN it listening. Publishing the
    image's whole declared interface would open all of it in the firewall."""
    machine = Narrowing(IMAGE, [5672])
    _narrow(db, machine)
    assert machine.published_ports[0] == [], (
        f"it opened ports on a guess: {machine.published_ports[0]}")


def test_the_narrowed_profile_publishes_what_was_DECLARED_AND_OBSERVED(db):
    """RE-POINTED 2026-08-29, and REQ-2026-0228 is why.

    SQL Server was provisioned with 135, 1431, 1433 AND 1434 open in a real
    firewall. Only 1433 is the database: 1434 is the browser service, 1431 a
    secondary listener, 135 the DTC RPC port. The image DECLARES exactly one
    port, so the right answer was in hand and this pass did not use it — it
    published everything the container happened to bind.

    A container binds what it likes internally. What the PUBLISHER DECLARES is
    the interface; what the MACHINE CONFIRMED is what actually works. Opening a
    port is a security decision, and the honest basis for one is both facts
    agreeing rather than either alone.

    `IMAGE` declares [5672, 15672]. 15692 is observed and NOT declared, so it
    is not opened."""
    machine = Narrowing(IMAGE, [5672, 15692])
    result = _narrow(db, machine)
    assert machine.published_ports[-1] == [5672], machine.published_ports
    assert result.status == "published"


def test_every_declared_port_the_machine_confirmed_is_still_published(db):
    """The other direction. Narrowing must not become "publish one port and
    hope" — a service with two real interfaces needs both."""
    machine = Narrowing(IMAGE, [5672, 15672])
    _narrow(db, machine)

    assert machine.published_ports[-1] == [5672, 15672], machine.published_ports


def test_an_image_that_declares_nothing_still_publishes_what_it_bound(db):
    """No interface to intersect with, so the machine's observation is the only
    evidence there is — the same exemption the certification gate makes."""
    silent = {**IMAGE, "ports": []}
    machine = Narrowing(silent, [8080])
    _narrow(db, machine, image=silent)

    assert machine.published_ports[-1] == [8080], machine.published_ports


def test_the_narrowed_recipe_is_PROVED_not_assumed(db):
    """A changed recipe is never certified on the old recipe's proof — the rule
    this project already holds for every other correction."""
    machine = Narrowing(IMAGE, [5672])
    _narrow(db, machine)
    assert machine.tried.count("container") == 2, (
        f"the narrowed profile was certified without a machine: {machine.tried}")


def test_narrowing_happens_once(db):
    """The second draft publishes what the first observed, so there is nothing
    left to narrow — and each pass is a real machine."""
    machine = Narrowing(IMAGE, [5672])
    _narrow(db, machine)
    assert machine.tried.count("container") == 2


def test_a_container_that_bound_nothing_is_not_re_proved(db):
    """No observation, nothing to narrow to. Publishing an empty set again would
    spend a machine to learn the same nothing."""
    machine = Narrowing(IMAGE, [])
    _narrow(db, machine, listening=[])
    assert machine.tried.count("container") == 1


def test_the_first_profile_is_withdrawn_before_the_narrowed_one_is_published(db):
    """Two profiles for one technology in the store is exactly the ambiguity the
    publish/withdraw discipline exists to prevent."""
    machine = Narrowing(IMAGE, [5672])
    withdrawn = []
    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: None,
        withdraw=lambda files: withdrawn.append(sorted(files)),
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])
    assert withdrawn, "the unnarrowed profile was left in the store"


# --- what REQ-2026-0193 cost, and must never cost again -----------------------
#
# The container rung proved, certified, then narrowed — and the narrowing proof
# failed. `take_it_back` withdrew the profile from the store and the
# CERTIFICATION STAYED. The catalogue went on claiming the portal could build
# RabbitMQ, the request passed the certification gate, and the orchestrator built
# the only thing it could with no recipe to render:
#
#     technologies=
#     --- packages ---
#     --- services ---
#     PORTAL: first-boot configuration finished
#
# A bare machine, billing, recorded as provisioned and resolved in Jira. The
# record and the reality disagreeing, with the record believed — the failure
# this entire certification design exists to prevent.

class Narrowing2(Narrowing):
    """A container whose narrowing proof fails after the first one passed."""

    def __init__(self, image, listening, narrow_fails=True):
        super().__init__(image, listening)
        self.narrow_fails = narrow_fails
        self.passes = 0

    def run_proof(self, session, manifest):
        recipe = json.loads(next(iter(self.published[-1].values())))
        if recipe.get("container"):
            self.published_ports.append(list(recipe.get("ports") or []))
            self.tried.append("container")
            if recipe.get("ports") and self.narrow_fails:
                self.published.clear()
                return ProofOutcome("PROOF-N", "failed",
                                    "still waiting for oci-service-vm to report")
            self.passes += 1
            return ProofOutcome("PROOF-C", "passed", "built, verified, destroyed")
        self.tried.append("package")
        self.published.clear()
        return ProofOutcome("PROOF-P", "failed", "rabbitmq NOT INSTALLED")


def _real_certify(db):
    """Certification as production does it, so the row really lands in the
    blueprint table and a later withdrawal has something to suspend."""
    from api import certification

    def certify(manifest, reference):
        return certification.certify_from_proof(
            db, "rabbitmq", "oci", manifest.get("ref", "oci/service-vm"),
            manifest.get("resource_kind", "oci-service-vm"), reference,
            version=manifest.get("version", ""))
    return certify


def test_a_containers_first_pass_proves_without_certifying(db):
    """THE root cause. It publishes no ports on purpose, so certifying it puts a
    recipe on the catalogue that reaches nothing — and that claim then survives
    the narrowing proof failing."""
    certified = []
    machine = Narrowing2(IMAGE, [5672], narrow_fails=True)
    autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: certified.append(r), withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])

    assert certified == [], (
        f"a recipe publishing no ports was certified: {certified}")


def test_a_failed_narrowing_leaves_nothing_certified(db):
    """What made REQ-2026-0193 provision a bare VM."""
    from db.models import Blueprint
    machine = Narrowing2(IMAGE, [5672], narrow_fails=True)
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=_real_certify(db), withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])
    db.commit()

    assert result.status == "failed"
    row = db.get(Blueprint, ("rabbitmq", "oci"))
    assert row is None or row.status != "certified", (
        "the catalogue still claims a technology whose recipe was withdrawn — "
        "the next request builds a machine and configures nothing")


def test_a_withdrawn_recipe_suspends_an_EARLIER_certification(db):
    """The certification can predate the withdrawal by a whole proof. Nothing
    connected the two, so it simply stood."""
    from db.models import Blueprint
    from api import certification as cert
    db.add(Blueprint(technology_code="rabbitmq", deployment_target="oci",
                     blueprint_ref="oci/service-vm",
                     resource_kind="oci-service-vm", status="certified"))
    db.commit()

    assert cert.withdraw_for_missing_recipe(db, "rabbitmq", "oci", "narrowing failed")
    db.commit()
    assert db.get(Blueprint, ("rabbitmq", "oci")).status == cert.WITHDRAWN


def test_the_narrowed_recipe_IS_certified_when_it_proves(db):
    """The deferral must not overshoot into never claiming anything."""
    certified = []
    machine = Narrowing2(IMAGE, [5672], narrow_fails=False)
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: certified.append(r), withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [5672])

    assert result.status == "published"
    assert certified, "a proved, narrowed recipe was never claimed"
    assert machine.published_ports[-1] == [5672]


#: The same image with nothing declared. Plenty of legitimate images declare no
#: ports at all, and for those "the container bound nothing" really is the whole
#: truth rather than a symptom.
SILENT_IMAGE = {**IMAGE, "ports": []}


def test_a_container_that_binds_nothing_is_still_certified(db):
    """No ports to narrow to means publishing none was right all along, and it
    has already been proved. The deferral exists to avoid claiming a recipe we
    are about to replace, not to leave a proved one unclaimed.

    RE-POINTED 2026-08-28, and the distinction is the whole of REQ-2026-0225.
    This asserted the bookkeeping using an image that DECLARES 5672 and 15672
    and a machine that saw neither — which is not "nothing to narrow to", it is
    a container that never started. SQL Server was certified through exactly
    that gap: declared 1433, bound 135, proved, certified, provisioned.

    The bookkeeping point stands and is what this still tests. It is simply
    made with an image that declares nothing, which is the case its own words
    describe. The health case is the test immediately below."""
    certified = []
    machine = Narrowing2(SILENT_IMAGE, [], narrow_fails=False)
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: certified.append(r), withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: SILENT_IMAGE,
        observe_ports=lambda ref, code: [])

    assert result.status == "published"
    assert certified, "a proved recipe was left unclaimed"
    assert machine.tried.count("container") == 1


def test_a_container_that_DECLARES_ports_and_binds_none_is_not_certified(db):
    """The other half, and the one REQ-2026-0225 needed. RabbitMQ's image says
    it serves 5672; a container binding neither that nor 15672 is not RabbitMQ
    running, whatever the proof says about the machine being healthy."""
    certified = []
    machine = Narrowing2(IMAGE, [], narrow_fails=False)
    result = autobuild.ensure(
        "rabbitmq", db, target="oci", shipped=machine.shipped,
        run_proof=machine.run_proof, publish=machine.publish,
        certify=lambda m, r: certified.append(r), withdraw=machine.withdraw,
        discover=lambda ref, code: {}, find_image=lambda code: IMAGE,
        observe_ports=lambda ref, code: [])

    assert certified == [], "a container that never listened was certified"
    assert result.status != "published", result.detail
    assert "5672" in result.detail


def test_take_it_back_ACTUALLY_calls_the_withdrawal(db):
    """Not a duplicate of the unit test above, and not covered by the narrowing
    test either: with the first pass no longer certifying, that scenario has
    nothing to suspend and passes whether or not the call exists — a plant
    removing it from `take_it_back` broke nothing.

    A certification can predate the withdrawal by an entire proof. This seeds
    one the way an earlier run would have left it, then fails a later rung.
    """
    from db.models import Blueprint
    from api import certification as cert
    db.add(Blueprint(technology_code="vault", deployment_target="oci",
                     blueprint_ref="oci/service-vm",
                     resource_kind="oci-service-vm", status="certified"))
    db.commit()

    machine = Machine(accepts=set())          # every rung refuted
    autobuild.ensure("vault", db, target="oci", shipped=machine.shipped,
                     run_proof=machine.run_proof, publish=machine.publish,
                     certify=lambda m, r: None, withdraw=machine.withdraw)
    db.commit()

    assert db.get(Blueprint, ("vault", "oci")).status == cert.WITHDRAWN, (
        "the recipe was withdrawn and the catalogue still claims the "
        "technology — the next request builds a machine and configures nothing")


def test_a_certification_that_still_has_its_recipe_is_left_alone(db):
    """The withdrawal must not overshoot: a technology whose profile is intact
    keeps its certification even when some OTHER rung is withdrawn."""
    from db.models import Blueprint
    db.add(Blueprint(technology_code="nginx", deployment_target="oci",
                     blueprint_ref="oci/service-vm",
                     resource_kind="oci-service-vm", status="certified"))
    db.commit()

    machine = Machine(accepts=set())
    autobuild.ensure("vault", db, target="oci", shipped=machine.shipped,
                     run_proof=machine.run_proof, publish=machine.publish,
                     certify=lambda m, r: None, withdraw=machine.withdraw)
    db.commit()

    assert db.get(Blueprint, ("nginx", "oci")).status == "certified"
