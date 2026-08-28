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

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, files):
        self.calls.append(files)


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
