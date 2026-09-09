"""The whole chain, once, from a clean database (P.14 — the phase).

Three components in, and out the other end: a layout the requester chose, a shape
that is the SUM of what they asked for, a cost derived from that shape, a Jira
ticket an approver can read, and a Terraform plan that builds exactly those
machines.

WHY ONE TEST AND NOT SIX. Every link in this chain already has its own tests, and
every one of them passed while the link next to it was broken. P.11 found two
defects that way, P.12 three, P.13 three — eight in code with green suites, each
exposed only when a new consumer read the output. The unit tests check that each
piece is right; this one checks that they are joined, which is a different claim
and the one that kept failing.

The chain, and what has to survive each hop:

    components   ->  the size each was requested at
    placement    ->  which layout, and one host per machine
    sizing       ->  the SUM of the co-resident requirements, plus headroom
    cost         ->  priced per machine, not per component
    Jira         ->  the topology in words, and the post-placement figure
    handoff      ->  the layout, signed
    Terraform    ->  one workspace per machine, at the shape that was priced

`test_cutting_any_link_breaks_the_chain` is the acceptance clause "and fails if
any link in that chain is broken", asserted rather than hoped for: a chain test
nobody has tried to break is a chain test that might be asserting nothing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
import orchestrator.main as omain
from api.placement_store import current_placement
from db.models import Blueprint, Estimate, Request
from db.seed import seed
from db.session import Base

# postgres16 at medium and nodejs20 at small, on a VM that hosts them.
# compute-vm is the machine itself, not a workload competing for room on one.
COMPONENTS = [
    {"technology_code": "compute-vm", "size": "small"},
    {"technology_code": "postgres16", "size": "medium"},
    {"technology_code": "nodejs20", "size": "small"},
]

# THE ARITHMETIC THIS CHAIN MUST PRODUCE, worked out from the seeded requirement
# rows rather than copied from a passing run:
#   postgres16 as a vm, medium  -> 4 vCPU, 16 GB, 200 GB   (recommended)
#   nodejs20   as a vm, small   -> 2 vCPU,  4 GB,  50 GB
#   consolidated onto one host  -> 6 vCPU, 20 GB, 250 GB   (summed, not maximised)
#   plus 20% headroom, rounded UP -> 8 vCPU, 24 GB, 300 GB
CONSOLIDATED_SHAPE = {"vcpu": 8, "memory_gb": 24, "storage_gb": 300}
# And separated, each at its own shape, for the comparison at the end.
SEPARATED_SHAPES = {(5, 20, 240), (3, 5, 60)}

DRAFT = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "deployment_target": "oci",
    "environment_name": "egate-e2e",
    "environment_tier": "dev",
    "data_classification": "internal",
    "components": COMPONENTS,
    "business_justification": "End-to-end check of the placement chain.",
    "priority": "high",
    "business_criticality": "tier2",
    "required_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
    "application_owner": "app.owner@emaratechg.ae",
    "technical_owner": "tech.owner@emaratechg.ae",
}


def allow_everything(_topology):
    return {"allow": True, "violations": []}


@pytest.fixture()
def db():
    """A CLEAN DATABASE, which the acceptance asks for by name. Seeded with the
    catalogue and nothing else: no requests, no placements, no estimates."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as session:
        seed(session)
        # Blueprints are registered by the catalogue flow, not by the seed. The
        # handoff needs them to say which resource kind builds each host.
        for code in ("compute-vm", "postgres16", "nodejs20"):
            session.add(Blueprint(technology_code=code, deployment_target="oci",
                                  status="certified", resource_kind="oci-instance",
                                  blueprint_ref=f"oci/{code}"))
        session.commit()
        yield session


@pytest.fixture()
def client(db):
    main.app.dependency_overrides[main.get_session] = lambda: db
    main.app.dependency_overrides[main.get_placement_evaluator] = lambda: allow_everything
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def walk_the_chain(client, db, option_key="consolidated") -> dict:
    """Every hop, in order, collecting what each one produced.

    Returns the facts rather than asserting them, so the assertions live in the
    tests that care and the link-cutting tests can reuse the walk.
    """
    facts: dict = {}

    # 1. A request, with three components at the sizes that were asked for.
    reference = client.post("/api/requests/draft", json=DRAFT).json()["reference"]
    facts["reference"] = reference

    # 2. The layouts the platform offers, and the one the requester chooses.
    offered = client.post("/api/placement/options",
                          json={"reference": reference}).json()
    facts["options"] = offered["options"]

    resolved = client.post("/api/placement/resolve",
                           json={"reference": reference,
                                 "option_key": option_key})
    facts["resolve_status"] = resolved.status_code
    facts["placement"] = resolved.json() if resolved.status_code == 200 else None

    # 3. Submitted: the ticket, and the estimate that is stored as approved.
    submitted = client.post(f"/api/requests/{reference}/submit")
    facts["submit_status"] = submitted.status_code
    facts["submitted"] = submitted.json() if submitted.status_code == 200 else None

    req = db.scalar(select(Request).where(Request.reference == reference))
    facts["stored_estimate"] = db.scalar(
        select(Estimate).where(Estimate.request_id == req.id))
    facts["recorded"] = current_placement(db, req.id)

    # 4. The signed handoff the orchestrator receives.
    body, _signature = main._handoff_payload(req)
    facts["payload"] = json.loads(body)

    # 5. What Terraform would be asked to build, from that payload alone.
    facts["units"] = omain._placement_units(facts["payload"])
    facts["specs"] = [omain._placement_spec(facts["payload"], u)
                      for u in (facts["units"] or [])]
    return facts


# --- the chain ----------------------------------------------------------------

def test_three_components_become_one_costed_approved_machine(client, db):
    """P.14. One test, the whole way through."""
    chain = walk_the_chain(client, db)

    # -- the layout was offered, chosen and recorded ------------------------
    assert [o["key"] for o in chain["options"]] == ["managed", "consolidated",
                                                    "separated"]
    assert chain["resolve_status"] == 200
    assert chain["placement"]["option_key"] == "consolidated"
    assert chain["placement"]["version"] == 1
    assert chain["recorded"] is not None, "the decision was not recorded"

    # -- the shape is the SUM of the co-resident requirements, plus headroom --
    sizing = chain["placement"]["sizing"]
    assert sizing["machine_count"] == 1, "consolidating means one machine"
    host = sizing["hosts"][0]
    for field, expected in CONSOLIDATED_SHAPE.items():
        assert host[field] == expected, f"{field}: {host[field]} != {expected}"
    assert host["headroom_percent"] == 20
    assert host["base"] == {"vcpu": 6, "memory_gb": 20, "storage_gb": 250,
                            "iops": 4000}, "the summed requirement before headroom"
    assert set(host["components"]) == {"postgres16", "nodejs20"}

    # -- the cost is derived from that shape, and is the one stored as approved
    monthly = chain["placement"]["estimate"]["totals"]["monthly"]
    assert monthly > 0
    assert float(chain["stored_estimate"].monthly) == pytest.approx(monthly), (
        "the approved figure is not the one the requester chose")

    # -- the approver reads an architecture, at that figure --------------------
    ticket = chain["submitted"]["approval"]["ticket_body"]
    assert "How it will be built:" in ticket
    assert "Layout: Consolidated" in ticket
    assert "8 vCPU, 24 GB RAM, 300 GB disk" in ticket
    assert "postgres16, nodejs20" in ticket
    assert "1 machine is priced below." in ticket
    assert f"monthly {monthly:.2f}" in ticket
    assert "Estimated cost after placement" in ticket

    # -- the layout reaches the orchestrator, inside the signature ------------
    assert "placement" in chain["payload"], "the layout was not signed"
    assert chain["payload"]["placement"]["version"] == 1
    assert chain["payload"]["placement"]["option_key"] == "consolidated"
    assert chain["payload"]["approved_monthly"] == pytest.approx(monthly)

    # -- and Terraform builds exactly that machine ---------------------------
    assert len(chain["units"]) == 1
    assert chain["units"][0]["workspace"] == "oci-instance"
    spec = chain["specs"][0]
    assert spec["ocpus"] == 4, "8 vCPU is 4 OCPUs on x86 flex"
    assert spec["memory_gb"] == 24
    assert spec["boot_volume_gb"] == 300


def test_the_same_components_separated_build_two_machines(client, db):
    """The other half of the phase's claim: the layout, not the component list,
    decides what gets built."""
    chain = walk_the_chain(client, db, option_key="separated")

    assert chain["placement"]["sizing"]["machine_count"] == 2
    shapes = {(h["vcpu"], h["memory_gb"], h["storage_gb"])
              for h in chain["placement"]["sizing"]["hosts"]}
    assert shapes == SEPARATED_SHAPES

    assert len(chain["units"]) == 2
    assert {u["workspace"] for u in chain["units"]} == {"oci-instance__host-1",
                                                        "oci-instance__host-2"}
    assert {s["ocpus"] for s in chain["specs"]} == {3, 2}

    ticket = chain["submitted"]["approval"]["ticket_body"]
    assert "2 machines are priced below." in ticket


def test_consolidating_costs_no_more_than_separating(client, db):
    """The saving the option exists to offer. Asserted as "no more" rather than
    "less" because with headroom switched off the two are identical — the saving
    comes from the headroom and the per-machine setup fee, not from magic."""
    one = walk_the_chain(client, db, "consolidated")
    # A second walk needs its own request; the first one is submitted.
    many = walk_the_chain(client, db, "separated")

    assert one["placement"]["estimate"]["totals"]["monthly"] <= \
        many["placement"]["estimate"]["totals"]["monthly"]


# --- the acceptance clause: it fails if any link breaks ------------------------

# WHICH MODULE A LINK IS CUT IN IS THE WHOLE TRICK, and getting it wrong is how a
# chain test ends up asserting nothing.
#
# `api.main` does `from api.sizing import load_requirements`, so it holds its OWN
# binding. Patching `api.sizing.load_requirements` replaces a name nobody reads:
# the chain runs untouched and the cut is reported as caught when it was never
# made. Both of the numeric links were written that way first and both passed —
# which is exactly the false confidence this file exists to remove, found only
# because the cuts are asserted instead of assumed.
#
# So every link is severed AT ITS POINT OF USE.
LINKS = {
    "sizing sums the requirements": (
        "api.main", "load_requirements", lambda *a, **k: {}),
    "cost is derived from the shape": (
        "api.main", "estimate_placement_cost",
        lambda *a, **k: {"totals": {"one_time": 0, "monthly": 0, "annual": 0},
                         "resolved": False, "currency": "AED",
                         "machine_count": 0, "machines": None, "managed": None,
                         "licences": [], "lines": [], "unpriced": [],
                         "provisional": [], "external_licences": [],
                         "by_category": {}}),
    "the placement is recorded": (
        "api.placement_store", "current_placement", lambda *a, **k: None),
    "the topology reaches the ticket": (
        "api.jira", "build_topology_summary", lambda *a, **k: []),
    "the layout reaches the orchestrator": (
        "api.main", "_placement_handoff", lambda *a, **k: None),
    "the orchestrator reads the layout": (
        "orchestrator.main", "_placement_units", lambda *a, **k: None),
}


@pytest.mark.parametrize("link", sorted(LINKS))
def test_cutting_any_link_breaks_the_chain(client, db, monkeypatch, link):
    """THE ACCEPTANCE CLAUSE, asserted rather than hoped for.

    A chain test nobody has tried to break is a chain test that might be
    asserting nothing. Each link is severed in turn — replaced with something
    that returns a plausible empty answer rather than raising, which is how these
    failures have actually presented — and the chain test must notice.

    A cut that the chain still passes is not a passing test; it is a link this
    file does not really check.
    """
    import importlib

    module_name, attribute, replacement = LINKS[link]
    monkeypatch.setattr(importlib.import_module(module_name), attribute,
                        replacement)

    with pytest.raises((AssertionError, KeyError, TypeError, AttributeError)):
        test_three_components_become_one_costed_approved_machine(client, db)


def test_every_link_is_actually_cut_by_something(client, db):
    """Guards the guard. If a name in LINKS stopped existing — renamed, moved —
    monkeypatch would raise on setattr and every parametrised case would pass for
    the wrong reason, reporting a chain that is checked when it is not."""
    import importlib

    for link, (module_name, attribute, _replacement) in LINKS.items():
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), f"{link}: {module_name}.{attribute} is gone"
