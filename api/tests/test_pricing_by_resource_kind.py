"""A bucket is not a virtual machine, and must not be billed as one.

FOUND 2026-08-21 while the agent proved `oci-objectstorage`. The proof reported
a monthly cost of AED 162.25 — the exact figure it had reported for an NGINX VM,
and for a Kubernetes cluster, and for a managed PostgreSQL database. Every OCI
component came to the same number because the estimate was driven by SIZE alone:
small/medium/large chose a VM shape, and that shape's compute was charged to
everything. Object Storage was billed AED 158.85 of compute for CPUs it has none
of.

Nothing broke that day — both proofs were genuinely under the cap and the
resources really were built and destroyed. What broke was the meaning of the
number: a cost ceiling that cannot tell a bucket from a cluster is a size check
wearing a price's clothes.

The worst part was `resolved: True`. The module can admit ignorance — feed it a
nonsense component and it correctly returns 0.0 with resolved False — but a
bucket looks like a valid component, so it took the VM path and answered
confidently.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import pricing
from db.models import Blueprint
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    for code, kind in (("nginx", "oci-service-vm"), ("oci-objectstorage", "oci-bucket"),
                       ("oci-oke", "oci-oke"), ("postgres16", "oci-postgres")):
        s.add(Blueprint(technology_code=code, deployment_target="oci",
                        blueprint_ref=f"oci/{code}", resource_kind=kind,
                        status="certified", certified_by="test"))
    s.commit()
    yield s
    s.close()


def line(db, code, size="small", target="oci", **extra):
    comp = {"technology_code": code, "size": size, **extra}
    return pricing.estimate_cost([comp], target, db)["lines"][0]


# --- the defect itself -------------------------------------------------------

def test_four_different_resources_no_longer_cost_exactly_the_same(db):
    """THE symptom. A bucket, a VM, a cluster and a database at AED 162.25 each."""
    monthlies = {c: line(db, c)["monthly"] for c in
                 ("nginx", "oci-objectstorage", "oci-oke", "postgres16")}
    assert len(set(monthlies.values())) > 1, (
        f"every kind still prices identically: {monthlies}")


def test_a_bucket_is_charged_no_compute_at_all(db):
    """It has no OCPUs. It was being billed AED 158.85 of them."""
    li = line(db, "oci-objectstorage")
    assert li["billing_model"] == pricing.BILLING_BUCKET
    assert li["compute_monthly"] == 0.0, "a bucket was charged for CPUs"


def test_a_bucket_costs_far_less_than_a_machine(db):
    """The direction matters as much as the number: storage-only must be
    dramatically cheaper than a machine, or nothing has really changed."""
    assert line(db, "oci-objectstorage")["monthly"] < line(db, "nginx")["monthly"] / 10


def test_a_vm_is_priced_exactly_as_it_always_was(db):
    """The VM model was never wrong. Six of the nine certified components are
    service VMs and their figures must not move."""
    li = line(db, "nginx")
    assert li["billing_model"] == pricing.BILLING_VM
    assert li["monthly"] == 162.25


def test_a_cluster_pays_for_its_control_plane(db):
    """OKE charges a flat fee per cluster/hour on top of the node pool. Pricing
    it as bare nodes understates every Kubernetes request."""
    li = line(db, "oci-oke")
    assert li["billing_model"] == pricing.BILLING_CLUSTER
    assert li["compute_monthly"] > line(db, "nginx")["compute_monthly"]


def test_a_managed_database_uses_its_own_rate(db):
    """Managed PostgreSQL is not billed at the VM OCPU rate."""
    li = line(db, "postgres16")
    assert li["billing_model"] == pricing.BILLING_MANAGED_DB


# --- (a) the honest floor ----------------------------------------------------

def test_an_unknown_kind_is_refused_rather_than_billed_as_a_vm(db):
    """THE property. 'We do not know what this costs' must not come out looking
    like a number somebody can approve."""
    db.add(Blueprint(technology_code="cassandra5", deployment_target="oci",
                     blueprint_ref="oci/cassandra5", resource_kind="oci-something-new",
                     status="certified", certified_by="test"))
    db.commit()

    li = line(db, "cassandra5")
    assert li["resolved"] is False, "an unrecognised kind was priced anyway"
    assert li["monthly"] == 0.0
    assert li["billing_model"] is None


def test_an_unpriceable_component_makes_the_proof_refuse(db):
    """check_cost refuses on None, and make_price returns None when a plan
    cannot be priced — so an unknown kind stops a proof instead of letting it
    spend money against a meaningless ceiling."""
    from common.proof_rules import check_cost
    assert check_cost(None).allowed is False


def test_the_caller_may_name_the_kind_before_a_blueprint_exists(db):
    """A proof prices a component that is NOT yet certified — that is the whole
    point of it — so there is no blueprint row to read. The runner knows which
    manifest it is about to build and says so."""
    li = line(db, "brand-new-thing", resource_kind="oci-bucket")
    assert li["billing_model"] == pricing.BILLING_BUCKET
    assert li["compute_monthly"] == 0.0


def test_technology_resource_kind_is_never_used_as_a_fallback(db):
    """It defaults to "oci-bucket" and calls NGINX a bucket. Trusting it would
    price a machine as storage — roughly AED 1 instead of 162 — and slide under
    every cost cap before building the expensive thing anyway.
    """
    from db.models import Technology
    from sqlalchemy import select

    tech = db.scalar(select(Technology).where(Technology.code == "nginx"))
    assert tech.resource_kind == "oci-bucket", "premise changed; revisit this test"

    db.query(Blueprint).filter_by(technology_code="nginx", deployment_target="oci").delete()
    db.commit()

    li = line(db, "nginx")
    assert li["resolved"] is False, "it fell back to Technology.resource_kind"
    assert li["monthly"] == 0.0, "an uncertified VM was priced as a bucket"


# --- targets without blueprints must not regress -----------------------------

@pytest.mark.parametrize("target", ["onprem", "azure", "aws", "gcp"])
def test_a_target_with_no_blueprints_prices_exactly_as_before(db, target):
    """Only OCI has blueprints. On the others nothing can name a kind, and
    everything in the catalogue is software on a machine — so the VM model is
    not an assumption there, it is the whole model."""
    li = line(db, "nginx", target=target)
    assert li["resolved"] is True, f"{target} pricing was zeroed out"
    assert li["monthly"] > 0
    assert li["billing_model"] == pricing.BILLING_VM


def test_a_caller_cannot_relabel_a_certified_component_to_pay_less(db):
    """THE client holds no authority (ARCHITECTURE.md P2). `components` arrives
    from the browser on /api/cost; if an explicit resource_kind could override a
    certified one, anybody could declare a VM to be a bucket and be quoted AED 3
    instead of 162 — a client deciding its own price.
    """
    honest = line(db, "nginx")["monthly"]
    lying = line(db, "nginx", resource_kind="oci-bucket")["monthly"]

    assert lying == honest, "a caller relabelled a VM as a bucket and was believed"
    assert lying == 162.25


def test_the_proof_prices_the_component_it_is_about_to_build(db):
    """Without this the whole self-certification loop deadlocks: a new component
    has no blueprint, so its kind is unknown, so it cannot be priced, so
    check_cost refuses, so it is never certified, so it never gets a blueprint.
    """
    import inspect
    from api import proof

    src = inspect.getsource(proof.run_proof)
    assert '"resource_kind": blueprint.resource_kind' in src, (
        "the proof no longer tells pricing what kind of thing it is building")


def test_a_missing_rate_row_refuses_instead_of_charging_zero(db):
    """FOUND IN THE RUNNING SYSTEM, 2026-08-21, minutes after this was written.

    The code was correct and the deployed database had never received the new
    rate rows — seeding only ever ran on a fresh database. So the OKE control
    plane defaulted to 0.00 and a cluster was quoted AED 162.25 with every
    appearance of confidence, in the very change written to stop exactly that.
    """
    from db.models import RateCard

    db.query(RateCard).filter_by(kind="cloud_oci", item="oke-cluster-hour").delete()
    db.commit()

    li = line(db, "oci-oke")
    assert li["resolved"] is False, "a cluster was priced without its control-plane rate"
    assert li["monthly"] == 0.0
    assert "oke-cluster-hour" in (li["unpriceable"] or ""), "it did not say what was missing"


def test_a_complete_rate_card_prices_normally(db):
    """The refusal must be about the missing row, not about clusters."""
    li = line(db, "oci-oke")
    assert li["resolved"] is True and li["unpriceable"] is None
