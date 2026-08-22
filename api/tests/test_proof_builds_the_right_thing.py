"""A proof must build the thing under test (found 2026-08-22, REQ-2026-0176).

THE WORST DEFECT OF THIS PROJECT SO FAR, and it hid behind three green proofs.

`run_proof` built its handoff payload with a reference, an idempotency key and a
policy_input — and no `resource_kind`. The orchestrator's fallback is
`payload.get("resource_kind", "oci-bucket")`, so EVERY proof ever run created a
bucket, destroyed it, and reported "built, verified, and torn down".

nginx was certified on the evidence of a bucket being created. So was keycloak,
whose profile installs a package that does not exist on Oracle Linux. Object
Storage was certified correctly by pure accident, being the one component that
really is a bucket.

The proof of it was in the cloud, not the code: across the whole of 21-22 August
exactly two compute instances were ever created, both by real user requests. Not
one proof had built a machine.

This is precisely the failure the certification design exists to prevent — a
badge on a component nobody proved — reproduced by the mechanism meant to
prevent it. `oci-oke` failed four consecutive real requests while certified by a
human click; keycloak was certified by a machine that built a bucket.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import proof
from db.models import Blueprint
from db.seed import seed
from db.session import Base


@pytest.fixture(autouse=True)
def _allow(monkeypatch):
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")
    monkeypatch.setenv("CERTIFICATION_SANDBOX_TIER", "Development")
    monkeypatch.setenv("CERTIFICATION_COST_CAP_MONTHLY", "300")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.add(Blueprint(technology_code="nginx", deployment_target="oci",
                    blueprint_ref="oci/service-vm", resource_kind="oci-service-vm",
                    status="draft"))
    s.commit()
    yield s
    s.close()


def bp(db):
    return db.get(Blueprint, ("nginx", "oci"))


class Handoff:
    """Records what was actually sent, and answers with a chosen plan."""

    def __init__(self, plan="oci-service-vm: Plan: 1 to add, 0 to change, 0 to destroy."):
        self.sent: list[tuple[str, dict]] = []
        self.plan = plan

    def __call__(self, path, payload):
        self.sent.append((path, payload))
        if path == "/provision":
            return True, '{"plan_summary": "%s"}' % self.plan
        return True, "{}"

    def paths(self):
        return [p for p, _ in self.sent]

    def payload(self):
        return self.sent[0][1]


def run(db, post):
    return proof.run_proof(db, bp(db), post=post, price=lambda c: 10.0,
                           verify=lambda r, p: (True, "healthy"))


# --- the handoff must say what to build --------------------------------------

def test_the_handoff_names_the_resource_kind_under_test(db):
    """Without this the orchestrator defaults to a bucket — which is how nginx
    came to be certified by the creation of an object store."""
    post = Handoff()
    run(db, post)
    sent = post.payload()

    assert sent.get("resource_kind") == "oci-service-vm", (
        "the proof did not say what to build, so the orchestrator will fall back "
        "to oci-bucket and prove the wrong thing")
    assert sent.get("resource_kinds") == ["oci-service-vm"]


def test_the_kind_travels_on_every_handoff(db):
    """A kind present on one call and missing from another would build a machine
    and tear down a bucket. (`verify` is a separate collaborator; that it carries
    the same payload is asserted in test_proof_wiring.)"""
    post = Handoff()
    run(db, post)

    assert post.paths() == ["/provision", "/apply", "/destroy"]
    for path, payload in post.sent:
        assert payload.get("resource_kind") == "oci-service-vm", path


def test_verify_is_given_the_whole_payload_not_a_fragment(db):
    """It used to be handed only the policy_input and to compose its own payload
    around it — which carried no resource_kind, so /verify asked about a bucket
    while /apply had built something else."""
    seen = {}

    def watching_verify(reference, payload):
        seen.update(payload)
        return True, "healthy"

    proof.run_proof(db, bp(db), post=Handoff(), price=lambda c: 10.0,
                    verify=watching_verify)

    assert seen.get("resource_kind") == "oci-service-vm"
    assert seen.get("policy_input"), "the components went missing"


# --- and the plan must agree -------------------------------------------------

def test_a_plan_for_a_different_kind_fails_the_proof(db):
    """THE guard that would have caught this. Terraform names the module in its
    summary; if a proof for oci-service-vm gets a plan for oci-bucket, the run
    proves nothing however cleanly it succeeds."""
    post = Handoff(plan="oci-bucket: Plan: 1 to add, 0 to change, 0 to destroy.")
    out = run(db, post)

    assert out.status == "failed"
    assert "does not build oci-service-vm" in out.detail
    assert "/apply" not in post.paths(), "it built the wrong thing anyway"


def test_a_plan_for_the_right_kind_proceeds(db):
    post = Handoff()
    out = run(db, post)
    assert out.status == "passed", out.detail


def test_an_unreadable_plan_is_not_assumed_to_be_the_right_kind(db):
    """Silence is not a verdict. The entire reason this check exists is that a
    proof was quietly building something else."""
    assert proof.plan_covers("", "oci-service-vm") is False
    assert proof.plan_covers("some other response", "oci-service-vm") is False
    assert proof.plan_covers("oci-service-vm: Plan: 1 to add", "") is False


def test_the_check_is_made_before_anything_is_built(db):
    """Cheapest place to learn it, and the only one where nothing has been
    created yet."""
    post = Handoff(plan="oci-bucket: Plan: 1 to add.")
    run(db, post)
    assert post.paths() == ["/provision"], (
        f"it went past the plan into {post.paths()}")
