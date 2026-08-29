"""`latest` is not the only tag, and for some publishers it is not a tag at all.

REQ-2026-0232 asked for Elasticsearch and got an EMPTY LADDER — no package, no
repo, no archive, no container — so `ensure` fell through to drafting Terraform
and certified a `dnf install elasticsearch` guess that installed nothing.

The image was there the whole time:

    docker.io/library/elasticsearch    406 tags | latest present: FALSE

Docker Hub's official image was deprecated and its `latest` tag withdrawn.
`find` asked only for `latest`, so an image that is published, official, and on
an allow-listed registry was invisible — and its absence was then read as "this
software publishes nothing", which is a different statement entirely.

THE SAME SHAPE AS EVERY OTHER DEFECT THIS FORTNIGHT: a narrower question asked
than the one that needed answering, and the narrow answer treated as the whole
truth.

HIGHEST RELEASE, NOT A PIN. A pinned tag per technology is a table, and tables
do not survive contact with the next technology. The choice is measured — the
tag must resolve to a real manifest, and the digest is what gets pinned — and it
is still proved on a machine before anything is certified, so a wrong guess
costs a proof rather than a provisioning.

Nothing here reaches the network.
"""

from __future__ import annotations

import pytest

from api import registry


def lists(*tags):
    """A scripted registry tag listing."""
    return lambda image: {"tags": list(tags)}


# --- what counts as a release ---------------------------------------------------

@pytest.mark.parametrize("tag,is_release", [
    ("9.5.2", True),
    ("1", True),
    ("v3", True),
    ("1.24.0", True),
    ("latest", False),
    ("1-alpine", False),          # a different base image, not a version
    ("8.15.0-arm64", False),      # a different architecture
    ("2017-CU1-ubuntu", False),   # a variant, and how mcr names SQL Server
    ("8.0.0-rc1", False),         # not a release
    ("nightly", False),
    ("edge", False),
    ("", False),
])
def test_only_a_release_tag_is_a_candidate(tag, is_release):
    assert bool(registry._RELEASE_TAG.match(tag)) is is_release


def test_the_newest_release_wins():
    assert registry.newest_release(
        "docker.io/library/x",
        fetch=lists("1", "7.17.0", "8.15.0", "9.4.5", "9.5.1", "9.5.2")) == "9.5.2"


def test_ordering_is_numeric_and_not_a_string_sort():
    """A string sort puts `9.5.2` after `10.0.0`, which would pin a technology a
    whole major version behind for as long as nobody looked."""
    assert registry.newest_release(
        "docker.io/library/x", fetch=lists("9.5.2", "10.0.0")) == "10.0.0"


def test_variants_and_prereleases_never_win():
    """`9.5.2-arm64` is the same release on another architecture and
    `10.0.0-rc1` is not released at all. Choosing either as THE image is the
    `mongodb-atlas-local` mistake in another costume."""
    assert registry.newest_release(
        "docker.io/library/x",
        fetch=lists("9.5.2", "9.5.2-arm64", "10.0.0-rc1", "nightly")) == "9.5.2"


def test_an_image_with_no_release_tags_answers_nothing():
    """`mcr.microsoft.com/mssql/server` names every tag `2017-CU1-ubuntu` and
    the like. It publishes `latest`, so it never needs this — and an honest
    empty answer is what must come back when it does not."""
    assert registry.newest_release(
        "docker.io/library/x", fetch=lists("2017-CU1-ubuntu", "2022-latest")) == ""


def test_a_registry_that_will_not_list_is_not_a_failure():
    def explode(image):
        raise RuntimeError("401 Unauthorized")

    assert registry.tags("docker.io/library/x", fetch=explode) == []
    assert registry.newest_release("docker.io/library/x", fetch=explode) == ""


def test_an_image_outside_the_allow_list_is_never_asked(monkeypatch):
    """The reference becomes a URL, so it is bounded the same way every other
    image reference in this module is.

    ASSERTS THAT NOTHING IS ATTEMPTED, not that nothing comes back. Two earlier
    versions of this test were satisfied by the wrong thing:

      * checking only the return value — [] either way, because a request to a
        host that does not exist fails and is swallowed. It passed with the
        guard removed, while the code made an HTTP request to a host an
        attacker chose;
      * raising AssertionError from a sentinel — swallowed too, because
        AssertionError IS an Exception and these functions are fail-soft by
        design, catching everything so one bad registry cannot end a ladder.

    So the calls are RECORDED and the record is asserted, which nothing can
    swallow."""
    attempted: list = []

    monkeypatch.setattr(registry, "_get",
                        lambda *a, **kw: attempted.append(a) or {})
    monkeypatch.setattr(registry, "_token",
                        lambda *a, **kw: attempted.append(a) or "")

    assert registry.tags("evil.example.com/x/y") == []
    assert registry.tags("docker.io/../../etc/passwd") == []
    assert registry.newest_release("evil.example.com/x/y") == ""
    assert attempted == [], (
        f"a request was made to a registry we do not allow: {attempted}")


# --- and what `find` does with it -----------------------------------------------

def _describes(available):
    """A registry where only these (image, tag) pairs resolve."""
    def describe(image, tag="latest"):
        if (image, tag) not in available:
            return None
        return {"digest": "sha256:" + "e" * 64, "ports": available[(image, tag)]}
    return describe


def test_an_image_with_no_latest_is_still_found(monkeypatch):
    """THE test. Elasticsearch exactly: published, official, allow-listed, and
    invisible because it does not publish `latest`."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search",
                        lambda code: ["docker.io/library/elasticsearch"])
    monkeypatch.setattr(registry, "describe", _describes({
        ("docker.io/library/elasticsearch", "9.5.2"): [9200, 9300]}))
    monkeypatch.setattr(registry, "newest_release", lambda image, **kw: "9.5.2")

    found = registry.find("elasticsearch")

    assert found is not None, "an image that publishes no `latest` is still invisible"
    assert found["tag"] == "9.5.2"
    assert found["ports"] == [9200, 9300]


def test_latest_is_still_preferred_when_it_exists(monkeypatch):
    """THE REGRESSION GUARD. RabbitMQ, SQL Server, nginx, redis and mongodb all
    publish `latest`, and every one of them was resolved through it. The
    fallback must never take a technology off the tag it has always used."""
    asked: list = []

    def newest(image, **kw):
        asked.append(image)
        return "4.3.5"

    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search",
                        lambda code: ["docker.io/library/rabbitmq"])
    monkeypatch.setattr(registry, "describe", _describes({
        ("docker.io/library/rabbitmq", "latest"): [5672],
        ("docker.io/library/rabbitmq", "4.3.5"): [5672]}))
    monkeypatch.setattr(registry, "newest_release", newest)

    found = registry.find("rabbitmq")

    assert found["tag"] == "latest", found
    assert asked == [], "the tag list was fetched for an image that has `latest`"


def test_an_explicit_tag_is_never_second_guessed(monkeypatch):
    """A caller asking for a specific tag means it. The fallback exists for the
    DEFAULT only."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: ["docker.io/library/x"])
    monkeypatch.setattr(registry, "describe", _describes({
        ("docker.io/library/x", "9.5.2"): [1]}))
    monkeypatch.setattr(registry, "newest_release",
                        lambda image, **kw: "9.5.2")

    assert registry.find("x", tag="8.0.0") is None


def test_an_image_with_neither_latest_nor_a_release_is_still_nothing(monkeypatch):
    """An honest None. Better than a tag that does not resolve."""
    monkeypatch.setattr(registry, "candidates", lambda code: [])
    monkeypatch.setattr(registry, "search", lambda code: ["docker.io/library/x"])
    monkeypatch.setattr(registry, "describe", _describes({}))
    monkeypatch.setattr(registry, "newest_release", lambda image, **kw: "")

    assert registry.find("x") is None
