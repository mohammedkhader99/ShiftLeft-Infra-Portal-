"""A combination a machine DISPROVED must stop being offered.

WHY THIS FILE EXISTS. REQ-2026-0139 and REQ-2026-0140 built one machine each with
five technologies on them, and each OS failed exactly one:

    python312 on Oracle Linux   the recipe installs `python3`, which is 3.9.25
    nodejs20  on Ubuntu 24.04   the recipe installs `nodejs`, which is 18.19.1

Both were sold under catalogue names that name a version — "Python 3.12",
"Node.js 20" — and both are the shape of the bug that shipped Redis 6.2 as Redis
7. Neither was visible until the machine was asked what version it actually had.

"Nobody has tried this" and "we tried it and it delivered the wrong thing" call
for opposite treatment: the first is a gap to fill, the second is a promise the
portal must stop making. So a refuted combination is withdrawn from the form and
refused server-side, with the MEASUREMENT in the message rather than a shrug.
"""

from __future__ import annotations

import pytest

from api import blueprint_capabilities, component_options


def _shipped(refuted):
    return [{"ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
             "builds": ["nginx", "python312", "nodejs20"],
             "os_families": ["rhel", "debian"], "refuted": refuted}]


@pytest.fixture()
def caps(request):
    blueprint_capabilities.reset()
    yield
    blueprint_capabilities.reset()


def test_a_refuted_family_is_withdrawn_from_what_the_technology_supports(caps):
    """THE rule. Oracle Linux images stop being offered for python312."""
    blueprint_capabilities.refresh(lambda: _shipped(
        {"python312": {"rhel": "Oracle Linux's `python3` is Python 3.9, not 3.12"}}))
    families = blueprint_capabilities.families_for("python312", lambda: [])
    assert families == {"debian"}, "the disproven family is still on offer"


def test_the_other_technologies_are_untouched(caps):
    """A refusal that spread would take working combinations down with it."""
    blueprint_capabilities.refresh(lambda: _shipped(
        {"python312": {"rhel": "…"}}))
    assert blueprint_capabilities.families_for("nginx", lambda: []) == {"rhel", "debian"}


def test_the_measurement_survives_to_the_refusal(caps):
    """'not supported' would be true and misleading: the recipe DOES support it,
    it was built, and it delivered the wrong thing. The number is what makes the
    message actionable."""
    blueprint_capabilities.refresh(lambda: _shipped(
        {"nodejs20": {"debian": "Ubuntu 24.04's `nodejs` is Node 18, not Node 20 "
                                "(measured: 18.19.1)"}}))
    why = blueprint_capabilities.refusal("nodejs20", "debian")
    assert "18.19.1" in why


def test_a_combination_with_no_such_evidence_has_no_refusal(caps):
    blueprint_capabilities.refresh(lambda: _shipped({}))
    assert blueprint_capabilities.refusal("nodejs20", "debian") == ""


def test_nothing_is_refuted_by_default(caps):
    """A refusal must come from a machine that was built and measured. Inventing
    one would refuse a combination nobody has any evidence against."""
    blueprint_capabilities.refresh(lambda: _shipped({}))
    assert blueprint_capabilities.families_for("python312", lambda: []) == {"rhel", "debian"}


# --- The real manifests, as shipped -------------------------------------------

def test_the_shipped_blueprints_carry_todays_evidence():
    """Reads what the orchestrator actually publishes, so the two measurements
    from REQ-2026-0139 and REQ-2026-0140 cannot quietly stop being applied."""
    from orchestrator import blueprint_registry
    shipped = blueprint_registry.discover()
    refuted = {}
    for bp in shipped:
        for code, families in (bp.get("refuted") or {}).items():
            for family in families:
                refuted[(code, family)] = families[family]
    assert ("python312", "rhel") in refuted
    assert ("nodejs20", "debian") in refuted
    assert "3.9" in refuted[("python312", "rhel")]
    assert "18" in refuted[("nodejs20", "debian")]


def test_a_refuted_pair_is_never_also_claimed_as_verified():
    """The two records would then disagree about the same machine."""
    from orchestrator import configure
    assert not (set(configure.REFUTED) & configure.VERIFIED)
