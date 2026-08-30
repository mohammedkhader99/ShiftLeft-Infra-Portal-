"""REQ-2026-0238: the right image, reported as the wrong one.

    image_opensearch=MISMATCH got
      docker.io/opensearchproject/opensearch@sha256:39a8f8c63028e8b5...

The recipe pinned `sha256:bcc179...` and the machine reported `sha256:39a8f8...`.
Both name the same image. `opensearchproject/opensearch` publishes a
MULTI-ARCHITECTURE INDEX — one digest naming a list of per-architecture
manifests — and podman, having pulled the index, reports the amd64 manifest it
actually resolved:

    pinned            sha256:bcc179...   the index
    the machine's own sha256:39a8f8...   its amd64/linux child

The check compared the two and said MISMATCH: correct on the evidence it had,
and wrong about the world. Measured against the live registry, `rabbitmq` is
multi-architecture too, and `mssql` is not — so this would have failed for most
modern images and passed for a few, which is the worst kind of intermittent.

THE CHECK IS NOT WEAKENED. A digest that names neither the index nor its
platform build is still a mismatch, and that is the whole reason the check
exists: a tag can be repointed after a proof, and what was certified and what a
request later installs would be different things wearing the same name.

Nothing here reaches the network.
"""

from __future__ import annotations

import json

import pytest

from api import ai_blueprint, registry
from common import profile_rules

INDEX = "sha256:" + "b" * 64
AMD64 = "sha256:" + "3" * 64
ARM64 = "sha256:" + "a" * 64


def index_manifest():
    return json.dumps({
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": ARM64, "platform": {"architecture": "arm64", "os": "linux"}},
            {"digest": AMD64, "platform": {"architecture": "amd64", "os": "linux"}},
        ],
    }).encode()


# --- reading the index ----------------------------------------------------------

def test_the_amd64_child_is_found():
    """These machines are amd64. The child that matters is the one they run."""
    assert registry._platform_digest(index_manifest()) == AMD64


def test_a_single_architecture_manifest_has_no_child():
    """`mcr.microsoft.com/mssql/server` is one of these. Inventing a platform
    digest for it would create a second name for an image that has one."""
    single = json.dumps({"config": {"digest": "sha256:x"}}).encode()

    assert registry._platform_digest(single) == ""


def test_an_index_with_no_amd64_build_answers_nothing():
    arm_only = json.dumps({"manifests": [
        {"digest": ARM64, "platform": {"architecture": "arm64", "os": "linux"}}]}).encode()

    assert registry._platform_digest(arm_only) == ""


def test_a_child_digest_that_is_not_a_sha256_is_refused():
    """It reaches a `podman pull` comparison on a real machine."""
    bad = json.dumps({"manifests": [
        {"digest": "not-a-digest", "platform": {"architecture": "amd64", "os": "linux"}}]}).encode()

    assert registry._platform_digest(bad) == ""


def test_unreadable_bytes_are_not_a_crash():
    assert registry._platform_digest(b"not json") == ""
    assert registry._platform_digest(b"") == ""


# --- carrying it through the recipe ---------------------------------------------

def test_the_recipe_may_declare_a_platform_digest():
    container = {"image": "docker.io/x/y", "tag": "latest", "digest": INDEX,
                 "platform_digest": AMD64,
                 "data_dir": "/var/lib/y", "data_mount": "/var/lib/y"}

    assert profile_rules.container_problems(container) == []


def test_a_platform_digest_that_is_not_pinned_is_refused():
    container = {"image": "docker.io/x/y", "tag": "latest", "digest": INDEX,
                 "platform_digest": "latest",
                 "data_dir": "/var/lib/y", "data_mount": "/var/lib/y"}

    assert profile_rules.container_problems(container)


def test_a_recipe_without_one_is_still_valid():
    """Single-architecture images have nothing to add, and every recipe written
    before today has none."""
    container = {"image": "docker.io/x/y", "tag": "latest", "digest": INDEX,
                 "data_dir": "/var/lib/y", "data_mount": "/var/lib/y"}

    assert profile_rules.container_problems(container) == []


def test_the_drafter_carries_it():
    draft = ai_blueprint.draft_from_image(
        "opensearch",
        {"image": "docker.io/opensearchproject/opensearch", "tag": "latest",
         "digest": INDEX, "platform_digest": AMD64, "ports": [9200]},
        target="oci")
    recipe = json.loads(next(iter(draft.files.values())))

    assert recipe["container"]["platform_digest"] == AMD64
    assert recipe["container"]["digest"] == INDEX


def test_the_drafter_omits_it_for_a_single_architecture_image():
    draft = ai_blueprint.draft_from_image(
        "mssql", {"image": "mcr.microsoft.com/mssql/server", "tag": "latest",
                  "digest": INDEX, "ports": [1433]}, target="oci")
    recipe = json.loads(next(iter(draft.files.values())))

    assert "platform_digest" not in recipe["container"]


# --- what describe() reports ----------------------------------------------------

def _answering(monkeypatch, manifest_bytes, digest):
    monkeypatch.setattr(registry, "_token", lambda *a, **k: "")
    monkeypatch.setattr(
        registry, "_get",
        lambda url, headers, want_headers=False: (
            # `_get` hands back raw bytes with the headers and a parsed body
            # without them — the same split the real one makes.
            ({"docker-content-digest": digest}, manifest_bytes) if want_headers
            else json.loads(manifest_bytes)))


def test_describe_reports_both_for_a_multi_architecture_tag(monkeypatch):
    _answering(monkeypatch, index_manifest(), INDEX)

    described = registry.describe("docker.io/opensearchproject/opensearch")

    assert described["digest"] == INDEX
    assert described["platform_digest"] == AMD64


def test_describe_reports_one_for_a_single_architecture_tag(monkeypatch):
    """A second name for an image that has one is a second thing to get wrong."""
    _answering(monkeypatch, b'{"config": {"digest": "sha256:x"}}', INDEX)

    assert "platform_digest" not in registry.describe("docker.io/library/x")


def test_describe_does_not_repeat_a_digest_that_equals_the_index(monkeypatch):
    same = json.dumps({"manifests": [
        {"digest": INDEX, "platform": {"architecture": "amd64", "os": "linux"}}]}).encode()
    _answering(monkeypatch, same, INDEX)

    assert "platform_digest" not in registry.describe("docker.io/library/x")
