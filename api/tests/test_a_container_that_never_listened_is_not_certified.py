"""A container that never bound its own service port did not start.

REQ-2026-0225. SQL Server was started with no MSSQL_SA_PASSWORD, so it printed
its complaint and exited. Then:

    the image DECLARES   [1433]        the database
    the machine SAW      [135]         MSSQL_RPC_PORT, which is not the database
    the recipe PUBLISHED [135]
    the second proof     confirmed 135 was bound  -> PASSED
    mssql                CERTIFIED

A container that had never served a query, certified, and a real VM provisioned
from it — with a firewall opened on the wrong port for a database nobody could
reach. It broke the reviewer's standing rule directly: "once you certify the
component it should not fail when the user selects it."

THE NARROWING PASS IS WHERE IT GOT IN. It exists to reduce six declared ports
to the two a container really binds, and it took "whatever it bound" as the
answer. That is a specification when the container came up, and a fiction when
it did not. The gap between what the publisher declares and what the machine
found is the evidence — and it was being read as a narrower spec instead of as
a warning.

AT LEAST ONE, NOT ALL. `library/rabbitmq` declares six ports — AMQP, AMQPS,
epmd, clustering and two Prometheus endpoints — and a default container binds
fewer. Demanding the whole declared set would refuse healthy containers;
demanding none of it certified a dead one.

Nothing here reaches the network or a machine.
"""

from __future__ import annotations

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
    "resource_kind": "oci-service-vm", "builds": ["mssql"], "version": "1.0.0",
}

#: Measured from mcr.microsoft.com/mssql/server on 2026-08-28.
MSSQL_IMAGE = {
    "image": "mcr.microsoft.com/mssql/server", "tag": "latest",
    "digest": "sha256:" + "d" * 64, "ports": [1433],
}

#: Measured from docker.io/library/rabbitmq: six declared, few ever bound.
RABBITMQ_IMAGE = {
    "image": "docker.io/library/rabbitmq", "tag": "latest",
    "digest": "sha256:" + "e" * 64,
    "ports": [4369, 5671, 5672, 15691, 15692, 25672],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
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


def _run(db, *, code="mssql", image=MSSQL_IMAGE, observed, proofs=("passed",)):
    """Drive the real ladder down its container rung with a scripted machine."""
    published, certified, withdrawn = [], [], []
    n = [0]

    def run_proof(session, manifest):
        n[0] += 1
        status = proofs[min(n[0] - 1, len(proofs) - 1)]
        return ProofOutcome(f"PROOF-{code.upper()}-{n[0]}", status,
                            "Built, verified, and torn down" if status == "passed"
                            else f"{code} NOT INSTALLED")

    result = autobuild.ensure(
        code, db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published else None,
        run_proof=run_proof,
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: withdrawn.append(files) or list(files),
        search=lambda c, family: [],
        find_image=lambda c: image,
        observe_ports=lambda ref, c: list(observed))
    return result, certified, withdrawn, n[0]


# --- the fiction ---------------------------------------------------------------

def test_a_container_that_never_bound_its_service_port_is_not_certified(db):
    """THE test. Declared 1433, bound 135 — that is not SQL Server running."""
    result, certified, _, _ = _run(db, observed=[135])

    assert certified == [], "a container that never served a query was certified"
    assert result.status != "published", result.detail


def test_a_container_that_bound_nothing_at_all_is_not_certified(db):
    """The same fiction in its other form. This path used to certify on the
    reasoning that "publishing no ports is the right recipe after all" — which
    is true for an image that declares none, and false for one that declares
    1433 and never opened it."""
    result, certified, _, _ = _run(db, observed=[])

    assert certified == []
    assert result.status != "published", result.detail


def test_the_recipe_is_withdrawn_when_it_did_not_come_up(db):
    """It must not be left in the store advertising itself — the defect that
    cost five requests before this one."""
    _, _, withdrawn, _ = _run(db, observed=[135])

    assert withdrawn, "the recipe that never came up is still in the store"


def test_the_failure_says_what_was_declared_and_what_was_seen(db):
    """A refusal must explain and guide, never just say no."""
    result, _, _, _ = _run(db, observed=[135])

    assert "1433" in result.detail, result.detail
    assert "135" in result.detail, result.detail
    assert "did not start" in result.detail


def test_it_does_not_spend_a_second_machine_on_a_container_that_never_started(db):
    """One proof, then stop. Re-proving a recipe for a service that never came
    up buys nothing but another VM."""
    _, _, _, proofs = _run(db, observed=[135])

    assert proofs == 1, f"{proofs} machines were built"


# --- and healthy containers are untouched --------------------------------------

def test_one_declared_port_is_enough(db):
    """RabbitMQ declares six and a default container binds fewer. Demanding the
    whole declared set would refuse containers that are working perfectly."""
    result, certified, _, _ = _run(
        db, code="rabbitmq", image=RABBITMQ_IMAGE, observed=[5672, 25672],
        proofs=("passed", "passed"))

    assert result.status == "published", result.detail
    assert certified


def test_an_image_that_declares_nothing_is_exempt(db):
    """There is no claim to check against, and plenty of legitimate images
    declare nothing at all."""
    silent = {**MSSQL_IMAGE, "ports": []}
    result, certified, _, _ = _run(db, image=silent, observed=[])

    assert result.status == "published", result.detail
    assert certified


def test_a_healthy_container_still_narrows_to_what_it_bound(db):
    """The narrowing pass must keep doing its job: publish exactly the ports the
    machine saw, and prove the changed recipe again."""
    result, certified, _, proofs = _run(
        db, observed=[1433], proofs=("passed", "passed"))

    assert result.status == "published", result.detail
    assert certified
    assert proofs == 2, "the narrowed recipe was certified on the old proof"
