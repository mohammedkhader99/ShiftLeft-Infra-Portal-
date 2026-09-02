"""Oracle publishes where no rule this portal has could ever look.

REQ-2026-0265 asked for Oracle Database and was refused with "no image on a
registry this portal allows". True as written, and misleading three times over
-- the registry WAS allowed, and three separate things stopped it being reached.
Each was checked against the live registry before this was written:

    the shapes      `oracle-db` produces docker.io/library/oracle-db and
                    quay.io/oracle-db/oracle-db. Neither exists, and no
                    name-shape rule will ever produce `database/free`.

    the token       every manifest came back 401, because Oracle's auth realm is
                    `/auth` with service "Oracle Registry" and the client only
                    knew Docker Hub's, Quay's and GHCR's. The registry had been
                    on the allow-list, unusable, since the list was broadened.

    the catalogue   the first-party sweep that found SQL Server at
                    mcr.microsoft.com asks /v2/_catalog. Oracle answers 401 and
                    will not issue a catalog-scope token anonymously, so the
                    sweep learns nothing at all.

    the match rule  and even given the full catalogue, `_first_party_match` asks
                    whether the first path segment IS the technology. Microsoft
                    groups by product -- `mssql/server` for `mssql`. Oracle
                    groups by FAMILY -- `database/free` -- so the segment is
                    `database`, which matches no sensible catalogue code.

So the image path cannot be discovered. It has to be stated, and PUBLISHED_IMAGES
is where a human states it. That is a table, and `_SHAPES` says "a shape, not a
table" -- the distinction is that a shape is for images that CAN be guessed, and
this is for the ones that provably cannot. VENDOR_REPOS and ARCHIVE_KNOWLEDGE
are curated for the same reason.

Nothing here reaches the network.
"""

from __future__ import annotations

import pytest

from api import registry
from common import profile_rules


def _describes(known):
    def describe(image, tag="latest"):
        if image not in known:
            return None
        return {"digest": "sha256:" + "a" * 64, "ports": known[image]}
    return describe


# --- it is consulted, and only as a last resort ----------------------------------

def test_a_curated_image_is_found_when_nothing_can_guess_it(monkeypatch):
    """THE DEFECT. Discovery returns nothing for `oracle-db`, and before this
    that was the end of the ladder."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: [])
    monkeypatch.setattr(registry, "PUBLISHED_IMAGES",
                        {"thing": "container-registry.oracle.com/family/thing"})
    monkeypatch.setattr(registry, "describe", _describes(
        {"container-registry.oracle.com/family/thing": [1521]}))

    found = registry.find("thing")

    assert found is not None, "a curated image was not consulted"
    assert found["image"] == "container-registry.oracle.com/family/thing"
    assert found["ports"] == [1521]


def test_discovery_always_wins_over_the_curated_row(monkeypatch):
    """A curated entry that could OUTRANK discovery would rot silently: the day
    a vendor starts publishing somewhere guessable, the stale row would keep
    being returned and nothing would notice. Last, always."""
    monkeypatch.setattr(registry, "candidates",
                        lambda code: ["docker.io/library/thing"])
    monkeypatch.setattr(registry, "search", lambda code: [])
    monkeypatch.setattr(registry, "PUBLISHED_IMAGES",
                        {"thing": "container-registry.oracle.com/family/thing"})
    monkeypatch.setattr(registry, "describe", _describes({
        "docker.io/library/thing": [1521],
        "container-registry.oracle.com/family/thing": [1521]}))

    found = registry.find("thing")

    assert found["image"] == "docker.io/library/thing", (
        "the curated row outranked an image discovery actually found")


def test_a_technology_with_no_curated_row_is_unaffected(monkeypatch):
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: [])
    monkeypatch.setattr(registry, "PUBLISHED_IMAGES", {"other": "docker.io/x/y"})
    monkeypatch.setattr(registry, "describe", _describes({"docker.io/x/y": []}))

    assert registry.find("thing") is None


def test_a_curated_image_is_still_pinned_by_digest(monkeypatch):
    """Curation says WHERE a vendor publishes. It says nothing about what is in
    the image, so `describe` still runs and a reference that does not resolve is
    still nothing. `database/enterprise` is exactly this case today: curated,
    and 401 without a licence credential."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: [])
    monkeypatch.setattr(registry, "PUBLISHED_IMAGES",
                        {"licensed": "container-registry.oracle.com/family/paid"})
    monkeypatch.setattr(registry, "describe", _describes({}))   # 401 / absent

    assert registry.find("licensed") is None, (
        "a curated reference was returned without being resolved")


# --- what a row is allowed to say ------------------------------------------------

@pytest.mark.parametrize("code, ref", sorted(registry.PUBLISHED_IMAGES.items()))
def test_every_curated_row_names_an_allow_listed_registry(code, ref):
    """Curation must not smuggle a registry past the allow-list. That list is
    the boundary of what may execute as root in the tenancy, and a row here is
    written by hand -- exactly the kind of edit that skips a review."""
    host = ref.partition("/")[0]

    assert host in profile_rules.REGISTRIES, (
        f"{code} points at {host}, which is not an allow-listed registry")


@pytest.mark.parametrize("code, ref", sorted(registry.PUBLISHED_IMAGES.items()))
def test_every_curated_row_is_a_valid_image_path(code, ref):
    path = ref.partition("/")[2]

    assert profile_rules.IMAGE_PATH.match(path), f"{code}: {path!r}"
    assert profile_rules.CODE.match(code), f"{code!r} is not a catalogue code"


def test_oracles_free_editions_are_the_ones_curated():
    """The two that pull anonymously. Enterprise is curated too and resolves to
    nothing until a licence credential exists — which is the honest state, not a
    gap: the ladder reports no image rather than pretending."""
    assert registry.PUBLISHED_IMAGES["oracle-free"].endswith("/database/free")
    assert registry.PUBLISHED_IMAGES["oracle-xe"].endswith("/database/express")
    assert registry.PUBLISHED_IMAGES["oracle-db"].endswith("/database/enterprise")


def test_the_curated_map_stays_small():
    """A guard on the principle, not on the number. `_SHAPES` says "a shape, not
    a table", and this table is the exception for images that provably cannot be
    guessed. If it grows into a catalogue of every technology, the shape rules
    have stopped working and that is the thing to fix."""
    assert len(registry.PUBLISHED_IMAGES) <= 12, (
        f"{len(registry.PUBLISHED_IMAGES)} curated images — discovery is being "
        f"replaced by a lookup table rather than repaired")


# --- the realm itself, which nothing was pinning --------------------------------

def test_oracles_auth_realm_is_actually_requested(monkeypatch):
    """THE FIX THAT NO TEST PROTECTED.

    Adding `container-registry.oracle.com` to the token realms is the whole
    reason Oracle became reachable, and deleting it again passed every test in
    this repository — because they all stub the transport, so nothing noticed
    that the client had stopped asking for a token. That is exactly how the
    registry sat on the allow-list, unusable, for weeks.

    So this asserts the BEHAVIOUR without the network: given a stubbed
    transport, the client must actually request Oracle's realm. A registry with
    no entry requests nothing, and this fails.
    """
    asked = []

    def urlopen(request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        asked.append(url)
        raise RuntimeError("stop here — the request is the assertion")

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)

    registry._token("container-registry.oracle.com", "database/free")

    assert asked, "no token was requested for Oracle — the realm is missing"
    url = asked[0]
    assert url.startswith("https://container-registry.oracle.com/auth"), url
    assert "service=Oracle%20Registry" in url, (
        "Oracle's service is 'Oracle Registry' with a space; the wrong service "
        "name returns 'Not found' and every manifest then 401s")
    assert "scope=repository:database/free:pull" in url, url


def test_a_registry_with_no_realm_asks_for_no_token(monkeypatch):
    """The other half: the map is consulted, not bypassed. Anonymous registries
    like mcr.microsoft.com need no token, and inventing one for them would break
    the pulls that work today."""
    asked = []
    monkeypatch.setattr(registry.urllib.request, "urlopen",
                        lambda *a, **k: asked.append(a) or (_ for _ in ()).throw(
                            RuntimeError("should not be called")))

    assert registry._token("mcr.microsoft.com", "mssql/server") == ""
    assert not asked, "a token was requested for a registry that needs none"
