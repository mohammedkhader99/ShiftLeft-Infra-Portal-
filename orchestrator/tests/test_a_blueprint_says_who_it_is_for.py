"""Some blueprints build infrastructure for the PORTAL, not for a requester.

Nearly every blueprint delivers a technology somebody chose from the catalogue,
and `builds` names which. A few do not: K.1's probe asks whether a machine in
the VCN can reach the cluster's API, and K.2's deployer will apply manifests
from inside it. Neither delivers anything a requester asked for.

WHY THE FIELD EXISTS, rather than a convention. `builds` is required and must be
non-empty, so a probe blueprint was nearly given `builds: [oke-probe]` —
inventing a catalogue technology to satisfy a required field. That would have
put a word in front of requesters that means nothing to them, and taught the
next person that `builds` is a formality rather than a statement about what a
blueprint delivers.

THE CONTAINMENT IS THE POINT. A platform blueprint must never be reachable as an
answer to "what builds this technology", because that is the path by which a
probe becomes something somebody can request.
"""

from __future__ import annotations

import pytest

from orchestrator import blueprint_registry as registry

MANIFEST = ("ref: t/{ref}\ntarget: oci\nresource_kind: t-{ref}\n"
            "module: .\n{extra}")


def write(tmp_path, name: str, extra: str, ref: str | None = None):
    (tmp_path / f"{name}.yaml").write_text(
        MANIFEST.format(ref=ref or name, extra=extra), encoding="utf-8")
    return {b["ref"]: b for b in registry.discover(tmp_path)}


# --- a blueprint for the catalogue is unchanged ------------------------------

def test_a_blueprint_that_says_nothing_serves_the_catalogue(tmp_path):
    """Every blueprint written before this field meant the catalogue, and must
    go on meaning it."""
    found = write(tmp_path, "ordinary", "builds: [nginx]\n")

    assert found["t/ordinary"]["serves"] == registry.SERVES_CATALOGUE
    assert not found["t/ordinary"].get("error")


def test_a_catalogue_blueprint_still_has_to_build_something(tmp_path):
    """The original rule. A blueprint delivering nothing to anybody is dead
    weight the registry cannot match to a request."""
    found = write(tmp_path, "empty", "builds: []\n")

    assert "missing one of" in found["t/empty"]["error"]


# --- and one for the portal itself -------------------------------------------

def test_a_platform_blueprint_may_build_nothing(tmp_path):
    found = write(tmp_path, "probe", "serves: platform\nbuilds: []\n")

    assert found["t/probe"]["serves"] == registry.SERVES_PLATFORM
    assert not found["t/probe"].get("error"), found["t/probe"].get("error")


def test_a_platform_blueprint_may_omit_builds_entirely(tmp_path):
    found = write(tmp_path, "probe", "serves: platform\n")

    assert not found["t/probe"].get("error")


def test_it_still_has_to_say_what_it_is_and_where(tmp_path):
    """`serves: platform` excuses `builds` and nothing else. A manifest with no
    resource kind cannot be planned, applied or destroyed."""
    (tmp_path / "x.yaml").write_text(
        "ref: t/x\ntarget: oci\nserves: platform\n", encoding="utf-8")
    found = {b["ref"]: b for b in registry.discover(tmp_path)}

    assert "missing one of" in found["t/x"]["error"]
    assert "builds" not in found["t/x"]["error"], (
        "builds is not required of a platform blueprint and must not be named")


# --- the containment ----------------------------------------------------------

def test_a_platform_blueprint_that_claims_technologies_is_refused(tmp_path):
    """ENFORCED IN BOTH DIRECTIONS. One that also claimed catalogue technologies
    would be reachable by `for_technology` and would quietly become requestable
    — the leak this field exists to prevent, arriving through the field."""
    found = write(tmp_path, "sneaky", "serves: platform\nbuilds: [nginx]\n")

    assert "builds nothing for the catalogue" in found["t/sneaky"]["error"]


def test_the_probe_is_not_an_answer_to_any_technology():
    """The shipped one, through the real registry."""
    probe = registry.for_resource_kind("oci-oke-probe")

    assert probe is not None and probe["serves"] == registry.SERVES_PLATFORM
    assert probe["builds"] == []
    assert registry.for_technology("oke-probe") is None
    assert registry.for_technology("oci-oke-probe") is None


def test_no_shipped_platform_blueprint_can_be_reached_by_technology():
    """The invariant, over whatever ships — not over a list kept here."""
    for bp in registry.discover():
        if bp.get("serves") != registry.SERVES_PLATFORM:
            continue
        assert bp.get("builds") == [], bp["ref"]
        for code in ("nginx", "postgres16", "oci-oke", "kafka", "apache"):
            found = registry.for_technology(code)
            assert found is None or found["ref"] != bp["ref"], (bp["ref"], code)


def test_an_unrecognised_word_falls_back_to_the_catalogue(tmp_path):
    """Not silently treated as `platform`. A typo must not excuse a blueprint
    from declaring what it builds — that is the direction where a mistake lets
    something through rather than stopping it."""
    found = write(tmp_path, "typo", "serves: platfrom\nbuilds: []\n")

    assert found["t/typo"]["serves"] == registry.SERVES_CATALOGUE
    assert "missing one of" in found["t/typo"]["error"]
