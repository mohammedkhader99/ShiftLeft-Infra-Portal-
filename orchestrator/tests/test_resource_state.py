"""Proof for things that cannot boot-report.

A machine proves itself by writing a self-report at the end of first boot. A
Kubernetes cluster cannot — OKE's worker nodes boot an Oracle-managed image this
project never renders — and neither can a bucket or a managed database, which
have no operating system at all.

The certification gate let all three through vacuously, on the grounds that
"boots no machine" means "no boot evidence applies". True, and NOT the same as
"nothing to prove": postgres16 and oci-objectstorage were certifiable with no
evidence whatsoever, and OKE would have been the moment it was wired.
"""

from __future__ import annotations

import pytest

from orchestrator import resource_state as rs


class _Pool:
    def __init__(self, name, size, active):
        self.id, self.name = "p1", name
        self.node_config_details = type("C", (), {"size": size})()
        self.nodes = [type("N", (), {"lifecycle_state": "ACTIVE"})()] * active


class _Container:
    def __init__(self, clusters, pools=()):
        self._clusters, self._pools = clusters, list(pools)

    def list_clusters(self, **kw):
        return type("R", (), {"data": self._clusters, "has_next_page": False,
                              "next_page": None})()

    def list_node_pools(self, **kw):
        return type("R", (), {"data": self._pools, "has_next_page": False,
                              "next_page": None})()

    def get_node_pool(self, _id):
        return type("R", (), {"data": self._pools[0]})()


def _cluster(state="ACTIVE", name="c1"):
    return type("C", (), {"id": "x", "name": name, "lifecycle_state": state})()


@pytest.fixture(autouse=True)
def _compartment(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_COMPARTMENT_OCID", "ocid1.compartment.x")


def _check(kind, name, container):
    return rs.check(kind, name, clients=(container, None))


# --- A cluster is not proven by existing ---------------------------------------

def test_an_active_cluster_with_its_nodes_is_ok():
    result = _check("oci-oke", "c1", _Container([_cluster()], [_Pool("np", 3, 3)]))
    assert result["state"] == "ok"


def test_an_active_cluster_with_NO_node_pool_is_broken():
    """THE cluster-shaped version of a VM that booted with no software on it: the
    API answers and nothing runs."""
    result = _check("oci-oke", "c1", _Container([_cluster()], []))
    assert result["state"] == "broken"
    assert "no node pool" in result["detail"]


def test_a_pool_short_of_its_nodes_is_not_yet_ok():
    """Nodes take minutes after the cluster reports ACTIVE, so this waits rather
    than failing — but it must not read as healthy in the meantime."""
    result = _check("oci-oke", "c1", _Container([_cluster()], [_Pool("np", 3, 1)]))
    assert result["state"] == "waiting"
    assert "1 of 3" in result["detail"]


def test_a_cluster_still_creating_is_waiting():
    result = _check("oci-oke", "c1", _Container([_cluster("CREATING")]))
    assert result["state"] == "waiting"


def test_a_failed_cluster_is_broken_not_waiting():
    """FAILED will never become ACTIVE. Waiting on it burns the deadline and
    reports the wrong reason at the end of it."""
    result = _check("oci-oke", "c1", _Container([_cluster("FAILED")]))
    assert result["state"] == "broken"


def test_a_missing_cluster_is_waiting_not_broken():
    assert _check("oci-oke", "c1", _Container([]))["state"] == "waiting"


def test_a_deleted_cluster_of_the_same_name_is_ignored():
    """Rebuilding a request reuses the name. Reading the old DELETED cluster
    would report the previous attempt's fate as this one's."""
    result = _check("oci-oke", "c1",
                    _Container([_cluster("DELETED"), _cluster("ACTIVE")],
                               [_Pool("np", 1, 1)]))
    assert result["state"] == "ok"


# --- Unknown is never permission ----------------------------------------------

def test_a_kind_with_no_check_is_unknown_not_healthy():
    """`unknown` blocks certification. Returning 'ok' for anything unrecognised
    would certify every future resource kind on no evidence at all."""
    assert _check("oci-something-new", "x", _Container([]))["state"] == "unknown"


def test_postgres_is_explicitly_unproven_rather_than_guessed():
    """It has no check yet. Saying so blocks certification; guessing would grant
    it."""
    result = _check("oci-postgres", "x", _Container([]))
    assert result["state"] == "unknown"
    assert "cannot be proven" in result["detail"]


def test_no_compartment_raises_rather_than_answering():
    """Being unable to ask is not an answer, and must not become one."""
    import os
    for key in ("OCI_COMPUTE_COMPARTMENT_OCID", "OCI_COMPARTMENT_OCID"):
        os.environ.pop(key, None)
    with pytest.raises(rs.StateUnavailable):
        _check("oci-oke", "c1", _Container([]))


def test_the_checkable_kinds_are_declared():
    """A kind absent from CHECKABLE returns unknown, so this list is what can be
    proven at all — worth being explicit about."""
    assert "oci-oke" in rs.CHECKABLE and "oci-bucket" in rs.CHECKABLE
