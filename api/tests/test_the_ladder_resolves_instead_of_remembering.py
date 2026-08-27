"""D1: what the vendor publishes, ranked by how little we have to guess.

The defect was stated in the code it replaces:

    ORDER IS COST, NOT CONFIDENCE.

and every rung but the first was gated on a hand-written dictionary:

    methods = ["package"]
    if code in VENDOR_REPOS:      methods.append("repo")
    if code in ARCHIVE_KNOWLEDGE: methods.append("archive")

So a technology in neither dictionary had a ONE-RUNG ladder: guess the package
name. RabbitMQ sat uncertified for days behind that sentence, and .NET spent
five machines on it. Meanwhile the container rung — which requires guessing
nothing, because the publisher declares the image, its digest and its ports —
was reachable only AFTER a machine had been spent.

Two properties carry this increment:

  1. ORDER IS CONFIDENCE. A curated recipe, then a container, then a package
     SEARCHED in metadata, then an archive — which fetches and executes a file
     as root and therefore stays last however well curated.
  2. A DICTIONARY IS AN OVERRIDE, NEVER A PREREQUISITE. A technology nobody has
     ever written anything about still gets a real ladder.

Nothing here reaches the network.
"""

from __future__ import annotations

import pytest

from api import registry, resolve


def sources(*, image=None, packages=()):
    """A scripted world: what the registry and the repositories would answer."""
    return {"find_image": lambda code: image,
            "search": lambda code, family: list(packages)}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    # EXPLICIT, because the shipped default is OFF. D1's code is in place and
    # dormant until the escalation tests are rewritten for the new order; these
    # tests describe what happens when it is switched on.
    monkeypatch.setenv("RESOLVE_BY_CONFIDENCE", "true")


# --- order is confidence, not cost --------------------------------------------

def test_a_container_is_preferred_to_guessing_a_package():
    """THE inversion. The publisher declares the image, its digest and its
    ports; a package name is something we work out."""
    methods = resolve.methods_for("anything", **sources(
        image={"image": "docker.io/library/anything"}, packages=["anything"]))

    assert methods.index("container") < methods.index("package")


def test_a_curated_recipe_beats_everything():
    """A person verified it, and C10 re-checks its URL before a machine. That is
    more evidence than any source can offer."""
    methods = resolve.methods_for("mongodb", curated_repo=True, **sources(
        image={"image": "docker.io/mongodb/x"}, packages=["mongodb-org"]))

    assert methods[0] == "repo"


def test_an_archive_stays_last_however_curated():
    """It fetches a file from the internet and runs it as root. Curation does
    not change what it does."""
    methods = resolve.methods_for("thing", curated_archive=True, **sources(
        image={"image": "docker.io/library/thing"}, packages=["thing"]))

    assert methods[-1] == "archive"


# --- a dictionary is an override, never a prerequisite ------------------------

def test_a_technology_nobody_has_written_anything_about_still_has_a_ladder():
    """RabbitMQ in one line. In neither dictionary, so its whole ladder was
    `["package"]` — guess the name — and it has been uncertified for days."""
    methods = resolve.methods_for("neverseen", **sources(
        image={"image": "docker.io/library/neverseen"}))

    assert methods == ["container"], (
        "a technology in no dictionary still has no way in")


def test_a_technology_with_no_artefact_anywhere_gets_an_empty_ladder():
    """An honest nothing. Better than a one-rung ladder that spends a machine
    to discover the same thing."""
    assert resolve.methods_for("imaginary", **sources()) == []


# --- the package rung stops guessing ------------------------------------------

def test_the_package_name_is_searched_not_the_catalogue_code():
    """`dotnet8` is a catalogue label, not a package. So is `redis7`."""
    found = resolve.package_name("dotnet8", search=lambda c, f: ["dotnet-sdk-8.0"])
    assert found == "dotnet-sdk-8.0"


def test_nothing_packaged_is_an_ordinary_answer():
    """MongoDB is not in Oracle's repositories at all, and saying so is what
    lets the container or vendor-repo rung take over."""
    assert resolve.package_name("mongodb", search=lambda c, f: []) == ""


def test_a_source_that_raises_is_not_a_failure():
    """Every source is fail-soft. A registry having a bad afternoon must not
    remove a rung the ladder would otherwise have."""
    def explode(code, family):
        raise RuntimeError("registry down")

    assert resolve.package_name("x", search=explode) == ""
    assert resolve.methods_for("x", find_image=lambda c: 1 / 0,
                               search=explode) == []


# --- the switch is a real revert ----------------------------------------------

def test_switching_it_off_restores_the_old_order_exactly(monkeypatch):
    """Not a different code path pretending to be the old one: cost-first, and
    dictionary-gated, as it was."""
    monkeypatch.setenv("RESOLVE_BY_CONFIDENCE", "false")

    methods = resolve.methods_for("mongodb", curated_repo=True, **sources(
        image={"image": "docker.io/x/y"}, packages=["mongodb-org"]))

    assert methods == ["package", "repo"], methods


# --- the trust rule: what may run as root -------------------------------------

def test_an_unknown_users_image_is_never_a_candidate():
    """THE security test. Searching the registry for "dotnet" returns
    `slacksec/dotnet` — zero stars, an unknown account. Pulling that as root is
    worse than any failed guess."""
    body = {"results": [
        {"repo_name": "slacksec/dotnet", "is_official": False, "star_count": 0},
        {"repo_name": "youngliving/dotnet", "is_official": False, "star_count": 0},
    ]}
    assert registry.search("dotnet", fetch=lambda code: body) == []


def test_an_official_image_is_trusted():
    body = {"results": [{"repo_name": "rabbitmq", "is_official": True}]}
    assert registry.search("rabbitmq", fetch=lambda code: body) == [
        "docker.io/library/rabbitmq"]


def test_a_vendors_own_namespace_is_trusted():
    """MongoDB publishes under `mongodb/`. That is a vendor account, not a
    stranger's."""
    body = {"results": [
        {"repo_name": "mongodb/mongodb-community-server", "is_official": False}]}
    assert registry.search("mongodb", fetch=lambda code: body) == [
        "docker.io/mongodb/mongodb-community-server"]


def test_popularity_is_not_provenance():
    """A wildly popular image from an unrelated account is still a stranger's."""
    body = {"results": [
        {"repo_name": "someone/dotnet", "is_official": False, "star_count": 99999}]}
    assert registry.search("dotnet", fetch=lambda code: body) == []


def test_a_search_that_fails_returns_nothing_rather_than_raising():
    def explode(code):
        raise RuntimeError("hub down")

    assert registry.search("x", fetch=explode) == []
