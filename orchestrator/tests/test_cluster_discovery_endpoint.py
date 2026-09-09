"""The orchestrator lists clusters; the API holds no cloud credentials.

Read-only and signature-only, like /catalogue/oci-options: every call underneath
is a list_*, so there is no execution gate to pass and nothing here can create,
change or destroy anything.

The property worth defending is the answer shape when the tenancy cannot be READ.
A reachable orchestrator that cannot see the tenancy is a different thing from an
unreachable one, and the caller must be able to tell them apart — otherwise "we
could not look" reaches a requester as "you are entitled to no cluster", which is
a statement about them rather than about the portal.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as omain
from orchestrator import cluster_discovery


def _signed(payload: dict):
    from common.signing import sign
    body = json.dumps(payload).encode()
    return body, sign(omain.WEBHOOK_SECRET, body)


@pytest.fixture()
def client():
    with TestClient(omain.app) as c:
        yield c


def ask(client, signature=None):
    body, sig = _signed({"operation": "clusters"})
    return client.post("/catalogue/clusters", content=body,
                       headers={"X-Signature": signature or sig})


def test_an_unsigned_request_is_refused(client):
    """The only gate this endpoint has, so it had better be the real one."""
    assert ask(client, signature="not-the-signature").status_code == 401


def test_a_signed_request_returns_the_clusters(client, monkeypatch):
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    body = ask(client).json()

    assert body["ok"] is True
    assert body["count"] == len(body["clusters"]) > 0


def test_a_tenancy_that_cannot_be_read_answers_200_with_the_reason(client, monkeypatch):
    """NOT a 5xx. The caller distinguishes "could not look" from "unreachable"
    by getting an answer at all, and only an answer can carry the reason."""
    def blind():
        raise cluster_discovery.ClusterDiscoveryUnavailable("no credentials")

    monkeypatch.setattr(cluster_discovery, "fetch", blind)
    response = ask(client)
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is False
    assert body["clusters"] == [] and body["count"] == 0
    assert "no credentials" in body["reason"]


def test_the_capacity_caveat_crosses_the_boundary(client, monkeypatch):
    """It has to survive the wire, not merely exist in the module. A consumer
    reading JSON is exactly the one who could mistake capacity for allocatable
    capacity."""
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    body = ask(client).json()

    assert body["capacity_is_an_upper_bound"] is True
    assert all(c["capacity_is_an_upper_bound"] for c in body["clusters"])


def test_no_cluster_row_carries_a_credential(client, monkeypatch):
    """A cluster OCID identifies a resource; it is not a secret. Nothing else
    from the tenancy should be crossing this boundary."""
    monkeypatch.setenv("OCI_CLUSTER_DISCOVERY_MODE", "mock")
    allowed = {"id", "name", "region", "lifecycle_state", "kubernetes_version",
               "project_code", "cost_centre_code", "capacity_vcpu",
               "capacity_memory_gb", "node_count", "capacity_is_an_upper_bound"}

    for cluster in ask(client).json()["clusters"]:
        assert set(cluster) <= allowed, set(cluster) - allowed
