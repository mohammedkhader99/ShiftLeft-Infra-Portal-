"""A requester is told whether they are getting a service or a machine.

"PostgreSQL" and "Kafka" sit next to each other on one catalogue and arrive as
completely different things. One is a database OCI runs, patches and backs up.
The other is machines that somebody — the requester's own team — now owns and
must patch. Nothing on the request form said which, and the difference is not
cosmetic: it is a running cost and an operational burden that lands on a team
that never agreed to it.

READ FROM THE BLUEPRINT THAT WILL ACTUALLY BUILD IT. Not from the technology's
name, which is the guess db/models.TechnologyDelivery was written to end
(`postgres16` reads as software by that rule and is a managed service), and not
from a hand-kept table beside the catalogue, which is the second source of truth
that every defect this month has turned out to be.

WHAT THIS DELIBERATELY DOES NOT DO. It does not offer a CHOICE. No technology has
two deliveries today, and `blueprint` is keyed on (technology_code,
deployment_target) — so a second certified delivery has nowhere to live. A
selector the platform could not honour would be the catalogue's "certified" badge
problem with a radio button in front of it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import blueprint_capabilities, component_options
from db.seed import DELIVERY, seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


@pytest.fixture(autouse=True)
def clean_cache():
    """The capability cache is module-level and would leak between tests."""
    blueprint_capabilities.reset()
    yield
    blueprint_capabilities.reset()


def shipped(*manifests):
    return lambda: list(manifests)


MANAGED = {"ref": "oci/postgres", "target": "oci", "resource_kind": "oci-postgres",
           "delivery": "managed", "builds": ["postgres16"],
           "description": "Managed OCI Database with PostgreSQL."}
ON_A_VM = {"ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
           "delivery": "vm", "builds": ["nginx", "redis7"],
           "description": "A service installed on a private OCI Compute instance."}


# --- what the orchestrator says --------------------------------------------------

def test_a_technology_is_mapped_to_how_it_is_built():
    fetch = shipped(MANAGED, ON_A_VM)

    assert [o["delivery"] for o in
            blueprint_capabilities.deliveries_for("postgres16", fetch)] == ["managed"]
    assert [o["delivery"] for o in
            blueprint_capabilities.deliveries_for("nginx", fetch)] == ["vm"]


def test_a_blueprint_that_does_not_say_is_not_counted():
    """Unclassified and `vm` are different facts. Defaulting one to the other
    would put a delivery on the form that nobody declared — which is precisely
    the inference this replaces."""
    silent = {**ON_A_VM, "delivery": ""}

    assert blueprint_capabilities.deliveries_for("nginx", shipped(silent)) == []


def test_a_rejected_blueprint_is_not_counted():
    """discover() reports a shadowed or invalid manifest with an `error` rather
    than dropping it, so the portal can say what went wrong. It must not then
    advertise it as a way to get software."""
    broken = {**ON_A_VM, "error": "a shipped blueprint already uses this ref"}

    assert blueprint_capabilities.deliveries_for("nginx", shipped(broken)) == []


def test_the_cloud_is_recorded_with_the_option():
    """Without it a request against AWS would be told how the OCI blueprint
    delivers, which is a different product on a different bill."""
    options = blueprint_capabilities.deliveries_for("postgres16", shipped(MANAGED))

    assert options[0]["target"] == "oci"


# --- what the form is given ------------------------------------------------------

def test_the_form_is_told_it_is_a_managed_service(db, monkeypatch):
    monkeypatch.setattr(component_options, "_fetch_blueprints", shipped(MANAGED, ON_A_VM))

    delivery = component_options.delivery_for(db, "postgres16", "oci")

    assert delivery["chosen"] == "managed"
    assert delivery["options"][0]["label"] == "Managed service"
    assert "no machine to patch" in delivery["options"][0]["meaning"]


def test_the_form_is_told_it_is_a_machine(db, monkeypatch):
    monkeypatch.setattr(component_options, "_fetch_blueprints", shipped(MANAGED, ON_A_VM))

    delivery = component_options.delivery_for(db, "nginx", "oci")

    assert delivery["chosen"] == "vm"
    assert delivery["options"][0]["label"] == "Virtual machine"


def test_a_blueprint_for_another_cloud_is_not_offered(db, monkeypatch):
    """Requesting on AWS must not be told how the OCI module delivers."""
    monkeypatch.setattr(component_options, "_fetch_blueprints", shipped(MANAGED))

    assert component_options.delivery_for(db, "postgres16", "aws")["options"] == []
    assert component_options.delivery_for(db, "postgres16", "oci")["options"] != []


def test_an_unreachable_orchestrator_is_not_reported_as_an_absence(db, monkeypatch):
    """`known` False and "nothing builds this" are different facts, and only one
    of them is the requester's problem. Conflating them is how the
    Apache-on-Ubuntu filter came to be silently inert in production."""
    monkeypatch.setattr(component_options, "_fetch_blueprints", lambda: [])

    delivery = component_options.delivery_for(db, "nginx", "oci")

    assert delivery["options"] == []
    assert delivery["known"] is False


def test_the_form_payload_carries_it(db, monkeypatch):
    """options_for is what the request form actually reads."""
    monkeypatch.setattr(component_options, "_fetch_blueprints", shipped(MANAGED, ON_A_VM))

    payload = component_options.options_for(db, "postgres16", "oci")

    assert payload["delivery"]["chosen"] == "managed"


def test_more_than_one_way_is_reported_and_not_silently_collapsed(db, monkeypatch):
    """When a second delivery does exist the requester must SEE that it exists,
    even while the platform still picks. Showing one and hiding the other is the
    catalogue lying by omission."""
    also_a_vm = {**ON_A_VM, "ref": "oci/postgres-vm", "resource_kind": "oci-postgres-vm",
                 "builds": ["postgres16"]}
    monkeypatch.setattr(component_options, "_fetch_blueprints",
                        shipped(MANAGED, also_a_vm))

    delivery = component_options.delivery_for(db, "postgres16", "oci")

    assert [o["delivery"] for o in delivery["options"]] == ["managed", "vm"]
    assert delivery["chosen"] == "managed"


# --- the two places that record this must not drift ------------------------------

CATALOGUE_TO_BLUEPRINT = {"managed": {"managed"}, "software": {"vm"}, "machine": {"vm"}}


def test_the_catalogue_and_the_blueprints_agree_about_what_is_managed():
    """TWO PLACES RECORD THIS FACT and they must not contradict each other.

    `db.seed.DELIVERY` says what the catalogue entry IS; a blueprint's `delivery`
    says how it is BUILT. They answer different questions and their answers have
    to line up — a catalogue that calls postgres16 a managed service while the
    blueprint building it puts software on a VM would price one thing and deliver
    another.

    Every defect found this month was two sources of truth for one fact with
    nothing checking them against each other. This is the check.
    """
    from orchestrator import blueprint_registry

    disagreements = []
    for bp in blueprint_registry.discover():
        built = bp.get("delivery")
        if not built:
            continue
        for code in bp.get("builds") or []:
            classified = (DELIVERY.get(code) or (None, ""))[0]
            if classified is None:
                continue  # not classified; the agent's ladder falls back
            allowed = CATALOGUE_TO_BLUEPRINT.get(classified)
            if allowed and built not in allowed:
                disagreements.append(
                    f"{code}: the catalogue calls it {classified!r} but "
                    f"{bp['ref']} delivers it as {built!r}")

    assert not disagreements, "\n".join(disagreements)
