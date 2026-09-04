"""The container rung is the last one, and a container that says why it will
not start was not being listened to.

`docker.io/library/mysql` exits immediately unless it is told how to set its
root password. The image declares nothing about one -- measured 2026-09-04, it
declares MYSQL_MAJOR, MYSQL_VERSION, MYSQL_SHELL_VERSION and its two ports --
so the only source is the log of a machine that tried, which the boot report
captures when a service never comes up:

    mysql log:     You need to specify one of the following as an environment variable:
    mysql log:     - MYSQL_ROOT_PASSWORD
    mysql log:     - MYSQL_ALLOW_EMPTY_PASSWORD
    mysql log:     - MYSQL_RANDOM_ROOT_PASSWORD

The ladder refuted the recipe, had no rung left, and stopped -- while the
machine that refuted it was holding the answer. The same shape as C7 for
packages, one rung further along.

WHAT MAY BE SUPPLIED is `api/container_env`'s decision, tested there. These
tests are about the LADDER: that it asks, that it retries once with what came
back, that a proved recipe carries it, and that a refusal a person must act on
ends the ladder with something a person can act on.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild
from api.proof import ProofOutcome
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci",
    "resource_kind": "oci-service-vm", "builds": ["mysql"], "version": "1.0.0",
}
MYSQL_IMAGE = {
    "image": "docker.io/library/mysql", "tag": "latest",
    "digest": "sha256:" + "a" * 64, "ports": [3306, 33060],
}
GENERATED = {"MYSQL_RANDOM_ROOT_PASSWORD": "yes"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE",
                "CONTAINER_LICENCE_ACCEPTED"):
        monkeypatch.delenv(key, raising=False)
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


def _recipe_of(files):
    return json.loads(next(v for k, v in files.items() if k.endswith(".json")))


def _run(db, *, supply, answering_is_enough=True):
    """The ladder, with a container that will not start until it is answered.

    The package search finds nothing, so the ladder reaches the vendor's image.
    A recipe with no environment models the real container: it comes up, binds
    nothing, and the proof fails. One that carries the answer starts --
    unless `answering_is_enough` is False, which is the container that was
    broken for some other reason all along.
    """
    published, certified, asked = [], [], []

    def run_proof(session, manifest):
        recipe = _recipe_of(published[-1])
        env = (recipe.get("container") or {}).get("environment") or {}
        if not env or not answering_is_enough:
            published.clear()
            return ProofOutcome("PROOF-MYSQL-1", "failed",
                                "mysql is not serving any port it declares: the image "
                                "declares 3306, 33060 and nothing inside the container "
                                "is listening at all. Its own log is in this report.")
        return ProofOutcome(f"PROOF-MYSQL-{len(asked) + 2}", "passed",
                            "Built, verified, and torn down")

    def container_needs(proof_reference, candidate):
        asked.append((proof_reference, candidate))
        return dict(supply)

    result = autobuild.ensure(
        "mysql", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published else None,
        run_proof=run_proof,
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: list(files),
        search=lambda c, family: [],
        find_image=lambda c: dict(MYSQL_IMAGE),
        discover=lambda ref, c: None,
        observe_ports=lambda ref, c: [3306],
        container_needs=container_needs)
    return result, published, certified, asked


# --- it asks, and it retries with the answer --------------------------------------

def test_a_container_that_will_not_start_is_asked_what_it_needs(db):
    _, _, _, asked = _run(db, supply={"environment": dict(GENERATED), "detail": "",
                                      "needs_a_person": []})

    assert asked and asked[0][1] == "mysql", (
        "the machine that refuted the recipe was never asked what it demanded")


def test_the_answer_reaches_the_recipe_that_is_certified(db):
    """And it is the generated password, never one this portal chose."""
    result, published, certified, _ = _run(
        db, supply={"environment": dict(GENERATED), "detail": "", "needs_a_person": []})

    assert certified, result.detail
    spec = _recipe_of(published[-1])["container"]
    assert spec["environment"] == GENERATED, spec.get("environment")


def test_it_is_asked_once(db):
    """The second attempt either starts or it does not. Asking again would buy
    the same answer for the price of another machine.

    DRIVEN WITH A CONTAINER THAT NEVER STARTS, which is the only case where
    asking twice is even possible. Written first with a container that starts
    on the second attempt, this passed against a version that had lost the
    once-only flag entirely -- a plant proved it. When the answer works the
    loop ends anyway, so it tested nothing.
    """
    _, published, _, asked = _run(db, supply={"environment": dict(GENERATED),
                                              "detail": "", "needs_a_person": []},
                                  answering_is_enough=False)

    assert len(asked) == 1, asked
    assert len(published) <= 3, (
        f"the container rung was drafted {len(published)} times: the retry is "
        f"not bounded, so a container that never starts buys a machine per pass")


def test_the_narrowed_ports_still_reach_the_recipe(db):
    """The environment retry must not cost the narrowing pass: what the machine
    bound is still what the recipe publishes."""
    result, published, certified, _ = _run(
        db, supply={"environment": dict(GENERATED), "detail": "", "needs_a_person": []})

    assert certified, result.detail
    assert _recipe_of(published[-1])["ports"] == [3306]


# --- and when only a person can answer, it says so --------------------------------

BLOCKED = {
    "environment": {},
    "needs_a_person": ["POSTGRES_PASSWORD"],
    "detail": ("postgres will not start until POSTGRES_PASSWORD is supplied, and "
               "this portal must not choose it: a password is a secret. An operator "
               "adds it to secrets/container.env as `postgres.<NAME>=<value>`."),
}


def test_a_password_only_a_person_can_supply_ends_the_ladder_with_guidance(db):
    """This is the end of the road, so what it says is what a person acts on.
    "Nothing was built" with no reason is the refusal this project keeps
    replacing with one that explains and guides."""
    result, _, certified, _ = _run(db, supply=BLOCKED)

    assert certified == [], "something was certified for a container that never started"
    assert "POSTGRES_PASSWORD" in result.detail
    assert "secrets/container.env" in result.detail


MIXED = {
    "environment": {"ACCEPT_EULA": "Y"},
    "needs_a_person": ["MSSQL_SA_PASSWORD"],
    "detail": ("mssql will not start until MSSQL_SA_PASSWORD is supplied, and this "
               "portal must not choose it: a password is a secret. An operator adds "
               "it to secrets/container.env as `mssql.<NAME>=<value>`."),
}


def test_an_answer_that_is_partly_supplyable_and_still_blocked_refuses(db):
    """FOUND BY REVIEW, 2026-09-04. `container_env` models an answer that can
    set SOME variables and is still stopped by others -- an operator has
    accepted the licence, so ACCEPT_EULA can be set, while MSSQL_SA_PASSWORD is
    a secret nothing here may choose. Its own test asserts all three at once:
    environment, needs_a_person, blocked.

    The ladder read only `environment`, retried with the half it could set, and
    the guidance naming the missing secret was discarded. Worse, the licence is
    already in the first draft whenever the setting is on, so the retry redrafts
    a byte-identical recipe -- which the memory then refuses as already refuted.
    The requester is told nothing they can act on."""
    result, _, certified, asked = _run(db, supply=MIXED)

    assert certified == []
    assert len(asked) == 1
    assert "MSSQL_SA_PASSWORD" in result.detail, (
        "the ladder retried with the half it could set and dropped the refusal "
        "that names what a person must supply")
    assert "secrets/container.env" in result.detail


def test_the_guidance_survives_the_cut_the_api_makes(db):
    """`_autobuild_component` returns `result.detail[:300]`, and that dict is the
    whole payload of the `autobuild.finished` audit entry -- the only place this
    text reaches a person. Appended after take_it_back's own long sentence, the
    operative half fell off the end: an operator learnt the variable's name and
    not that it goes in secrets/container.env."""
    result, _, _, _ = _run(db, supply=BLOCKED)

    as_the_api_returns_it = result.detail[:300]
    assert "POSTGRES_PASSWORD" in as_the_api_returns_it
    assert "secrets/container.env" in as_the_api_returns_it, (
        f"the guidance is cut off at the API boundary: {as_the_api_returns_it!r}")


def test_the_summary_counts_install_methods_not_machines(db):
    """The retry runs the container rung a second time, and the closing summary
    counted each machine as an install method: "Tried 2 install methods
    (container, container)". An approver reads that sentence as evidence."""
    result, _, _, _ = _run(db, supply={"environment": dict(GENERATED),
                                       "detail": "", "needs_a_person": []},
                           answering_is_enough=False)

    assert "container, container" not in result.detail, result.detail
    assert "Tried 2 install methods" not in result.detail, (
        "one install method, tried twice, was counted as two")


def test_nothing_is_invented_when_the_machine_asked_for_nothing(db):
    """Most images start with no environment at all. An empty answer leaves the
    ladder exactly as it was, and the container's failure stands on its own."""
    result, published, certified, asked = _run(
        db, supply={"environment": {}, "detail": "", "needs_a_person": []})

    assert len(asked) == 1
    assert certified == []
    assert all(not (_recipe_of(f).get("container") or {}).get("environment")
               for f in published)


def test_a_ladder_with_no_collaborator_behaves_as_before(db):
    """`container_needs` is injected like every other collaborator, and its
    absence must not change what the ladder does."""
    published, certified = [], []
    result = autobuild.ensure(
        "mysql", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published else None,
        run_proof=lambda s, m: ProofOutcome("PROOF-X", "failed", "bound nothing"),
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: list(files),
        search=lambda c, family: [],
        find_image=lambda c: dict(MYSQL_IMAGE),
        discover=lambda ref, c: None,
        observe_ports=lambda ref, c: [])

    assert certified == []
    assert result.status != "published"
