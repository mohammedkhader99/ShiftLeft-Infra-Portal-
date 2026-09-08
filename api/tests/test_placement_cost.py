"""Pricing a topology rather than a shopping list (P.7).

The mistake this guards against does not look like a bug. Pricing per component
after placement exists charges consolidation as though it were separation: three
components on one machine get three machines' compute and three setup fees. The
estimate is still a plausible number, still approvable, and makes the option the
portal recommends look worse the more it actually saves.

So the assertions below are mostly about which unit a charge is bought in:

    compute and storage   once per MACHINE
    setup fee             once per MACHINE
    licence               once per COMPONENT, wherever it runs
    managed service       its own rate, never a machine's
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.pricing import compare_options, estimate_placement_cost
from api.sizing import size_hosts
from db.seed import seed as run_seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        run_seed(s)
        yield s


def req(vcpu, memory, storage, iops=1000):
    return {"recommended_vcpu": vcpu, "recommended_memory_gb": memory,
            "recommended_storage_gb": storage, "recommended_iops": iops}


REQS = {
    ("postgres16", "vm"): req(4, 16, 200),
    ("nodejs20", "vm"): req(2, 4, 50),
    # A component with a real licence fee, so licence behaviour is visible in
    # the numbers. PostgreSQL's licence is 0.00, which would hide it.
    ("mssql", "vm"): req(4, 16, 200),
}

CONSOLIDATED = [{"id": "host-1", "host_mode": "vm",
                 "components": ["postgres16", "nodejs20"]}]
SEPARATED = [
    {"id": "host-1", "host_mode": "vm", "components": ["postgres16"]},
    {"id": "host-2", "host_mode": "vm", "components": ["nodejs20"]},
]
MANAGED = [
    {"id": "managed-postgres16", "host_mode": "managed", "components": ["postgres16"]},
    {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]},
]
SIZES = {"postgres16": "medium", "nodejs20": "small", "mssql": "medium"}


def price(hosts, session, target="oci", percent=20):
    sized = size_hosts(hosts, REQS, percent=percent)
    return estimate_placement_cost(sized, target, session, SIZES)


# --- the unit each charge is bought in ---------------------------------------

def test_compute_is_charged_once_per_machine_not_once_per_component(session):
    """Two components on one host must never cost MORE than the same two apart.

    Note what this does not claim. Cloud compute is priced linearly per vCPU, so
    one 6-vCPU machine costs exactly what two machines totalling 6 vCPU cost:
    with headroom off, consolidating saves nothing at all. The saving is real
    but it comes from headroom being bought per machine and from per-machine
    setup fees — not from the compute rate. Asserting a strict saving here would
    be asserting something the pricing model does not provide.
    """
    together = price(CONSOLIDATED, session)
    apart = price(SEPARATED, session)

    assert together["machine_count"] == 1
    assert apart["machine_count"] == 2
    assert together["totals"]["monthly"] <= apart["totals"]["monthly"], (
        "consolidating cost more than separating; compute is being charged per "
        "component, which prices the recommended option out of contention")


def test_under_linear_pricing_consolidation_saves_only_via_headroom(session):
    """Documents where the saving actually comes from, so nobody later 'fixes'
    the pricing to show a discount that the rate card does not give.

    With headroom off the two options are identical to the fils. Turn headroom
    on and consolidating wins, because the spare capacity is bought once instead
    of twice.
    """
    bare_together = price(CONSOLIDATED, session, percent=0)
    bare_apart = price(SEPARATED, session, percent=0)
    assert bare_together["totals"]["monthly"] == bare_apart["totals"]["monthly"]

    padded_together = price(CONSOLIDATED, session, percent=20)
    padded_apart = price(SEPARATED, session, percent=20)
    assert padded_together["totals"]["monthly"] < padded_apart["totals"]["monthly"]


def test_one_machine_line_per_host(session):
    """The machine estimate is built from one synthetic component per host. If
    it ever grows a line per component, compute is being double charged."""
    together = price(CONSOLIDATED, session)
    assert len(together["machines"]["lines"]) == 1

    apart = price(SEPARATED, session)
    assert len(apart["machines"]["lines"]) == 2


def test_the_setup_fee_is_paid_once_per_machine(session):
    """On-premises charges a one-time setup. Billing it per component would
    make a consolidated request pay three setups for the one machine it builds."""
    together = price(CONSOLIDATED, session, target="onprem")
    apart = price(SEPARATED, session, target="onprem")

    assert together["totals"]["one_time"] > 0, "onprem should charge a setup fee"
    assert together["totals"]["one_time"] < apart["totals"]["one_time"]


def test_a_licence_is_charged_per_component_wherever_it_runs(session):
    """Moving two licensed engines onto one machine does not halve the
    licensing. Compute consolidates; licences do not."""
    two_licensed = [{"id": "host-1", "host_mode": "vm",
                     "components": ["mssql", "nodejs20"]}]
    one_licensed = [{"id": "host-1", "host_mode": "vm",
                     "components": ["nodejs20"]}]

    with_licence = price(two_licensed, session)
    without = price(one_licensed, session)

    assert [line["technology_code"] for line in with_licence["licences"]] == ["mssql"]
    assert not without["licences"]
    assert with_licence["totals"]["monthly"] > without["totals"]["monthly"]


def test_a_managed_service_is_priced_by_its_own_rate_not_as_a_machine(session):
    managed = price(MANAGED, session)

    assert managed["machine_count"] == 1, "only the Node.js host is a machine"
    assert managed["managed"] is not None
    assert len(managed["managed"]["lines"]) == 1
    # Its licence comes through the managed estimate, not added a second time.
    assert not any(l["technology_code"] == "postgres16"
                   for l in managed["licences"])


# --- an estimate must never be confidently wrong -----------------------------

def test_an_unresolved_host_makes_the_whole_estimate_unresolved(session):
    """A number missing a machine is worse than no number, because somebody can
    approve it."""
    unknown = [{"id": "host-1", "host_mode": "vm",
                "components": ["postgres16", "mystery"]}]
    result = price(unknown, session)

    assert result["resolved"] is False


def test_a_resolved_topology_is_marked_resolved(session):
    assert price(CONSOLIDATED, session)["resolved"] is True


# --- the deltas, which are the point -----------------------------------------

def test_the_cheapest_option_is_marked_and_the_others_carry_their_delta(session):
    options = [
        dict(price(MANAGED, session), title="Managed"),
        dict(price(CONSOLIDATED, session), title="Consolidated"),
        dict(price(SEPARATED, session), title="Separated"),
    ]
    compared = compare_options(options)

    cheapest = [c for c in compared if c["cheapest"]]
    assert cheapest, "some option must be the cheapest"
    assert all(c["monthly_delta"] >= 0 for c in compared)
    assert all(c["monthly_delta"] == 0 for c in cheapest)
    # A tie is a real answer, not a bug: two layouts can genuinely cost the
    # same. What must not happen is a dearer option being called cheapest.
    dearest = max(c["totals"]["monthly"] for c in compared)
    if dearest > min(c["totals"]["monthly"] for c in compared):
        assert not any(c["cheapest"] and c["totals"]["monthly"] == dearest
                       for c in compared)


def test_an_unpriceable_option_is_not_treated_as_free(session):
    """A zero delta would make an option nobody can cost look like the bargain
    of the set."""
    unknown = [{"id": "h", "host_mode": "vm", "components": ["mystery"]}]
    compared = compare_options([
        dict(price(CONSOLIDATED, session), title="Consolidated"),
        dict(price(unknown, session), title="Broken"),
    ])

    broken = next(c for c in compared if c["title"] == "Broken")
    assert broken["monthly_delta"] is None
    assert broken["cheapest"] is False


def test_when_nothing_can_be_priced_no_option_is_called_cheapest(session):
    unknown = [{"id": "h", "host_mode": "vm", "components": ["mystery"]}]
    compared = compare_options([dict(price(unknown, session), title="Broken")])

    assert compared[0]["monthly_delta"] is None
    assert not any(c["cheapest"] for c in compared)


def test_headroom_shows_up_in_the_price(session):
    """Headroom buys real capacity, so it costs real money. If it ever stops
    changing the estimate, it has stopped being applied."""
    bare = price(CONSOLIDATED, session, percent=0)
    padded = price(CONSOLIDATED, session, percent=50)

    assert padded["totals"]["monthly"] > bare["totals"]["monthly"]


def test_the_estimate_names_where_its_prices_came_from(session):
    """An approver seeing a figure should be able to learn whether it came from
    a live rate or a cached one."""
    assert price(CONSOLIDATED, session)["pricing_source"]


def test_a_malformed_option_is_refused_rather_than_mis_answered(session):
    """P.9 nested `resolved` and `totals` one level down, and compare_options
    answered "nothing is priceable" — no error, a plausible body, every delta
    None. A wrong shape must fail loudly."""
    with pytest.raises(ValueError, match="must carry `totals`"):
        compare_options([{"resolved": True, "estimate": {"totals": {"monthly": 1}}}])
