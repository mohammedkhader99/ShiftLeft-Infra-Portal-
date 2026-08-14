"""A partial teardown removes what it names; a full one sweeps everything.

The orchestrator destroyed by WORKSPACE rather than by request:

    # Destroy is driven by the workspaces that EXIST, not by what the request
    # asked for. ... Anything on disk gets torn down.

That is right for retiring a whole environment — a stack whose kinds changed
since it was built would otherwise leave the old resource running, untracked and
still billing. It is catastrophic for removing ONE component of a stack, which is
how a decommission naming only apache destroyed the nginx machine too.

The portal now says which it is, because only the portal knows whether the
requester picked some components or all of them. These tests pin both halves:
narrowing must narrow, and sweeping must still sweep.
"""

import pytest

import orchestrator.main as omain


def _targets(payload: dict, built: set[str], monkeypatch) -> list[str]:
    """The kinds /destroy would tear down, without touching a cloud.

    Mirrors the selection logic in the endpoint; the endpoint itself needs a
    signed request and a real Terraform run, neither of which belongs in a unit
    test. The behavioural guard for the whole path is the API-side test.
    """
    monkeypatch.setattr(omain.provisioner, "existing_workspaces", lambda ref: built)
    kinds = omain._resource_kinds(payload)
    partial = bool(payload.get("partial_destroy"))
    if "" in built:
        return [omain._resource_kind(payload)]
    if partial:
        return [k for k in kinds if k in built]
    return [k for k in kinds if k in built] + [k for k in built if k not in kinds]


BOTH = {"oci-apache", "oci-service-vm"}


def test_a_partial_destroy_removes_only_what_it_names(monkeypatch):
    """THE bug: one component selected, both machines destroyed."""
    payload = {"resource_kinds": ["oci-apache"], "partial_destroy": True}
    assert _targets(payload, BOTH, monkeypatch) == ["oci-apache"]


def test_a_partial_destroy_leaves_the_other_workspace_alone(monkeypatch):
    payload = {"resource_kinds": ["oci-service-vm"], "partial_destroy": True}
    assert "oci-apache" not in _targets(payload, BOTH, monkeypatch)


def test_a_full_destroy_still_sweeps_untracked_workspaces(monkeypatch):
    """The behaviour the original comment defends, and it must survive: a kind no
    longer in the request but still on disk is a resource nothing is tracking and
    everything is billing."""
    payload = {"resource_kinds": ["oci-apache"], "partial_destroy": False}
    targets = _targets(payload, BOTH, monkeypatch)
    assert set(targets) == BOTH, "a full teardown must still remove the orphan"


def test_an_older_portal_that_says_nothing_gets_the_old_behaviour(monkeypatch):
    """`partial_destroy` absent means a portal that predates this. Treating that
    as partial would silently start leaving resources behind."""
    payload = {"resource_kinds": ["oci-apache"]}
    assert set(_targets(payload, BOTH, monkeypatch)) == BOTH


def test_a_legacy_flat_workspace_is_unaffected(monkeypatch):
    """One workspace, one resource, built before suffixed names existed."""
    payload = {"resource_kinds": ["oci-apache"], "resource_kind": "oci-instance",
               "partial_destroy": True}
    assert _targets(payload, {""}, monkeypatch) == ["oci-instance"]


def test_naming_a_kind_with_no_workspace_destroys_nothing(monkeypatch):
    """Asking to remove something already gone must be a no-op, not an error and
    certainly not a reason to remove something else."""
    payload = {"resource_kinds": ["oci-apache"], "partial_destroy": True}
    assert _targets(payload, {"oci-service-vm"}, monkeypatch) == []


def test_the_endpoint_reads_the_flag():
    """A structural check that the endpoint consults partial_destroy at all —
    the logic above is a mirror, and a mirror can drift from what it reflects."""
    import inspect
    source = inspect.getsource(omain.destroy)
    assert 'payload.get("partial_destroy")' in source
    assert "elif partial:" in source
