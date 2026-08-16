"""The estate's environment tiers, and why they are a constrained vocabulary.

environment_class held only `prod` and `non-prod`. That could not express this
estate: development, QMG, pre-test, test and UAT were one value between them.

It became load-bearing on 16 Aug 2026, when the plan for OKE settled on ONE VCN
PER TIER. A network map keyed on the old vocabulary would have put UAT and
development in the same VCN while appearing to honour the requirement exactly —
the kind of convincing wrongness this codebase has spent a week removing.

DR was proposed and deliberately deferred. Adding it is not a one-line change:
disaster recovery generally implies a different REGION, and the map would need a
region alongside its subnets. All six tiers below are in me-dubai-1.
"""

from __future__ import annotations

import pytest

from db.models import ENVIRONMENT_TIERS, LEGACY_ENVIRONMENT_CLASSES, Environment


def test_the_six_tiers_are_exactly_what_was_agreed():
    """A tripwire, not a restatement. Adding a tier means adding a VCN mapping
    for it, so a tier appearing here without one is a machine with nowhere to go.
    """
    assert ENVIRONMENT_TIERS == (
        "Development", "QMG", "Pre-Test", "Test", "UAT", "Production")


def test_dr_was_deferred_deliberately():
    """Deferred on 16 Aug 2026, and it needs a region dimension the map does not
    have. Re-adding it must be a decision, not a tidy-up."""
    assert "DR" not in ENVIRONMENT_TIERS


def test_every_tier_fits_the_column():
    """String(16), and Postgres enforces it where SQLite does not — the exact
    trap that hid a 19-character status until it was hunted down."""
    limit = Environment.__table__.columns["environment_class"].type.length
    too_long = [t for t in ENVIRONMENT_TIERS if len(t) > limit]
    assert not too_long, f"these do not fit varchar({limit}): {too_long}"


def test_the_old_vocabulary_maps_onto_the_new_one():
    """A database seeded before this change holds prod / non-prod. Leaving those
    in place would give rows a tier nothing recognises — and with a per-tier
    network map, a tier nothing recognises is a request that cannot be placed."""
    assert set(LEGACY_ENVIRONMENT_CLASSES) == {"prod", "non-prod"}
    for old, new in LEGACY_ENVIRONMENT_CLASSES.items():
        assert new in ENVIRONMENT_TIERS, f"{old} maps to {new}, which is not a tier"


def test_non_prod_maps_to_uat_not_development():
    """visa-uat is the row this moves, and its name says which tier it is.
    Defaulting every non-prod row to Development would put a UAT environment in
    the development VCN the moment the network map exists."""
    assert LEGACY_ENVIRONMENT_CLASSES["non-prod"] == "UAT"


def test_the_default_is_the_least_privileged_tier():
    """A row created without a tier must not silently become Production."""
    default = Environment.__table__.columns["environment_class"].default.arg
    assert default == "Development"


@pytest.mark.parametrize("tier", ENVIRONMENT_TIERS)
def test_no_tier_is_blank_or_padded(tier):
    """These become keys in a network map and, later, path-safe identifiers."""
    assert tier and tier == tier.strip()
