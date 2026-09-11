"""Asking a registry what an image is, and pinning it (C8).

Three real machines established that RabbitMQ is not installable from Oracle
Linux 9's repositories or from EPEL 9. Its official image has been pulled nearly
four billion times. When a SOUND repository search comes back empty, "does this
publish an image" is the next honest question — and a registry answers it for
any technology without anyone here writing a row.

NOTHING HERE TOUCHES THE NETWORK. The transport is stubbed, because a test that
needs Docker Hub to be up is a test that fails for reasons having nothing to do
with this code. What the live registries actually return was measured separately
on 2026-08-24 and is recorded in the fixtures below.
"""

from __future__ import annotations

import json
from email.message import Message

import pytest

from api import registry
from common import profile_rules

# Measured against the real registries on 2026-08-24, not invented:
#   docker.io/library/rabbitmq   sha256:9d392587…  ports 4369 5671 5672 15691 15692 25672
#   docker.io/library/nginx      sha256:0d4374c7…  ports 80
#   quay.io/keycloak/keycloak    sha256:83133051…  ports 8080 8443 9000
DIGEST = "sha256:" + "9d392587" * 8


class _Response:
    """Enough of an HTTP response to exercise the real _get, headers included.

    STUBBED AT THE TRANSPORT, not at `_get`. An earlier version of this file
    replaced `_get` itself — which is where the header handling lives — so the
    case-insensitivity regression test below passed while testing nothing at all.
    `email.message.Message` is what urllib actually hands back, and its
    case-insensitive lookup is exactly the behaviour `dict()` throws away.
    """

    def __init__(self, headers: dict, body: bytes):
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRegistry:
    """The three requests a resolve makes: token, manifest, config blob."""

    def __init__(self, *, digest=DIGEST, exposed=None, header="docker-content-digest",
                 exists=True):
        self.digest, self.exists, self.header = digest, exists, header
        self.exposed = exposed if exposed is not None else {"5672/tcp": {}}
        self.asked: list[str] = []

    def urlopen(self, request, timeout=None):
        url = request.full_url
        self.asked.append(url)
        if "token" in url or "/auth" in url:
            return _Response({}, json.dumps({"token": "t"}).encode())
        if not self.exists:
            raise OSError("404")
        if "/manifests/" in url:
            body = json.dumps({"config": {"digest": "sha256:" + "c" * 64}}).encode()
            headers = {self.header: self.digest} if self.digest else {}
            return _Response(headers, body)
        if "/blobs/" in url:
            return _Response(
                {}, json.dumps({"config": {"ExposedPorts": self.exposed}}).encode())
        raise AssertionError(url)


@pytest.fixture()
def fake(monkeypatch):
    def install(reg):
        # THE TRANSPORT IS REPLACED, SO THE MODULE MAY PROCEED. conftest pins
        # REGISTRY_MODE=mock so the suite never reaches a real registry; these
        # tests have already put a fake socket in its place, and the code path
        # under test IS the live one — that is the whole point of stubbing here
        # rather than at `_get`. Saying so explicitly is what tells the guard
        # apart from an accidental network call.
        monkeypatch.setenv("REGISTRY_MODE", "live")
        monkeypatch.setattr(registry.urllib.request, "urlopen", reg.urlopen)
        return reg
    return install


# --- which references are worth asking about ----------------------------------

def test_the_conventional_shapes_are_tried_in_order():
    """A SHAPE, NOT A TABLE. Adding a technology must never mean adding a row."""
    assert registry.candidates("rabbitmq")[0] == "docker.io/library/rabbitmq"
    assert "quay.io/rabbitmq/rabbitmq" in registry.candidates("rabbitmq")


def test_a_catalogue_version_label_is_stripped():
    """`redis7` is what the catalogue calls it; `redis` is what the publisher
    calls the image — the same reasoning as candidate package names."""
    assert "docker.io/library/redis" in registry.candidates("redis7")
    assert registry.candidates("redis7")[0] == "docker.io/library/redis7"


@pytest.mark.parametrize("code", ["a; rm -rf /", "../../etc", "A/B", "", None])
def test_a_code_that_could_not_be_an_image_yields_nothing(code):
    assert registry.candidates(code) == []


def test_every_candidate_is_on_a_sanctioned_registry():
    for code in ("rabbitmq", "keycloak", "nginx"):
        for ref in registry.candidates(code):
            assert ref.partition("/")[0] in profile_rules.REGISTRIES


# --- resolving one image ------------------------------------------------------

def test_the_digest_comes_back_pinned(fake):
    fake(FakeRegistry())
    assert registry.describe("docker.io/library/rabbitmq")["digest"] == DIGEST


def test_the_digest_header_is_read_whatever_case_it_arrives_in(fake):
    """A REGRESSION TEST FOR A REAL BUG. `response.headers` is case-insensitive
    and `dict()` of it is not — it keeps whatever case the server sent. Docker
    Hub sends `docker-content-digest`, Quay sends `Docker-Content-Digest`, so
    Keycloak resolved and RabbitMQ came back as "no published image"."""
    for header in ("docker-content-digest", "Docker-Content-Digest",
                   "DOCKER-CONTENT-DIGEST"):
        fake(FakeRegistry(header=header))
        found = registry.describe("docker.io/library/rabbitmq")
        assert found and found["digest"] == DIGEST, f"missed with {header!r}"


@pytest.mark.parametrize("digest", [
    "sha256:" + "A" * 64, "sha256:short", "md5:" + "a" * 32, "", None,
])
def test_a_digest_that_is_not_a_sha256_is_no_answer(fake, digest):
    """Better to report no image than to pin something that is not a digest."""
    fake(FakeRegistry(digest=digest))
    assert registry.describe("docker.io/library/rabbitmq") is None


@pytest.mark.parametrize("image", [
    "evil.example.com/x/y", "attacker.io/x/y", "rabbitmq",
    "docker.io/a/../b", "", None,
])
def test_an_image_off_a_sanctioned_registry_is_never_asked_about(fake, image):
    """The allow-list applies BEFORE the request, so an unsanctioned host is
    never even contacted — no request, no leak of what this portal is building."""
    reg = fake(FakeRegistry())
    assert registry.describe(image) is None
    assert reg.asked == []


def test_a_tag_that_could_not_be_a_tag_is_refused(fake):
    reg = fake(FakeRegistry())
    assert registry.describe("docker.io/library/nginx", "a b; rm -rf /") is None
    assert reg.asked == []


def test_an_unreachable_registry_is_a_no_not_a_crash(fake):
    """The portal must fall back to what it did before, never refuse a request
    because a public service was down."""
    fake(FakeRegistry(exists=False))
    assert registry.describe("docker.io/library/rabbitmq") is None
    assert registry.find("rabbitmq") is None


# --- the ports the image itself declares --------------------------------------

def test_the_ports_come_from_the_image_not_from_a_guess(fake):
    fake(FakeRegistry(exposed={"5672/tcp": {}, "15672/tcp": {}}))
    assert registry.describe("docker.io/library/rabbitmq")["ports"] == [5672, 15672]


def test_udp_and_nonsense_ports_are_left_out(fake):
    fake(FakeRegistry(exposed={"5672/tcp": {}, "9999/udp": {}, "0/tcp": {},
                               "99999/tcp": {}, "abc/tcp": {}}))
    assert registry.describe("docker.io/library/x")["ports"] == [5672]


def test_an_image_declaring_no_ports_is_a_real_answer(fake):
    """A container exposing nothing needs no firewall opened for it, and
    inventing a port would open one to nothing."""
    fake(FakeRegistry(exposed={}))
    assert registry.describe("docker.io/library/x")["ports"] == []


# --- finding one for a technology ---------------------------------------------

def test_the_first_published_image_wins(fake):
    fake(FakeRegistry())
    found = registry.find("rabbitmq")
    assert found["image"] == "docker.io/library/rabbitmq"
    assert found["tag"] == "latest" and found["digest"] == DIGEST


def test_software_that_publishes_no_image_returns_nothing(fake):
    """Internal and licensed software will always need a recipe someone writes.
    The ladder treats None as the end of the road, not as a failure."""
    fake(FakeRegistry(exists=False))
    assert registry.find("some-internal-thing") is None


def test_what_it_returns_would_pass_the_profile_linter(fake):
    """Whatever comes back is going to a `podman pull` run by root, so it faces
    the same allow-lists as a hand-written profile — checked here rather than
    trusted at the point of use."""
    fake(FakeRegistry())
    found = registry.find("rabbitmq")
    container = {"image": found["image"], "tag": found["tag"],
                 "digest": found["digest"], "data_dir": "/var/lib/rabbitmq",
                 "data_mount": "/var/lib/rabbitmq"}
    assert profile_rules.container_problems(container) == []
