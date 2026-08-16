"""A tier with no network is refused at the FORM, not after approval.

Found by the platform owner selecting Test on the request form and asking which
VCN the cluster would land in. The answer was "none — it is refused", and the
guard was working: a Test cluster must not be built inside the Development VCN.

But it refused at PLAN time, after the request had been validated, priced, sent
to Jira and approved. The requester saw a retry loop with the reason buried in a
status detail.

The same expensive shape as the resource name that overran its limit and killed
REQ-2026-0128 after approval: the portal knew the answer while the form was still
open and said nothing until it cost something.
"""

from __future__ import annotations

import pytest

from api import blueprint_capabilities as caps
from api import validation


def _shipped(tiers):
    return [{"ref": "oci/oke", "target": "oci", "resource_kind": "oci-oke",
             "builds": ["oci-oke"], "os_families": [], "network_tiers": tiers},
            {"ref": "oci/service-vm", "target": "oci",
             "resource_kind": "oci-service-vm", "builds": ["nginx"],
             "os_families": ["rhel"], "network_tiers": None}]


@pytest.fixture(autouse=True)
def _caps():
    caps.reset()
    yield
    caps.reset()


def _errors(tier, code="oci-oke", tiers=("Development",)):
    caps.refresh(lambda: _shipped(list(tiers)))
    errors: dict[str, str] = {}
    validation._validate_tier_has_a_network(
        {"environment_tier": tier, "components": [{"technology_code": code}]}, errors)
    return errors


def test_a_tier_with_no_network_is_refused():
    """THE case. Test is not mapped; Development is."""
    errors = _errors("Test")
    assert "environment_tier" in errors
    assert "Test" in errors["environment_tier"]


def test_the_refusal_names_what_IS_available():
    """'Not available' leaves a requester stuck. The alternative is what makes it
    an action rather than a wall."""
    assert "Development" in _errors("Test")["environment_tier"]


def test_the_refusal_offers_the_other_way_out():
    """Choosing another tier is not always right — sometimes the network is what
    should change, and the requester is the one who knows."""
    assert "network team" in _errors("Test")["environment_tier"]


def test_a_mapped_tier_passes():
    assert _errors("Development") == {}


def test_a_technology_needing_no_network_map_is_unrestricted():
    """nginx takes the shared compute subnet. Restricting it by tier would refuse
    most of the catalogue for a rule that does not apply to it."""
    assert _errors("Test", code="nginx") == {}


def test_nothing_mapped_at_all_is_refused_differently():
    """No tier can satisfy it, so pointing at the tier field would be misleading —
    the fault is that no network exists, not that this tier is the wrong one."""
    errors = _errors("Development", tiers=())
    assert "components" in errors
    assert "no tier has one yet" in errors["components"]


def test_the_old_lowercase_tier_is_understood():
    """Saved drafts carry them, and refusing one for a rename would be a second
    wrong answer."""
    assert _errors("development") == {}


def test_a_request_with_no_tier_yet_is_not_refused_here():
    """A half-filled draft must not be shouted at about a network before the
    requester has chosen a tier — the missing tier is reported separately."""
    assert _errors("") == {}
