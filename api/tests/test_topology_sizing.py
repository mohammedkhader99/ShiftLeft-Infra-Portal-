"""Sizing a machine from what is actually placed on it (P.6, F-CAT-18).

One arithmetic mistake is worth more attention than the rest: sizing a shared
host to the LARGEST of its components rather than their SUM. It looks reasonable,
it produces a plausible number, every test that only checks "a shape came back"
passes — and the machine runs whichever component is bigger while starving the
other. Several tests below exist solely to make that mistake impossible to make
quietly.

The second is subtler: a managed service adds nothing to any host. The cloud
runs it on hardware nobody here provisions, so counting its shape would size and
bill for a machine that does not exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.sizing import (
    DEFAULT_HEADROOM_PERCENT,
    headroom_percent,
    load_requirements,
    size_hosts,
)
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


def requirement(vcpu, memory, storage, iops):
    return {"recommended_vcpu": vcpu, "recommended_memory_gb": memory,
            "recommended_storage_gb": storage, "recommended_iops": iops}


# Deliberately different sizes so a sum and a maximum cannot coincide.
REQS = {
    ("postgres16", "vm"): requirement(4, 16, 200, 3000),
    ("nodejs20", "vm"): requirement(2, 4, 50, 1000),
    ("nginx", "vm"): requirement(1, 2, 20, 500),
    ("postgres16", "container"): requirement(4, 16, 200, 3000),
}

CONSOLIDATED = [{"id": "host-1", "host_mode": "vm",
                 "components": ["postgres16", "nodejs20", "nginx"]}]
SEPARATED = [
    {"id": "host-1", "host_mode": "vm", "components": ["postgres16"]},
    {"id": "host-2", "host_mode": "vm", "components": ["nodejs20"]},
]
MANAGED = [
    {"id": "managed-postgres16", "host_mode": "managed", "components": ["postgres16"]},
    {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]},
]


# --- the acceptance check ----------------------------------------------------

def test_three_components_on_one_host_are_summed_not_maximised():
    """4+2+1 vCPU is 7, not 4. The whole increment in one assertion."""
    result = size_hosts(CONSOLIDATED, REQS, percent=0)
    host = result["hosts"][0]

    assert host["base"] == {"vcpu": 7, "memory_gb": 22,
                            "storage_gb": 270, "iops": 4500}
    assert host["base"]["vcpu"] != 4, "sized to the largest component, not the sum"


def test_the_headroom_is_visible_in_the_breakdown():
    """A requester asking 'why is my machine bigger than what I asked for?'
    must be able to see the answer rather than be told it."""
    result = size_hosts(CONSOLIDATED, REQS, percent=20)
    host = result["hosts"][0]

    assert host["base"]["vcpu"] == 7          # what the components asked for
    assert host["headroom_percent"] == 20     # what was added, and why it differs
    assert host["vcpu"] == 9                  # ceil(7 * 1.2)


def test_headroom_always_rounds_up():
    """ceil(7 * 1.2) is 8.4 -> 9. Rounding a requirement down turns headroom
    into a shortfall."""
    assert size_hosts(CONSOLIDATED, REQS, percent=20)["hosts"][0]["vcpu"] == 9
    assert size_hosts(CONSOLIDATED, REQS, percent=1)["hosts"][0]["vcpu"] == 8


def test_zero_headroom_sizes_to_the_bare_sum():
    result = size_hosts(CONSOLIDATED, REQS, percent=0)
    host = result["hosts"][0]
    assert host["vcpu"] == host["base"]["vcpu"]


# --- managed services contribute nothing -------------------------------------

def test_a_managed_service_adds_nothing_to_the_totals():
    """Counting the cloud's database would size and bill a machine that does
    not exist."""
    result = size_hosts(MANAGED, REQS, percent=0)

    assert result["machine_count"] == 1
    assert result["totals"]["vcpu"] == 2  # the Node.js host alone
    managed_host = next(h for h in result["hosts"] if h["host_mode"] == "managed")
    assert managed_host["vcpu"] is None
    assert managed_host["resolved"] is True
    assert "No machine is provisioned" in managed_host["note"]


def test_a_managed_host_still_appears_in_the_topology():
    """It contributes nothing, but omitting it would lose the fact that the
    database is running at all."""
    result = size_hosts(MANAGED, REQS, percent=0)
    placed = {c for h in result["hosts"] for c in h["components"]}
    assert placed == {"postgres16", "nodejs20"}


# --- separated -----------------------------------------------------------------

def test_separated_hosts_are_sized_independently():
    result = size_hosts(SEPARATED, REQS, percent=0)

    shapes = {h["host_id"]: h["vcpu"] for h in result["hosts"]}
    assert shapes == {"host-1": 4, "host-2": 2}
    assert result["machine_count"] == 2
    assert result["totals"]["vcpu"] == 6


def test_consolidating_costs_less_capacity_than_separating():
    """Not a law of nature — it holds because headroom is charged per machine.
    If this ever flips, the placement options are being priced misleadingly."""
    together = size_hosts(
        [{"id": "h", "host_mode": "vm", "components": ["postgres16", "nodejs20"]}],
        REQS, percent=20)
    apart = size_hosts(SEPARATED, REQS, percent=20)

    assert together["totals"]["vcpu"] <= apart["totals"]["vcpu"]


# --- a missing requirement must never be treated as zero ---------------------

def test_an_unknown_component_leaves_the_host_unresolved():
    """Summing a missing requirement as zero under-sizes the machine silently,
    which is the failure this whole phase exists to end."""
    result = size_hosts(
        [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mystery"]}],
        REQS, percent=0)
    host = result["hosts"][0]

    assert host["resolved"] is False
    assert host["missing"] == ["mystery"]
    assert host["vcpu"] is None, "no shape at all, rather than a wrong one"
    assert result["resolved"] is False


def test_the_unresolved_note_names_the_component_and_the_host_mode():
    result = size_hosts(
        [{"id": "h", "host_mode": "container", "components": ["nodejs20"]}],
        REQS, percent=0)
    note = result["hosts"][0]["note"]

    assert "nodejs20" in note and "container" in note


def test_an_unresolved_host_is_not_counted_as_a_machine():
    result = size_hosts(
        [{"id": "h", "host_mode": "vm", "components": ["mystery"]}], REQS)
    assert result["machine_count"] == 0
    assert result["totals"]["vcpu"] == 0


# --- the configurable factor --------------------------------------------------

def test_headroom_is_clamped_to_something_sane(monkeypatch):
    """A negative headroom would size a machine BELOW what its own components
    asked for — worse than having no headroom at all."""
    monkeypatch.setenv("PLACEMENT_HEADROOM_PERCENT", "-50")
    assert headroom_percent() == 0
    monkeypatch.setenv("PLACEMENT_HEADROOM_PERCENT", "9999")
    assert headroom_percent() == 100


def test_nonsense_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PLACEMENT_HEADROOM_PERCENT", "twenty")
    assert headroom_percent() == DEFAULT_HEADROOM_PERCENT


def test_the_setting_is_admin_configurable():
    """It is shown to and set by a person, so it belongs in the console rather
    than only in an env file."""
    from api.settings import ALLOWLIST
    meta = ALLOWLIST["PLACEMENT_HEADROOM_PERCENT"]
    assert meta["type"] == "int" and meta["min"] == 0 and meta["max"] == 100


# --- against the real seeded catalogue ---------------------------------------

def test_requirements_load_at_the_size_each_component_was_asked_for(session):
    """The bug this guards: keying on (technology, host mode) without pinning
    the size lets one size overwrite another, and the machine gets built to
    whichever row was read last."""
    reqs = load_requirements(session, {"postgres16": "medium", "nodejs20": "small"})

    assert reqs[("postgres16", "vm")]["size"] == "medium"
    assert reqs[("nodejs20", "vm")]["size"] == "small"
    assert (reqs[("postgres16", "vm")]["recommended_vcpu"]
            > reqs[("nodejs20", "vm")]["recommended_vcpu"])


def test_a_real_consolidated_host_sizes_from_the_seeded_catalogue(session):
    reqs = load_requirements(session, {"postgres16": "small", "nodejs20": "small"})
    result = size_hosts(
        [{"id": "host-1", "host_mode": "vm",
          "components": ["postgres16", "nodejs20"]}],
        reqs, percent=0)
    host = result["hosts"][0]

    assert host["resolved"] is True
    # Both are 'small', recommended 2/4/50 each, so the host carries both.
    assert host["base"]["vcpu"] == 4
    assert host["base"]["memory_gb"] == 8
