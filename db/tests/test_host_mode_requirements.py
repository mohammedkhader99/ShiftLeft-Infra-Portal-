"""The floor a placed component must clear, per host mode (P.2, F-CAT-18).

Two tables now describe shape, and the interesting failure is between them
rather than inside either. `sizing_anchor` says what "medium" RESOLVES TO;
`host_mode_requirement` says what the workload NEEDS. Nothing forces those to
agree, so the portal can offer "PostgreSQL, small, as a container" where small
resolves below the container floor — a choice a requester can make, a cost the
approver can approve, and a machine that cannot run the thing that was asked
for. The guard below is the reason this file exists.

The numbers themselves are a baseline, not vendor figures, and the tests are
written to reflect that: they assert the SHAPE of the difference — a VM carries
a guest OS and a container does not — rather than any particular value, so
capacity owners can replace the numbers without rewriting the tests.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import sessionmaker

from db import models, seed
from db.models import HostModeRequirement, shape_meets_minimum
from db.seed import seed as run_seed
from db.session import Base

SIZES = ("small", "medium", "large", "xlarge")


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        yield s


def requirements() -> dict[tuple[str, str, str], dict]:
    return {(r["technology_code"], r["host_mode"], r["size"]): r
            for r in seed._host_mode_requirement_rows()}


# --- the acceptance check ----------------------------------------------------

@pytest.mark.parametrize("size", SIZES)
def test_the_same_technology_and_size_needs_more_as_a_vm_than_a_container(size):
    """The one difference that can be defended from first principles: a VM runs
    a guest operating system and a container does not."""
    req = requirements()
    vm = req[("postgres16", "vm", size)]
    container = req[("postgres16", "container", size)]

    assert vm["minimum_vcpu"] > container["minimum_vcpu"]
    assert vm["minimum_memory_gb"] > container["minimum_memory_gb"]
    assert vm["minimum_storage_gb"] > container["minimum_storage_gb"]


@pytest.mark.parametrize("size", SIZES)
def test_the_vm_overhead_is_exactly_the_guest_os(size):
    """Computed in the seed rather than retyped, so the two cannot drift by a
    typo. If someone edits one baseline by hand, this fails."""
    req = requirements()
    vm = req[("nodejs20", "vm", size)]
    container = req[("nodejs20", "container", size)]
    cpu, mem, disk = seed.OS_OVERHEAD

    assert vm["minimum_vcpu"] - container["minimum_vcpu"] == cpu
    assert vm["minimum_memory_gb"] - container["minimum_memory_gb"] == mem
    assert vm["minimum_storage_gb"] - container["minimum_storage_gb"] == disk


def test_iops_does_not_vary_by_host_mode():
    """IOPS is a property of the block volume, not of what runs on top of it.
    A difference here would be invention rather than measurement."""
    req = requirements()
    for size in SIZES:
        assert (req[("postgres16", "vm", size)]["minimum_iops"]
                == req[("postgres16", "container", size)]["minimum_iops"])


# --- the guard that matters --------------------------------------------------

def test_every_offered_size_clears_the_floor_of_every_host_mode_it_is_offered_in(session):
    """The contradiction this file exists to prevent.

    If the anchor for a size resolves below a host mode's minimum, the portal
    offers a combination nothing can build — and it does so silently, because
    each table is internally consistent.
    """
    run_seed(session)
    anchors = {
        (t.code, a.size): a
        for t in session.scalars(select(models.Technology)).all()
        for a in t.sizing_anchors
    }

    impossible = []
    for req in session.scalars(select(HostModeRequirement)).all():
        anchor = anchors.get((req.technology_code, req.size))
        assert anchor, f"no sizing anchor for {req.technology_code}/{req.size}"
        shortfalls = shape_meets_minimum(
            req, anchor.vcpu, anchor.memory_gb, anchor.storage_gb)
        if shortfalls:
            impossible.append((req.technology_code, req.host_mode, req.size,
                               shortfalls))

    assert not impossible, (
        "these size/host-mode combinations are offered but resolve below their "
        f"own minimum: {impossible}")


def test_the_guard_would_notice():
    """A guard that cannot fail is not a guard."""
    req = HostModeRequirement(
        technology_code="postgres16", host_mode="vm", size="small",
        minimum_vcpu=8, minimum_memory_gb=32, minimum_storage_gb=500,
        minimum_iops=1000, recommended_vcpu=8, recommended_memory_gb=32,
        recommended_storage_gb=500, recommended_iops=1000)
    assert len(shape_meets_minimum(req, 2, 4, 50)) == 3


def test_every_shortfall_is_reported_not_only_the_first():
    """Being told the machine is too small in one dimension, fixing it, and
    being told again about the next is what F-UX-10 exists to prevent."""
    req = HostModeRequirement(
        technology_code="nodejs20", host_mode="vm", size="small",
        minimum_vcpu=4, minimum_memory_gb=8, minimum_storage_gb=100,
        minimum_iops=500, recommended_vcpu=4, recommended_memory_gb=8,
        recommended_storage_gb=100, recommended_iops=500)
    shortfalls = shape_meets_minimum(req, 1, 2, 20)

    assert len(shortfalls) == 3
    assert any("vCPU" in s for s in shortfalls)
    assert any("memory" in s for s in shortfalls)
    assert any("storage" in s for s in shortfalls)


# --- what must and must not have rows ----------------------------------------

def test_managed_has_no_requirement_rows():
    """There is no host to size. A row of zeros would sum into a consolidated
    host as though the managed service were running on it."""
    assert not [r for r in seed._host_mode_requirement_rows()
                if r["host_mode"] == "managed"]


def test_every_hostable_mode_has_requirements_for_every_size():
    """Derived from HOST_MODES_SEED, so a host mode cannot exist without a
    floor, nor a floor without a host mode."""
    hostable = {(code, mode)
                for code, _clouds, mode, _note in seed.HOST_MODES_SEED
                if mode != "managed"}
    have = {(r["technology_code"], r["host_mode"])
            for r in seed._host_mode_requirement_rows()}
    assert hostable == have

    counts = {}
    for r in seed._host_mode_requirement_rows():
        counts[(r["technology_code"], r["host_mode"])] = \
            counts.get((r["technology_code"], r["host_mode"]), 0) + 1
    assert set(counts.values()) == {len(SIZES)}


def test_a_recommendation_is_never_below_its_own_floor():
    for r in seed._host_mode_requirement_rows():
        for field in ("vcpu", "memory_gb", "storage_gb", "iops"):
            assert r[f"recommended_{field}"] >= r[f"minimum_{field}"], (r, field)


# --- versioning ---------------------------------------------------------------

def test_a_superseded_requirement_stays_readable(session):
    """Capacity owners will replace this baseline. The row they replaced must
    still be recoverable, so what a request was judged against months ago can
    be reconstructed."""
    run_seed(session)
    original = session.scalar(
        select(HostModeRequirement).where(
            HostModeRequirement.technology_code == "postgres16",
            HostModeRequirement.host_mode == "vm",
            HostModeRequirement.size == "small"))
    assert original and original.version == 1

    session.add(HostModeRequirement(
        technology_code="postgres16", host_mode="vm", size="small",
        minimum_vcpu=4, minimum_memory_gb=8, minimum_storage_gb=80,
        minimum_iops=1000, recommended_vcpu=8, recommended_memory_gb=16,
        recommended_storage_gb=160, recommended_iops=3000,
        effective_from=date(2026, 10, 1), version=2))
    session.flush()

    rows = session.scalars(
        select(HostModeRequirement)
        .where(HostModeRequirement.technology_code == "postgres16",
               HostModeRequirement.host_mode == "vm",
               HostModeRequirement.size == "small")
        .order_by(HostModeRequirement.effective_from)).all()

    assert [r.version for r in rows] == [1, 2]
    assert rows[0].minimum_vcpu == 2, "the superseded floor must not be rewritten"


def test_seeding_twice_does_not_duplicate(session):
    run_seed(session)
    first = len(session.scalars(select(HostModeRequirement)).all())
    run_seed(session)
    assert len(session.scalars(select(HostModeRequirement)).all()) == first


def test_seeded_strings_fit_their_columns():
    """SQLite ignores a VARCHAR limit; Postgres enforces it."""
    cols = HostModeRequirement.__table__.columns
    caps = {n: cols[n].type.length for n in ("technology_code", "host_mode", "size")
            if isinstance(cols[n].type, String)}
    too_long = {(r["technology_code"], c): len(r[c])
                for r in seed._host_mode_requirement_rows()
                for c, cap in caps.items() if len(r[c]) > cap}
    assert not too_long, too_long
