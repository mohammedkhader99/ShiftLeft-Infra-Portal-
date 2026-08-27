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

def test_software_that_IS_packaged_never_reaches_for_a_container():
    """THE correction. D1 first inverted the ladder to put containers first, on
    the argument that a container requires no guessing. That was an
    over-correction: a container changes how the service is patched, backed up
    and monitored, and no failure was ever caused by the ORDER — every one was
    caused by GUESSING the package name, which is now looked up.

    So the operational order stands, and a container is not reached for when the
    repositories offer the software."""
    methods = resolve.methods_for("anything", **sources(
        image={"image": "docker.io/library/anything"}, packages=["anything"]))

    assert methods == ["package"], (
        f"{methods} — a container was offered for software that is packaged")


def test_software_that_is_NOT_packaged_reaches_for_a_container_with_no_machine():
    """The whole point of the increment. The container rung has always waited
    for one fact — "the repositories do not carry this" — and has always bought
    it by building a VM and reading its report. The same fact now costs nothing.
    """
    methods = resolve.methods_for("mongodb", **sources(
        image={"image": "docker.io/mongodb/x"}, packages=[]))

    assert methods == ["container"], methods


def test_a_vendor_repository_comes_before_a_container():
    """Both install onto the machine the normal way; a container does not. The
    repository is also the curated path, re-checked by C10 before a machine."""
    methods = resolve.methods_for("mongodb", curated_repo=True, **sources(
        image={"image": "docker.io/mongodb/x"}, packages=[]))

    assert methods == ["repo", "container"], methods


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


# --- which of a vendor's images is THE one ------------------------------------

def test_the_canonical_image_wins_not_whichever_the_search_returned_first():
    """Found before a machine ran it, and it would have been hard to spot after.

    MongoDB publishes several images under its own namespace. Trusting Docker
    Hub's ordering picked `mongodb-atlas-local` — the LOCAL DEVELOPMENT EMULATOR
    — over `mongodb-community-server`, and picked differently on the next call,
    because Hub's relevance score is not stable. A request for a database would
    have been provisioned a development tool, intermittently.
    """
    body = {"results": [
        {"repo_name": "mongodb/mongodb-atlas-local", "star_count": 12},
        {"repo_name": "mongodb/mongodb-community-server", "star_count": 194},
        {"repo_name": "mongodb/mongodb-enterprise-server", "star_count": 40},
    ]}

    assert registry.search("mongodb", fetch=lambda code: body)[0] == (
        "docker.io/mongodb/mongodb-community-server")


def test_the_ranking_does_not_depend_on_the_order_the_registry_replied_in():
    rows = [
        {"repo_name": "mongodb/mongodb-atlas-local", "star_count": 12},
        {"repo_name": "mongodb/mongodb-community-server", "star_count": 194},
    ]
    forward = registry.search("mongodb", fetch=lambda c: {"results": rows})
    backward = registry.search("mongodb", fetch=lambda c: {"results": rows[::-1]})

    assert forward == backward, f"{forward} vs {backward}"


def test_an_official_image_still_beats_a_more_popular_vendor_one():
    """Stars are a tiebreak WITHIN what is already trusted, never a promotion
    into it. An official image is the publisher's own canonical build."""
    body = {"results": [
        {"repo_name": "mongodb/mongodb-community-server", "star_count": 9999},
        {"repo_name": "mongo", "is_official": True, "star_count": 10},
    ]}

    assert registry.search("mongodb", fetch=lambda code: body)[0] == (
        "docker.io/library/mongo")
