"""The closed loop: draft, check, prove, diagnose, redraft (C5c).

The reviewer's mandatory requirement — the portal's agent writes its own
Terraform. What makes that safe is not the model but the chain a draft has to
survive, so most of these tests are about the ways a draft is stopped.

Four of the eight defects found on 2026-08-17 were written by an AI with the
provider documentation open, and every one of them looked reasonable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_blueprint, autobuild
from api.proof import ProofOutcome
from db.models import Blueprint
from db.seed import seed
from db.session import Base

_DETAILS = {
    "passed": "built, verified, destroyed",
    "failed": "Invalid kubernetes version v1.29.1",
    "abandoned": "teardown failed; may still be running",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
        monkeypatch.delenv(key, raising=False)


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


@pytest.fixture()
def bp():
    return Blueprint(technology_code="cassandra5", deployment_target="oci",
                     blueprint_ref="oci/cassandra", resource_kind="oci-cassandra",
                     status="draft")


class Publisher:
    """Stands in for writing to the generated store, and records whether it was
    ever called — which is the property most of these tests turn on."""

    def __init__(self, writes: bool = True):
        self.calls: list[dict] = []
        self.writes = writes

    def __call__(self, files):
        self.calls.append(files)
        # WHAT IT WROTE, because that is publish's contract and a proof now
        # checks the answer. This returned None, which reads as an empty store,
        # so the FAKE decided whether a build could pass.
        # PROOF-MYSQL-20260902T123737 reported a published profile that existed
        # nowhere, and nothing here would have caught it.
        # SPELT THE WAY make_publish SPELLS IT. A draft names repository-style
        # paths and the real publisher returns resolved absolute ones, so only
        # the file name is comparable. Returning the caller's own strings made
        # the two sides match trivially, and a comparison that used whole paths
        # -- and would therefore never match in production -- passed here.
        import pathlib
        return ([f"/generated/store/{pathlib.PurePosixPath(str(n)).name}"
                 for n in (files or {})] if self.writes else [])


def proves(*statuses):
    """A run_proof returning each status in turn."""
    seq = list(statuses)

    def run(session, blueprint):
        status = seq.pop(0) if seq else "passed"
        return ProofOutcome("PROOF-X-20260821T090000", status, _DETAILS[status])
    return run


def allow(monkeypatch, attempts="3"):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AUTOBUILD_MAX_ATTEMPTS", attempts)
    monkeypatch.setenv("AI_MODE", "mock")


# --- Off unless asked for ----------------------------------------------------

def test_it_is_disabled_by_default(db, bp):
    pub = Publisher()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)
    assert result.status == "refused"
    assert "AUTOBUILD_ENABLED" in result.detail
    assert pub.calls == [], "it published despite being switched off"


# --- The happy path ----------------------------------------------------------

def test_a_draft_that_survives_every_gate_is_published(db, bp, monkeypatch):
    allow(monkeypatch)
    pub = Publisher()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)

    assert result.status == "published"
    assert len(pub.calls) == 1
    assert result.attempts[-1].outcome == "passed"


def test_nothing_is_published_before_the_proof(db, bp, monkeypatch):
    """THE property. A draft reaching the store on its own merits would put
    model-written Terraform on the provisioning path with nothing in between."""
    allow(monkeypatch)
    pub = Publisher()
    autobuild.build("cassandra5", db, blueprint=bp,
                    run_proof=proves("failed", "failed", "failed"), publish=pub)
    assert pub.calls == [], "unproven code was written to the store"


# --- Stopped by the linter, before a cloud is touched ------------------------

def test_a_draft_with_a_known_defect_never_reaches_a_cloud(db, bp, monkeypatch):
    """Cheapest gate first: a draft carrying a defect that already cost this
    project builds must not spend a real build to discover it."""
    allow(monkeypatch)
    monkeypatch.setattr(ai_blueprint, "_scaffold_draft",
                        lambda c, t="oci": ai_blueprint.Draft(
                            candidate=c, kind="new-service",
                            files={"main.tf": "assign_public_ip = true"}))
    proofs: list[int] = []

    def counting_proof(session, blueprint):
        proofs.append(1)
        return ProofOutcome("r", "passed", "ok")

    pub = Publisher()
    # A cloud-managed name so this still exercises `build`, the Terraform route:
    # a plain `cassandra5` is software on a machine and now gets a profile (C6).
    result = autobuild.build("oci-cassandra", db, blueprint=bp,
                             run_proof=counting_proof, publish=pub)

    assert proofs == [], "a blocked draft was still sent to be built"
    assert pub.calls == []
    assert result.attempts[0].outcome == "blocked"
    assert "public-ip" in [f["rule"] for f in result.attempts[0].findings]


# --- The redraft loop, and its limit -----------------------------------------

def test_a_failed_proof_is_diagnosed_and_retried(db, bp, monkeypatch):
    allow(monkeypatch)
    pub = Publisher()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("failed", "passed"), publish=pub)

    assert result.status == "published"
    assert len(result.attempts) == 2
    assert result.attempts[0].outcome == "failed"


def test_the_loop_is_bounded(db, bp, monkeypatch):
    """An agent that can redraft forever is an agent that can spend forever:
    every attempt is a real build in a real tenancy."""
    allow(monkeypatch, attempts="2")
    pub = Publisher()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("failed", "failed", "failed"),
                             publish=pub)

    assert result.status == "exhausted"
    assert len(result.attempts) == 2, "it exceeded its own attempt limit"
    assert pub.calls == []


def test_giving_up_says_why_rather_than_going_quiet(db, bp, monkeypatch):
    allow(monkeypatch, attempts="1")
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("failed"), publish=Publisher())
    assert "Gave up after 1 attempts" in result.detail
    assert "v1.29.1" in result.detail, "the last failure was not carried through"


def test_a_proof_that_cannot_tear_down_stops_the_loop_immediately(db, bp, monkeypatch):
    """Retrying would build a second copy of something already leaking."""
    allow(monkeypatch, attempts="3")
    pub = Publisher()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("abandoned", "passed"), publish=pub)

    assert result.status == "failed"
    assert len(result.attempts) == 1, "it kept building after a failed teardown"
    assert "may still be running" in result.detail
    assert pub.calls == []


def test_the_attempt_limit_floors_at_one(monkeypatch):
    monkeypatch.setenv("AUTOBUILD_MAX_ATTEMPTS", "0")
    assert autobuild.max_attempts() == 1
    monkeypatch.setenv("AUTOBUILD_MAX_ATTEMPTS", "nonsense")
    assert autobuild.max_attempts() == 3


# --- What the record has to show ---------------------------------------------

def test_every_attempt_is_recorded_for_a_human_to_follow(db, bp, monkeypatch):
    """A reviewer needs the agent's working, not its conclusion."""
    allow(monkeypatch)
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("failed", "passed"),
                             publish=Publisher())
    record = autobuild.summarise(result)

    assert record["status"] == "published"
    assert [a["number"] for a in record["attempts"]] == [1, 2]
    assert record["attempts"][0]["outcome"] == "failed"
    assert record["at"]


# --- a refusal is not a failed draft ------------------------------------------

def test_an_unpriceable_plan_does_not_burn_the_attempt_budget(db, bp, monkeypatch):
    """REQ-2026-0222, and the timestamps are the whole story:

        17:26:42.914   refused
        17:26:42.981   refused
        17:26:43.028   refused

    Three attempts in 114 MILLISECONDS. SQL Server was requested while the LIVE
    pricing API was not answering; `make_price` returned None, the cost gate
    refused — correctly — and the loop read that as a failed draft and redrafted
    into the same outage. SQL Server was then written off as "the agent could
    not produce a recipe that passed", which was never true: the recipe was
    never reached.

    A refusal comes from the preflight or the cost gate. Neither says anything
    about the draft, so neither may cost an attempt.
    """
    allow(monkeypatch)
    from api.proof import ProofOutcome

    tried = []

    def run_proof(session, blueprint):
        tried.append(1)
        return ProofOutcome("PROOF-X", "refused",
                            "The plan could not be priced, so the cost cap "
                            "cannot be checked. Refusing rather than building blind.")

    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=run_proof, publish=Publisher())

    assert len(tried) == 1, (
        f"the budget was spent {len(tried)} times on a refusal that no redraft "
        f"could have changed")
    assert result.status == "refused", result.status
    assert "could not be priced" in result.detail, (
        "the reason was replaced by 'could not produce a recipe that passed', "
        "which blames the draft for an external outage")


# --- the recipe has to be in the store (PROOF-MYSQL-20260902T123737) ------------

def test_a_proof_that_publishes_nothing_is_not_a_pass(db, bp, monkeypatch):
    """THE DEFECT. That proof reported "Built, verified and destroyed on attempt
    1" and named a profile which was in neither container nor on the host.
    `publish` returns what it wrote; the caller threw that away and reported
    what was PROPOSED instead.

    A recipe that is not in the store builds a machine with nothing installed on
    it -- make_publish's own comment says exactly that -- while every step
    reports success.
    """
    allow(monkeypatch)
    pub = Publisher(writes=False)

    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)

    assert result.status == "failed", (
        "a proof whose recipe never reached the store was reported as published")
    assert pub.calls, "it did not even try to publish"
    assert not result.files, "it still claims files that are not there"
    assert "does not contain" in result.detail or "did not reach" in result.detail


def test_a_partly_written_recipe_is_detected():
    """A profile without its module, or a module without its profile, is a
    recipe with a hole in it.

    The RULE is tested here rather than a build, because how many files a draft
    proposes is not this test's to control -- an earlier version asserted only
    when the draft happened to propose more than one, which meant it could not
    fail.
    """
    proposed = {"generated/profiles/x.json": "{}",
                "generated/terraform/main.tf": "resource {}"}
    written = ["/generated/store/x.json"]          # the module never landed

    missing = autobuild._stored_names(proposed) - autobuild._stored_names(written)

    assert missing == {"main.tf"}, missing


def test_writing_something_else_is_not_writing_the_recipe(db, bp, monkeypatch):
    """A store that takes A file is not a store that took THIS file.

    The check has to compare what was asked for against what came back, not
    merely notice that the list is non-empty. Testing the rule in isolation left
    that open: a version reading "no complaint if anything at all was written"
    passed every test here while a recipe with a hole in it sailed through.

    This drives the real build, so the comparison in `build` is what answers.
    """
    allow(monkeypatch)

    class WroteSomethingElse(Publisher):
        def __call__(self, files):
            self.calls.append(files)
            return ["/generated/store/not-the-recipe.json"]

    pub = WroteSomethingElse()
    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)

    assert result.status == "failed", (
        "the store returned a file nobody asked for and the proof called it "
        "published")
    assert not result.files


def test_the_comparison_survives_the_two_spellings():
    """A draft names `generated/profiles/x.json`; the store returns
    `/generated/profiles/x.json`. Comparing whole paths would find NOTHING in
    common and fail every build -- or, with the sides swapped, match nothing and
    pass every one."""
    proposed = {"generated/profiles/x.json": "{}"}
    written = ["/generated/profiles/x.json"]

    assert not (autobuild._stored_names(proposed)
                - autobuild._stored_names(written))


# --- a recipe has to claim something a machine could refute ---------------------

def _profile(**fields):
    import json
    return {"generated/profiles/thing.json": json.dumps({"code": "thing", **fields})}


def test_a_recipe_that_claims_nothing_is_refused():
    """A PROOF CAN ONLY REFUTE A CLAIM. No port to serve and no version to ask
    means a machine can build it, report success, and have established nothing
    about whether the software is there at all.

    This is Oracle's shape when its container binds nothing inside the watch:
    container profiles carry no version command by design -- an image's version
    label is usually its base OS -- so ports are the only claim they have.
    """
    assert autobuild._claims_nothing(_profile(ports=[])) == "thing"
    assert autobuild._claims_nothing(_profile(ports=[], version_command="")) == "thing"
    assert autobuild._claims_nothing(_profile()) == "thing"


def test_a_runtime_that_serves_nothing_is_fine():
    """NOT "must serve". `dotnet8` has no ports and is perfectly good -- it is a
    runtime, and its version is what a machine checks. Demanding ports would
    condemn it, exactly as demanding every declared port would condemn
    RabbitMQ."""
    assert autobuild._claims_nothing(
        _profile(ports=[], version_command="dotnet8 --version 2>&1")) == ""


def test_a_service_with_ports_and_no_version_is_fine():
    """Every container profile in the store is this shape."""
    assert autobuild._claims_nothing(_profile(ports=[9200])) == ""


def test_a_draft_with_no_profile_is_not_this_gates_business():
    """A Terraform module is judged by the build. Inventing a complaint here
    would refuse work this rule was never about."""
    assert autobuild._claims_nothing({"generated/terraform/main.tf": "resource {}"}) == ""
    assert autobuild._claims_nothing({}) == ""


def test_every_recipe_already_in_the_store_still_passes():
    """SURVEYED BEFORE ENABLING, which is the lesson from the certification gate
    that made bare VMs unprovisionable for twelve days. A rule that condemns
    work already done is a rule that gets switched off in a hurry."""
    import json
    import pathlib

    store = pathlib.Path("generated/profiles")
    if not store.is_dir():
        import pytest
        pytest.skip("no generated store in this checkout")

    condemned = {}
    for f in sorted(store.glob("*.json")):
        silent = autobuild._claims_nothing({str(f): f.read_text(encoding="utf-8")})
        if silent:
            condemned[f.stem] = json.loads(f.read_text(encoding="utf-8"))

    assert not condemned, (
        f"enabling this would condemn recipes already certified: "
        f"{sorted(condemned)}")


def test_a_build_whose_recipe_claims_nothing_does_not_publish(db, bp, monkeypatch):
    """The rule reaching the build, not just existing beside it."""
    allow(monkeypatch)
    pub = Publisher()
    monkeypatch.setattr(autobuild, "_claims_nothing", lambda files: "thing")

    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)

    assert result.status == "failed", "a recipe claiming nothing was published"
    assert pub.calls == [], "it reached the store despite claiming nothing"
    assert not result.files
    assert "claims nothing" in result.detail or "claimed nothing" in result.detail


# --- installed is not running (MySQL certified as a client) ---------------------

def _recipe(**fields):
    import json
    return {"generated/profiles/thing.json": json.dumps({"code": "thing", **fields})}


def test_a_client_is_not_the_service_it_was_asked_for():
    """THE DEFECT. On Oracle Linux the package `mysql` is the command-line
    CLIENT; the server is `mysql-server`. The package rung is tried first when
    the repositories carry the name, so the agent installed the client, wrote
    `services: []`, opened no port, asked `mysql --version`, got an answer, and
    every gate passed. A requester would have had a machine with no database.

    It slipped past `_claims_nothing` because it DOES claim something. It claims
    the wrong kind of thing.
    """
    recipe = _recipe(ports=[], version_command="mysql --version 2>&1",
                     rhel={"packages": ["mysql"], "services": []})

    assert autobuild._installed_but_not_running(recipe, [3306, 33060]) == "thing"


def test_a_runtime_is_not_condemned_for_serving_nothing():
    """`dotnet8` serves nothing and is perfectly correct -- a runtime is files
    you invoke. Its image declares no ports, so the vendor claims nothing and
    there is nothing to contradict. A rule that fired here would condemn every
    runtime in the catalogue to catch one database."""
    recipe = _recipe(ports=[], version_command="dotnet8 --version 2>&1",
                     rhel={"packages": ["dotnet-sdk-8.0"], "services": []})

    assert autobuild._installed_but_not_running(recipe, []) == ""
    assert autobuild._installed_but_not_running(recipe, None) == ""


def test_a_recipe_that_serves_a_port_is_fine():
    recipe = _recipe(ports=[3306], rhel={"packages": ["mysql-server"]})

    assert autobuild._installed_but_not_running(recipe, [3306, 33060]) == ""


def test_a_recipe_that_starts_a_unit_is_running_even_before_its_ports_are_known():
    """The first container pass publishes no ports on purpose and asks the
    machine what it bound. A package recipe that starts a service is a running
    thing whether or not its ports have been narrowed yet -- refusing it would
    break the narrowing pass."""
    recipe = _recipe(ports=[], rhel={"packages": ["mysql-server"],
                                     "services": ["mysqld"]})

    assert autobuild._installed_but_not_running(recipe, [3306]) == ""


def test_every_recipe_in_the_store_survives_its_own_vendors_claim():
    """SURVEYED BEFORE ENABLING. Each judged against what its own image
    declares, because a rule that condemns work already certified is a rule
    that gets switched off in a hurry."""
    import json
    import pathlib

    store = pathlib.Path("generated/profiles")
    if not store.is_dir():
        import pytest
        pytest.skip("no generated store in this checkout")

    # What each publisher's image declares, measured against the live registries.
    declares = {"dotnet8": [], "keycloak": [8080, 8443, 9000], "mongodb": [27017],
                "mssql": [1433], "rabbitmq": [5672], "vault": [8200],
                "opensearch": [9200]}

    condemned = []
    for f in sorted(store.glob("*.json")):
        if f.stem not in declares:
            continue                      # not surveyed; mysql is the known bad one
        if autobuild._installed_but_not_running(
                {str(f): f.read_text(encoding="utf-8")}, declares[f.stem]):
            condemned.append(f.stem)

    assert not condemned, f"this would condemn recipes already certified: {condemned}"


def test_a_build_whose_recipe_installs_but_runs_nothing_does_not_publish(
        db, bp, monkeypatch):
    """The rule reaching the build, not merely existing beside it."""
    allow(monkeypatch)
    pub = Publisher()
    monkeypatch.setattr(autobuild, "_installed_but_not_running",
                        lambda files, declares: "thing")

    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub,
                             image_declares=[3306])

    assert result.status == "failed", "a client-only recipe was published"
    assert pub.calls == [], "it reached the store anyway"
    assert not result.files


def test_without_the_vendors_claim_the_rule_cannot_fire(db, bp, monkeypatch):
    """`build` is also called by the admin endpoint and the certification
    runner, which may not have resolved an image. No evidence, no verdict --
    it must not invent one."""
    allow(monkeypatch)
    pub = Publisher()

    result = autobuild.build("cassandra5", db, blueprint=bp,
                             run_proof=proves("passed"), publish=pub)

    assert result.status == "published", result.detail


def test_the_request_path_hands_the_vendors_claim_to_the_build():
    """THE WIRING, which a plant showed nothing was testing.

    `ensure` is the path a real request takes and the only place the image's
    declaration is gathered; `build` is where the rule runs. Cut the wire and
    the rule still exists, still has passing tests, and never fires for a
    requester -- the same two-doors defect `must publish` had.

    READ FROM THE SOURCE, and deliberately. Driving `ensure` far enough to reach
    `build` needs six collaborators stubbed into agreeing, and a test that
    elaborate tends to prove things about the stubs. The repository already does
    this where a wiring guarantee is what matters -- see
    test_a_machine_is_never_verified_by_its_power_state. What is asserted is
    narrow and exact: the call passes the evidence.
    """
    import pathlib

    source = pathlib.Path("api/autobuild.py").read_text(encoding="utf-8")
    # The statement, up to the blank line that ends it. A regex over balanced
    # parentheses was the first attempt and it did not match the real call --
    # which has two levels of nesting -- so the test failed whether or not the
    # wire was cut, and a plant looked "caught" when it was merely failing for
    # a second reason.
    at = source.index("built = build(")
    call = source[at:source.index(chr(10) + chr(10), at)]

    assert "image_declares=" in call, (
        "ensure does not hand the image's declaration to build, so the rule "
        "cannot fire for a real request")
    assert "consulted" in call, (
        "the declaration passed is not the one gathered while resolving the "
        "ladder")
