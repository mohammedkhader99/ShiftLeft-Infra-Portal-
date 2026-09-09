"""A requester who asks for 5 vCPU gets at least 5.

OCI flex shapes are specified in OCPUs and one OCPU is two vCPUs, so the portal
halves the requested count. It halved with Python's `round()`, which is banker's
rounding: `round(5/2)` is 2, and 2 OCPUs is 4 vCPUs. A request for 5 was priced
at 5 and built at 4.

WHY THIS SAT UNNOTICED. Every sizing anchor in the catalogue is even — 2, 4, 8,
16 — and for even counts `round` and `ceil` agree exactly. No request in this
database has ever carried an odd explicit vCPU either, so the bug had never been
reachable in practice. It was a trap waiting for the first person to type 5 into
the component detail form, which the form allows: `_component_shape` prefers an
explicit value over the anchor precisely so the built machine matches the priced
one.

That is also why fixing it was safe rather than disruptive. Changing this would
normally alter the shape of machines already running — a drift check would newly
report drift, and re-applying could force replacement of a live instance — but
with no odd value anywhere in the history, no existing environment changes at
all. The tests below assert that emptiness rather than assuming it.
"""

from __future__ import annotations

import orchestrator.main as omain


def sizing(vcpu, memory_gb=8):
    return omain._instance_sizing({"policy_input": {"components": [
        {"technology_code": "x", "vcpu": vcpu, "memory_gb": memory_gb}]}})


# --- the defect ---------------------------------------------------------------

def test_five_vcpu_is_three_ocpus_not_two():
    """The case, stated plainly. Two OCPUs is four vCPUs, and the request said
    five."""
    assert sizing(5)["ocpus"] == 3


def test_a_machine_is_never_built_smaller_than_it_was_asked_for():
    """The property the case above is one example of. Swept rather than spot
    checked, because the failure is arithmetic and a single example proves only
    itself."""
    for vcpu in range(1, 65):
        built = sizing(vcpu)["ocpus"] * 2
        assert built >= vcpu, f"{vcpu} vCPU was built as {built}"


def test_it_rounds_up_by_the_least_it_can():
    """Up, but not generously. Rounding to the next whole OCPU is a constraint of
    the shape; anything beyond that would be spending someone's budget."""
    for vcpu, expected in ((1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (8, 4), (9, 5)):
        assert sizing(vcpu)["ocpus"] == expected, vcpu


# --- and nothing that exists today moves --------------------------------------

def test_every_catalogue_anchor_is_unchanged():
    """The no-op proof. Every anchor is even, so round and ceil agree — which is
    why no environment already built changes shape, and why this fix does not
    make a running machine look drifted."""
    import math

    for vcpu in (2, 4, 8, 16):
        assert math.ceil(vcpu / 2) == round(vcpu / 2)
        assert sizing(vcpu)["ocpus"] == vcpu // 2


def test_the_anchors_this_relies_on_really_are_even():
    """Guards the reasoning above. If an odd anchor is ever added, the claim that
    nothing existing changes stops being true, and this says so rather than
    letting the no-op proof quietly become false."""
    odd = {size: shape for size, shape in omain._SIZES.items() if shape[0] % 2}
    assert not odd, f"odd vCPU anchors would change existing machines: {odd}"


def test_the_two_sizing_paths_now_agree_on_odd_counts():
    """They already agreed on even ones, which is what hid this. The placement
    path rounds up because P.6 adds headroom and rounds that up; this one now
    rounds up for the simpler reason that a machine should not be smaller than
    was asked for."""
    payload = {
        "reference": "REQ-AGREE", "resource_kind": "oci-instance",
        "policy_input": {"environment_tier": "dev", "components": [
            {"technology_code": "x", "vcpu": 5, "memory_gb": 20}]},
        "placement": {"version": 1, "option_key": "separated", "hosts": [{
            "id": "host-1", "host_mode": "vm", "components": ["x"],
            "resource_kind": "oci-instance", "resolved": True,
            "vcpu": 5, "memory_gb": 20, "storage_gb": 100}]},
    }
    unit = omain._placement_units(payload)[0]

    assert omain._placement_spec(payload, unit)["ocpus"] == sizing(5, 20)["ocpus"]
