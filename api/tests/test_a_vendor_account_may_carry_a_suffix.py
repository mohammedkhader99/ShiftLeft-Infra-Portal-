"""REQ-2026-0236 asked for OpenSearch and was told no image exists.

    Docker Hub returns : opensearchproject/opensearch   198 stars
    the trust rule     : the namespace must EQUAL the technology's name
                         "opensearchproject" != "opensearch"  -> refused
    the request        : "no container image was found on a registry this
                          portal trusts" -> manual fulfilment

True as written, and misleading: the image is there and the vendor is obvious.
A vendor account carrying a suffix is the norm, not the exception.

THE RULE THIS DOES NOT LOOSEN. It exists to keep out `slacksec/dotnet` — zero
stars, an unknown account — because what is at stake is what runs as ROOT on a
machine. So the suffixed form is trusted only when BOTH halves name the
technology: the account AND the repository.

SURVEYED AGAINST WHAT DOCKER HUB ACTUALLY RETURNS before it was switched on,
which is where the cases below come from — not from imagination.

AND IT IS A REAL WIDENING. An account named `opensearchmalware` would satisfy
it. What stands behind it: both halves must match, the image is pinned by
digest, ranking prefers stars within what is already trusted, and nothing is
certified until a machine has built it. Written down so the next person weighing
this has the same facts rather than a reassurance.

Nothing here reaches the network.
"""

from __future__ import annotations

import pytest

from api.registry import _trusted


# --- the account that was refused -----------------------------------------------

def test_a_vendor_account_with_a_suffix_is_trusted():
    """THE test. OpenSearch's own project account, 198 stars."""
    assert _trusted("opensearchproject/opensearch", False, "opensearch") is True


def test_the_exact_namespace_still_works():
    """Unchanged, and it is how mongodb, keycloak and grafana resolve today."""
    assert _trusted("mongodb/mongodb-community-server", False, "mongodb") is True
    assert _trusted("keycloak/keycloak", False, "keycloak") is True
    assert _trusted("grafana/grafana", False, "grafana") is True


def test_an_official_image_is_still_trusted_outright():
    assert _trusted("elasticsearch", True, "elasticsearch") is True
    assert _trusted("rabbitmq", True, "rabbitmq") is True


# --- and everyone else is still kept out ----------------------------------------

@pytest.mark.parametrize("repo,code,why", [
    ("onlyoffice/opensearch", "opensearch", "somebody else's product"),
    ("rancher/opensearch", "opensearch", "somebody else's product"),
    ("bitnamicharts/opensearch", "opensearch", "a packager, not the vendor"),
    ("corpusops/opensearch", "opensearch", "a packager"),
    ("mcp/elasticsearch", "elasticsearch", "a stranger"),
    ("itzg/elasticsearch", "elasticsearch", "72 stars and still a stranger"),
    ("slacksec/dotnet", "dotnet8", "THE image this rule exists for"),
    ("someone/dotnet", "dotnet8", "popularity is not provenance"),
])
def test_a_stranger_is_still_a_stranger(repo, code, why):
    assert _trusted(repo, False, code) is False, why


def test_vendor_tooling_under_the_same_account_is_not_the_technology():
    """BOTH halves have to name it. `opensearchproject` publishes dashboards,
    operators and build tooling; asking for OpenSearch must not get one of
    those."""
    assert _trusted("opensearchproject/opensearch-dashboards", False,
                    "opensearch") is True   # it does name the technology
    assert _trusted("opensearchproject/dashboards", False, "opensearch") is False
    assert _trusted("opensearchproject/build-ci", False, "opensearch") is False
    # STARTS WITH, not CONTAINS — and this is the case that tells them apart.
    # A repository merely MENTIONING the technology is a fork, an archive or a
    # chart, not the thing itself. Without this line, loosening the tail check
    # to `contains` broke no test at all.
    assert _trusted("opensearchproject/legacy-opensearch-archive", False,
                    "opensearch") is False
    assert _trusted("opensearchproject/helm-charts-opensearch", False,
                    "opensearch") is False


def test_a_namespace_that_merely_contains_the_name_is_not_the_vendor():
    """`starts with`, not `contains`. `not-opensearch-really/opensearch` is a
    stranger wearing the name in the middle of theirs."""
    assert _trusted("notopensearchreally/opensearch", False, "opensearch") is False
    assert _trusted("myopensearch/opensearch", False, "opensearch") is False


def test_a_bare_repository_with_no_namespace_is_never_trusted_unofficially():
    """`library/x` is Docker Hub's own official namespace and arrives with
    is_official set. A bare name without it is somebody's personal repo."""
    assert _trusted("opensearch", False, "opensearch") is False


def test_an_empty_or_unusable_code_trusts_nothing():
    assert _trusted("opensearchproject/opensearch", False, "") is False
    assert _trusted("opensearchproject/opensearch", False, "123") is False


# --- what it deliberately does NOT fix ------------------------------------------

def test_kafka_is_still_not_found_and_that_is_honest():
    """`apache/kafka` is Kafka's real image, and nothing in the word "kafka"
    points at Apache. No name rule can bridge that, and pretending otherwise
    would mean trusting `apache/*` for anything — which is precisely the
    stranger-shaped hole this rule exists to keep shut. Kafka needs a
    first-party catalogue entry or a curated one."""
    assert _trusted("apache/kafka", False, "kafka") is False
    assert _trusted("hashicorp/vault", False, "vault") is False
