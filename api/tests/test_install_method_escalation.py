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
    assert "7.0" in profile["repo"]["url"]
    assert profile["expects"] == "7"


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
