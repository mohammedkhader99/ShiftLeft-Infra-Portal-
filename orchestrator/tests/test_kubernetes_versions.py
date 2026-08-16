"""The Kubernetes version comes from OCI, not from a constant in this repository.

REQ-2026-0148 was the first cluster anyone asked this portal for, and it failed at
apply: "Invalid Kubernetes version v1.29.1. Supported versions: [v1.33.0 …
v1.36.1]". The module carried default_kubernetes_version = "v1.29.1", true when
written and retired since.

The same shape as the catalogue offering nginx 1.20 on an Oracle Linux 9.8 with
no such stream — a version hard-coded in the repo, correct once, with nothing
checking it against the thing that has to accept it. A cloud retires versions on
its own schedule and no file here can be edited often enough to keep up.
"""

from __future__ import annotations

import pytest

from orchestrator import kubernetes_versions as kv

LIVE = ["v1.33.0", "v1.33.1", "v1.33.10", "v1.34.0", "v1.34.1", "v1.34.2",
        "v1.35.0", "v1.35.2", "v1.36.0", "v1.36.1"]


class _Client:
    def __init__(self, versions=LIVE, fail=False):
        self._v, self._fail = versions, fail

    def get_cluster_options(self, cluster_option_id):
        if self._fail:
            raise RuntimeError("NotAuthorizedOrNotFound")
        return type("R", (), {"data": type("D", (), {
            "kubernetes_versions": self._v})()})()


@pytest.fixture(autouse=True)
def _no_cache():
    kv._cache, kv._fetched_at = [], 0.0
    yield
    kv._cache, kv._fetched_at = [], 0.0


def test_the_newest_is_chosen_when_nothing_is_configured():
    assert kv.resolve("", _Client())[0] == "v1.36.1"


def test_a_double_digit_patch_does_not_beat_a_higher_minor():
    """OCI returns an order that LOOKS sorted and is not: v1.33.1 comes before
    v1.33.10, which is string order. Taking the last element works today and
    picks a lower version the moment a double-digit patch lands mid-list."""
    assert kv.newest(_Client(["v1.33.1", "v1.33.10", "v1.9.0"])) == "v1.33.10"
    # The cache is per-process and holds for an hour, so a second client inside
    # the TTL would otherwise be answered from the first one's list — which is
    # the cache doing its job, and made the first version of this test fail.
    kv._cache, kv._fetched_at = [], 0.0
    assert kv.newest(_Client(["v1.36.1", "v1.33.10"])) == "v1.36.1"


def test_a_retired_version_is_REPORTED_not_silently_replaced():
    """THE rule. Quietly substituting a working version would build something
    other than what was asked for — the failure this codebase spent a week
    removing. REQ-2026-0148 asked for v1.29.1."""
    version, why = kv.resolve("v1.29.1", _Client())
    assert version == ""
    assert "no longer accepts" in why
    assert "v1.36.1" in why, "the message must name what IS accepted"


def test_a_version_oci_still_accepts_is_honoured():
    """Pinning a specific version is legitimate — a cluster may need to match
    what a team already runs."""
    assert kv.resolve("v1.34.0", _Client()) == ("v1.34.0", "")


def test_an_unreachable_oci_does_not_block_a_configured_version():
    """A portal that cannot reach OCI has worse problems than a version default,
    and refusing here would hide them behind the wrong error."""
    assert kv.resolve("v1.34.0", _Client(fail=True))[0] == "v1.34.0"


def test_an_unreachable_oci_with_nothing_configured_says_so():
    """It must not invent a version. Guessing is how v1.29.1 got there."""
    version, why = kv.resolve("", _Client(fail=True))
    assert version == ""
    assert "OCI_OKE_KUBERNETES_VERSION" in why, "it has to say what would fix it"


def test_an_empty_answer_is_unknown_not_none_supported():
    """OCI returning nothing must not read as 'no version works', which would
    refuse every cluster."""
    assert kv.supported(_Client([])) == []
    assert kv.resolve("v1.34.0", _Client([]))[0] == "v1.34.0"


def test_the_answer_is_cached_rather_than_asked_per_request():
    calls = {"n": 0}

    class Counting(_Client):
        def get_cluster_options(self, cluster_option_id):
            calls["n"] += 1
            return super().get_cluster_options(cluster_option_id)

    client = Counting()
    kv.supported(client)
    kv.supported(client)
    assert calls["n"] == 1
