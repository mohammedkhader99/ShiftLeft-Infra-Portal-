"""D2 step 1: we allow twenty registries and only ever searched one.

REQ-2026-0217, 0218, 0222, 0223 and 0224 all asked for SQL Server, and all five
ended in manual fulfilment after a real machine reported `mssql NOT INSTALLED`.
The reason was not the cost cap, the burned attempts or the missing certify call
that each of those requests also uncovered. It was simpler and older:

    registries we ALLOW : 20      mcr.microsoft.com, registry.redhat.io, ...
    registries we SEARCH:  2      docker.io, quay.io

Microsoft publishes SQL Server at `mcr.microsoft.com/mssql/server` — 274 tags,
port 1433 declared, anonymous pull — and nothing in this system could see it.
`candidates` builds `docker.io/library/mssql`, which does not exist. `search`
asks Hub, which does not carry it. So `install_methods` returned an EMPTY
ladder for mssql: no package, no repo, no archive, no container. Nothing to try
at all, and a request that went to a person every time.

The fix is not a row naming Microsoft. It is that a first-party registry is ONE
PUBLISHER BY CONSTRUCTION — which is why it was put on the allow-list — so its
own catalogue can be asked directly, with no trust rule needed beyond the one
that put it on the list.

Nothing here reaches the network.
"""

from __future__ import annotations

import pytest

from api import registry


@pytest.fixture(autouse=True)
def _no_cached_catalogues():
    """The catalogue cache is process-wide, so a test that left one behind would
    decide the next test's answer."""
    registry._catalogues.clear()
    yield
    registry._catalogues.clear()


def mcr(*paths):
    """A scripted registry: only mcr.microsoft.com answers, with these repos."""
    def fetch(reg):
        if reg != "mcr.microsoft.com":
            raise RuntimeError("401 Unauthorized")
        return {"repositories": list(paths)}
    return fetch


# --- the registries we ask -----------------------------------------------------

def test_a_shared_registry_is_never_asked_for_its_catalogue():
    """Docker Hub is where a stranger can publish, so its namespaces mean
    nothing and `_trusted` has to judge each result. Listing it wholesale would
    hand every stranger's image to the ladder."""
    first_party = registry.first_party_registries()

    for shared in ("docker.io", "quay.io", "ghcr.io", "registry.gitlab.com"):
        assert shared not in first_party, (
            f"{shared} hosts anyone's images and is being catalogue-searched, "
            "so the trust rule that guards it has been bypassed")


def test_the_vendors_we_allow_are_the_vendors_we_ask():
    """The whole defect in one assertion: the allow-list and the search list
    were different lists, and nobody noticed for as long as the only technology
    that needed the difference was one nobody had asked for yet."""
    allowed = {r for r in registry.profile_rules.REGISTRIES}
    asked = set(registry.first_party_registries()) | registry._SHARED_REGISTRIES

    assert allowed <= asked, (
        f"allowed but never asked: {sorted(allowed - asked)}")
    assert "mcr.microsoft.com" in registry.first_party_registries()


# --- finding the software ------------------------------------------------------

def test_sql_server_is_found_where_microsoft_actually_publishes_it():
    """THE test. Five requests and five machines to learn what one catalogue
    request answers."""
    found = registry.catalogue_search(
        "mssql", fetch_catalogue=mcr("mssql/server", "dotnet/core/sdk"))

    assert "mcr.microsoft.com/mssql/server" in found, found


def test_another_vendors_product_is_never_offered():
    found = registry.catalogue_search(
        "mssql", fetch_catalogue=mcr("dotnet/core/sdk", "powershell/test-deps"))

    assert found == [], found


def test_a_trailing_version_label_still_matches_the_vendors_namespace():
    """`dotnet8` is a catalogue label; Microsoft's namespace is `dotnet`. The
    same stem rule `candidates` and `_trusted` already use."""
    found = registry.catalogue_search(
        "dotnet8", fetch_catalogue=mcr("dotnet/core/sdk"))

    assert found == ["mcr.microsoft.com/dotnet/core/sdk"]


def test_the_product_outranks_the_accessories_grouped_beneath_it():
    """A vendor groups its accessories BELOW its product. `mssql/bdc/*` is the
    big-data-cluster tooling, not SQL Server."""
    found = registry.catalogue_search("mssql", fetch_catalogue=mcr(
        "mssql/bdc/mssql-controller", "mssql/bdc/mssql-dns", "mssql/server"))

    assert found[0] == "mcr.microsoft.com/mssql/server", found


def test_a_registry_that_will_not_list_contributes_nothing():
    """Most require a token even to list. That is an ordinary outcome — the
    ladder learns nothing from them and carries on."""
    def refuses(reg):
        raise RuntimeError("401 Unauthorized")

    assert registry.catalogue("registry.redhat.io", fetch=refuses) == ()
    assert registry.catalogue_search("mssql", fetch_catalogue=refuses) == []


def test_a_repository_path_that_is_not_a_valid_image_path_is_discarded():
    """The value ends up in a `podman pull` run by root, so it is validated the
    same way every other image reference in this module is."""
    found = registry.catalogue_search(
        "mssql", fetch_catalogue=mcr("mssql/../../etc/passwd", "mssql/server"))

    assert found == ["mcr.microsoft.com/mssql/server"], found


def test_the_catalogue_is_asked_once_however_many_technologies_ask():
    """One request, cached. A registry re-listed per candidate name would make
    the ladder's cost depend on how many names we guessed."""
    calls = []

    def counted(reg):
        calls.append(reg)
        return {"repositories": ["mssql/server", "dotnet/core/sdk"]}

    registry.catalogue_search("mssql", fetch_catalogue=counted)
    registry.catalogue_search("dotnet8", fetch_catalogue=counted)

    assert calls.count("mcr.microsoft.com") == 1, calls


# --- the hermetic rule ---------------------------------------------------------

def test_a_scripted_hub_search_does_not_quietly_sweep_live_catalogues():
    """`image_for` in api/resolve.py carries this rule already, and for the same
    reason: a test that scripts one source and silently acquires another has its
    assertions decided by what a vendor published this morning."""
    def exploding_catalogue(reg):  # pragma: no cover - must never be called
        raise AssertionError("a scripted search reached the network")

    registry._catalogues.clear()
    body = {"results": [{"repo_name": "mongo", "is_official": True}]}

    # fetch injected, fetch_catalogue absent -> catalogues are not consulted
    assert registry.search("mongodb", fetch=lambda code: body) == [
        "docker.io/library/mongo"]
    # and when a test DOES want them, it says so
    assert registry.search("mssql", fetch=lambda code: {"results": []},
                           fetch_catalogue=mcr("mssql/server")) == [
        "mcr.microsoft.com/mssql/server"]


def test_hubs_answer_still_comes_first():
    """Hub is a search ranked by a registry that knows what is actually used;
    a catalogue is an unranked list. Hub first, vendors after."""
    body = {"results": [{"repo_name": "mssql", "is_official": True}]}

    found = registry.search("mssql", fetch=lambda code: body,
                            fetch_catalogue=mcr("mssql/server"))

    assert found == ["docker.io/library/mssql", "mcr.microsoft.com/mssql/server"]


# --- what decides, once several images resolve ---------------------------------

def _describes(ports_by_image):
    def describe(image, tag="latest"):
        if image not in ports_by_image:
            return None
        return {"digest": "sha256:" + "a" * 64, "ports": ports_by_image[image]}
    return describe


def test_find_prefers_the_image_that_declares_it_listens(monkeypatch):
    """The `mongodb-atlas-local` mistake in its next disguise. `mssql/ha` and
    every `mssql/bdc/*` controller resolve perfectly well and are not SQL
    Server; `mssql/server` declares 1433. Rank on something MEASURED."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: [
        "mcr.microsoft.com/mssql/ha", "mcr.microsoft.com/mssql/server"])
    monkeypatch.setattr(registry, "describe", _describes({
        "mcr.microsoft.com/mssql/ha": [],
        "mcr.microsoft.com/mssql/server": [1433]}))

    found = registry.find("mssql")

    assert found["image"] == "mcr.microsoft.com/mssql/server", found
    assert found["ports"] == [1433]


def test_an_image_that_declares_nothing_is_still_returned_when_it_is_all_there_is(
        monkeypatch):
    """A preference, not a filter. Plenty of legitimate images declare no port,
    and refusing them all would be a worse error than ranking them last."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: ["docker.io/x/quiet"])
    monkeypatch.setattr(registry, "describe",
                        _describes({"docker.io/x/quiet": []}))

    found = registry.find("quiet")

    assert found is not None and found["image"] == "docker.io/x/quiet"


def test_find_still_answers_nothing_when_no_image_resolves(monkeypatch):
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: ["docker.io/x/nope"])
    monkeypatch.setattr(registry, "describe", _describes({}))

    assert registry.find("nope") is None


def test_the_number_of_images_measured_is_bounded(monkeypatch):
    """Each one is a manifest fetch against a public host, and a vendor
    catalogue can offer a dozen accessories around one product."""
    asked = []
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search",
                        lambda code: [f"docker.io/x/n{i}" for i in range(50)])

    def describe(image, tag="latest"):
        asked.append(image)
        return None

    monkeypatch.setattr(registry, "describe", describe)
    registry.find("many")

    assert len(asked) <= registry.FIND_LIMIT, len(asked)
