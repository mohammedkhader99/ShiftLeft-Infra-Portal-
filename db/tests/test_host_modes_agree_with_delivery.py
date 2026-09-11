"""Two tables describe the same catalogue entry, so they must not drift apart.

`technology_delivery.delivery_model` says what a thing IS. `technology_host_mode`
says where an instance of it may RUN. They overlap without being the same, which
is precisely the shape of fact that goes stale quietly: someone adds a `managed`
host mode for a technology whose delivery model still says `software`, and the
portal offers a cloud service that the rest of the system believes is something
you install on a machine you own.

`TechnologyDelivery` exists because the agent used to infer delivery from the
technology CODE — a naming convention doing a domain model's job, wrong in both
directions, and REQ-2026-0183 spent a real machine discovering that
`dnf install backup` finds nothing. Adding a second overlapping field without a
guard between them would be that mistake again with more tables.

So this asserts the two agree, on the seed data itself rather than on a list of
examples: a technology added tomorrow is covered the day it is written.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from db import models, seed
from db.models import (
    HOST_CONTAINER,
    HOST_MANAGED,
    HOST_MODES,
    HOST_VM,
    host_modes_disagree_with_delivery,
    requires_host,
)

DEFAULT_TARGETS = models.Technology.__table__.columns["targets"].default.arg


def seeded_host_modes() -> dict[str, set[str]]:
    """Host modes per technology, unioned across every cloud."""
    modes: dict[str, set[str]] = {}
    for row in seed._host_mode_rows():
        modes.setdefault(row["technology_code"], set()).add(row["host_mode"])
    return modes


def targets_for(code: str) -> set[str]:
    """The clouds a technology is offered on, defaulting as the column does."""
    for entry in seed.TECHNOLOGIES:
        if entry["code"] == code:
            return set(entry.get("targets", DEFAULT_TARGETS).split(","))
    return set()


# --- the guard itself --------------------------------------------------------

class TestTheInvariant:
    """Each way the two tables can contradict each other, and the one shape
    that looks like a contradiction and is not."""

    def test_capability_may_have_no_host(self):
        assert host_modes_disagree_with_delivery("capability", set()) is None

    def test_capability_with_a_host_is_a_contradiction(self):
        assert "outcome nobody installs" in host_modes_disagree_with_delivery(
            "capability", {HOST_VM})

    def test_a_machine_is_the_host_so_it_can_only_be_a_vm(self):
        assert host_modes_disagree_with_delivery("machine", {HOST_VM}) is None
        assert "IS the host" in host_modes_disagree_with_delivery(
            "machine", {HOST_VM, HOST_CONTAINER})

    def test_managed_must_be_managed_somewhere(self):
        assert "must be among its host modes" in host_modes_disagree_with_delivery(
            "managed", {HOST_VM, HOST_CONTAINER})

    def test_software_may_not_claim_to_be_a_cloud_service(self):
        assert "one of the two is wrong" in host_modes_disagree_with_delivery(
            "software", {HOST_VM, HOST_MANAGED})

    def test_managed_on_one_cloud_and_not_another_is_not_a_contradiction(self):
        """Managed PostgreSQL exists on OCI and not on-premises. Judging per
        cloud rather than on the union would call that correct data a defect."""
        assert host_modes_disagree_with_delivery(
            "managed", {HOST_MANAGED, HOST_VM, HOST_CONTAINER}) is None

    def test_a_technology_must_run_somewhere(self):
        assert "at least one host mode" in host_modes_disagree_with_delivery(
            "software", set())

    def test_an_unknown_host_mode_is_caught(self):
        assert "unknown host mode" in host_modes_disagree_with_delivery(
            "software", {"serverless"})


# --- the seed data ------------------------------------------------------------

def test_every_seeded_technology_agrees_with_its_delivery_model():
    """The headline check: seed data, not examples."""
    disagreements = {}
    for code, modes in seeded_host_modes().items():
        delivery = seed.DELIVERY.get(code)
        assert delivery, f"{code} has host modes but no delivery model"
        complaint = host_modes_disagree_with_delivery(delivery[0], modes)
        if complaint:
            disagreements[code] = complaint
    assert not disagreements, disagreements


def test_host_modes_are_only_offered_on_clouds_the_technology_supports():
    """Offering managed PostgreSQL on a cloud the catalogue does not list it for
    produces an option the requester can pick and nothing can build."""
    wrong = [
        (row["technology_code"], row["cloud"])
        for row in seed._host_mode_rows()
        if row["cloud"] not in targets_for(row["technology_code"])
    ]
    assert not wrong, f"host modes on clouds the technology is not offered on: {wrong}"


def test_every_seeded_host_mode_is_a_known_one():
    unknown = {r["host_mode"] for r in seed._host_mode_rows()} - set(HOST_MODES)
    assert not unknown, unknown


def test_seeded_strings_fit_their_columns():
    """SQLite ignores a VARCHAR limit and Postgres enforces it, so a long note
    would pass every test here and fail only in the container."""
    table = models.TechnologyHostMode.__table__.columns
    caps = {name: table[name].type.length
            for name in ("technology_code", "cloud", "host_mode", "note")
            if isinstance(table[name].type, String)}
    too_long = {
        (row["technology_code"], column): len(row[column])
        for row in seed._host_mode_rows()
        for column, cap in caps.items()
        if len(row[column]) > cap
    }
    assert not too_long, f"values exceeding their column: {too_long} (caps: {caps})"


# --- what the placement task actually needs ----------------------------------

@pytest.mark.parametrize(
    "code, expected",
    [
        ("compute-vm", {HOST_VM}),
        ("nodejs20", {HOST_VM, HOST_CONTAINER}),
        ("postgres16", {HOST_VM, HOST_CONTAINER, HOST_MANAGED}),
        ("oci-oke", {HOST_MANAGED}),
        ("azure-aks", {HOST_MANAGED}),
    ],
)
def test_the_five_technologies_the_task_names(code, expected):
    """PostgreSQL supports all three; Node.js the first two. From the task."""
    assert seeded_host_modes().get(code) == expected


def test_managed_postgres_is_offered_on_oci_only():
    """Not because other clouds lack the service, but because OCI is the one the
    portal has a Terraform module and a proof build for. Advertising the rest
    would claim a capability nothing has demonstrated (ARCHITECTURE.md P8)."""
    clouds = {r["cloud"] for r in seed._host_mode_rows()
              if r["technology_code"] == "postgres16"
              and r["host_mode"] == HOST_MANAGED}
    assert clouds == {"oci"}


def test_requires_host_is_derived_not_stored():
    assert requires_host(HOST_VM) is True
    assert requires_host(HOST_CONTAINER) is True
    assert requires_host(HOST_MANAGED) is False


def test_the_guard_would_notice():
    """A guard that cannot fail is not a guard."""
    assert host_modes_disagree_with_delivery("software", {HOST_MANAGED}) is not None


# --- the gap the checks above could not see ----------------------------------
#
# `test_every_seeded_technology_agrees_with_its_delivery_model` walks the
# technologies that HAVE host modes. That is the right check and it passed
# throughout, while 48 of the catalogue's 56 entries had none at all — so P.2's
# requirement rows covered three technologies and the placement step told anyone
# choosing OpenSearch, Kafka or MySQL that it "cannot be sized".
#
# A check that validates what is present cannot notice what is absent. These
# assert the coverage itself.

def test_every_technology_that_needs_a_host_mode_has_one():
    """`host_modes_disagree_with_delivery` says a non-capability delivery model
    "needs at least one host mode". Nothing was asserting that over the whole
    catalogue, so the sentence was true of the rows that existed and silent about
    the ones that did not."""
    seeded = seeded_host_modes()
    missing = sorted(
        code for code, (delivery, _note) in seed.DELIVERY.items()
        if delivery != models.DELIVERY_CAPABILITY
        and any(entry["code"] == code for entry in seed.TECHNOLOGIES)
        and not seeded.get(code)
    )
    assert not missing, f"no host mode, so nothing can size them: {missing}"


def test_a_capability_still_has_none():
    """The other direction, and the reason the rule cannot simply give everything
    a host mode: an outcome nobody installs has nothing to place, and a row of
    zeros would be a worse answer than an absence."""
    seeded = seeded_host_modes()
    wrong = sorted(code for code, (delivery, _note) in seed.DELIVERY.items()
                   if delivery == models.DELIVERY_CAPABILITY and seeded.get(code))
    assert not wrong, f"capabilities cannot have host modes: {wrong}"


def test_every_host_mode_can_be_sized():
    """A host mode with no requirement row is an option the requester can pick
    and the portal then cannot size — which is what the placement screen has been
    reporting honestly and uselessly for most of the catalogue."""
    sizeable = {(row["technology_code"], row["host_mode"])
                for row in seed._host_mode_requirement_rows()}
    unsizeable = sorted(
        (code, mode) for code, _clouds, mode, _note in seed.HOST_MODES_SEED
        if mode != models.HOST_MANAGED and (code, mode) not in sizeable)
    assert not unsizeable, f"host modes with no requirement row: {unsizeable}"


def test_a_managed_host_mode_has_no_requirement_row():
    """Deliberately absent rather than zero. There is no machine to size, and a
    row of zeros would flow into the sizing sum as though there were."""
    rows = {(r["technology_code"], r["host_mode"])
            for r in seed._host_mode_requirement_rows()}
    assert not [pair for pair in rows if pair[1] == models.HOST_MANAGED]


def test_a_container_is_offered_for_software_and_for_nothing_else():
    """REVISITED 2026-09-11, WHICH THE OLD TEST ASKED FOR.

    It read "no technology is offered as a container yet" and named the two that
    predated it, so that a third would be a deliberate act. This is that
    deliberate act: asked whether to offer the Kubernetes route, grey it out, or
    build the portal as though the network route existed, the platform owner
    chose the third.

    WHAT IS STILL TRUE, AND THE OLD TEST SAID IT PLAINLY. Every certified
    blueprint in this system builds a machine, and none deploys a workload into
    a cluster. A container layout resolves, prices, reaches an approver and then
    fails at provisioning. That is the known cost of the decision, not a
    surprise, and `api.placement.cluster_deployment_offered` is the switch back.

    WHAT IS ASSERTED NOW IS THE RULE, NOT A LIST. Software runs on a machine or
    as an image, so it has both. A machine IS the host; a managed service is run
    by the cloud; a capability is an outcome nobody installs. None of those three
    gains a container form, and a list of names would have to be edited every
    time the catalogue grew — which is how the host modes came to cover three
    technologies out of fifty-one in the first place.
    """
    container = {code for code, _clouds, mode, _note in seed.HOST_MODES_SEED
                 if mode == models.HOST_CONTAINER}
    software = {code for code, (delivery, _note) in seed.DELIVERY.items()
                if delivery == models.DELIVERY_SOFTWARE}
    placed = {code for code, _clouds, _mode, _note in seed.HOST_MODES_SEED}
    # The explicit list is allowed to know more than the rule — that is what it
    # is for. PostgreSQL is delivered `managed` and genuinely runs all three
    # ways, which no rule over delivery models could work out.
    named = {code for code, _clouds, mode, _note in seed.HOST_MODES_EXPLICIT
             if mode == models.HOST_CONTAINER}

    assert container <= software | named, (
        "something that is neither installable software nor explicitly declared "
        f"is offered as a container: {sorted(container - software - named)}")
    assert (software & placed) <= container, (
        "software that can be placed at all should offer both the machine and "
        f"the image form; missing: {sorted((software & placed) - container)}")
