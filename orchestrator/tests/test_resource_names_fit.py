"""Composed resource names must fit the module that will receive them.

Found in use, after approval: REQ-2026-0128 named a VM
`test121-req-2026-0128-service-vm` — 32 characters against the module's limit of
31 — and Terraform refused it at PLAN time. The requester had already had a Jira
ticket approved for something that could never be built.

The arithmetic was brutal: the reference alone costs 14 characters and the
`-service-vm` suffix another 11, leaving SIX for an environment name. `test4`
worked; `test121` did not. Almost no real environment name would have.

Two rules carry the fix, and the second matters more than the first:

  1. a name that already fits is returned UNCHANGED, because renaming a live
     compute instance changes its hostname and Terraform implements that by
     destroying and rebuilding the machine; and
  2. an over-long name is compressed deterministically, trimming the environment
     name and never the reference — the reference is what makes it unique.
"""

import pytest

import orchestrator.main as omain
from orchestrator import blueprint_registry

KINDS = ("oci-service-vm", "oci-apache", "oci-kafka", "oci-oke")


def _name(env: str, kind: str, reference: str = "REQ-2026-0128") -> str:
    return omain._resource_name(f"{env}-{reference.lower()}", kind, reference,
                                "oci-service-vm")


# --- Every blueprint's own limit is respected --------------------------------

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("env", [
    "a", "test4", "test121", "egate-uat", "vision-qmg", "visa-preprod-eu",
    "a-very-long-environment-name-indeed",
])
def test_the_composed_name_fits_the_module(kind, env):
    limit = blueprint_registry.name_limit_for_kind(kind)
    assert limit, f"{kind} declares no name_max_length"
    name = _name(env, kind)
    assert len(name) <= limit, f"{name!r} is {len(name)} chars, limit {limit}"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("env", ["test121", "vision-qmg", "a-very-long-name-here"])
def test_the_name_still_satisfies_the_modules_own_pattern(kind, env):
    """The modules require ^[a-zA-Z][a-zA-Z0-9-]*$. Trimming must not leave a
    trailing hyphen or start the name with one."""
    import re
    name = _name(env, kind)
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", name), name
    assert not name.endswith("-"), name


# --- A name that fits is never touched ---------------------------------------

def test_a_name_that_already_fits_is_returned_unchanged():
    """THE rule that protects running machines. Every existing resource has a
    name its module accepted, so it must take this path and keep it — a rename
    forces a destroy-and-rebuild of a live VM."""
    assert _name("test4", "oci-service-vm") == "test4-req-2026-0128-service-vm"
    assert _name("test4", "oci-apache") == "test4-req-2026-0128-apache"


def test_only_the_over_long_names_change():
    """test4 fits and is untouched; test121 is one character over and is not."""
    assert "req-2026-0128" in _name("test4", "oci-service-vm")
    assert "req-2026-0128" not in _name("test121", "oci-service-vm")


# --- Compression keeps what makes the name unique ----------------------------

def test_the_reference_survives_compression():
    """The environment name is trimmed; the reference is not. Two requests for
    the same environment must never produce the same resource name."""
    a = _name("a-very-long-environment-name", "oci-service-vm", "REQ-2026-0128")
    b = _name("a-very-long-environment-name", "oci-service-vm", "REQ-2026-0129")
    assert a != b
    assert "0128" in a and "0129" in b


def test_two_environments_in_one_request_stay_distinct():
    for kind_a, kind_b in [("oci-service-vm", "oci-apache")]:
        assert _name("a-very-long-environment-name", kind_a) != \
               _name("a-very-long-environment-name", kind_b)


def test_the_same_input_always_gives_the_same_name():
    """Terraform reads a changed name as a changed resource. A composition that
    varied between runs would propose replacing a machine on every plan."""
    assert _name("vision-qmg", "oci-service-vm") == _name("vision-qmg", "oci-service-vm")


def test_the_shortened_reference_keeps_year_and_sequence():
    assert omain._short_reference("REQ-2026-0128") == "26-0128"
    assert omain._short_reference("REQ-2027-0001") == "27-0001"
    # An unfamiliar shape is passed through rather than mangled.
    assert omain._short_reference("ADHOC") == "adhoc"


# --- The specific failure that started this ----------------------------------

def test_the_request_that_failed_would_now_succeed():
    name = _name("test121", "oci-service-vm")
    assert name == "test121-26-0128-service-vm"
    assert len(name) <= blueprint_registry.name_limit_for_kind("oci-service-vm")


def test_a_legacy_flat_workspace_still_keeps_its_bare_name(monkeypatch):
    """A resource built before suffixed names keeps the unsuffixed one, which is
    the whole reason _resource_name consults the workspace at all."""
    monkeypatch.setattr(omain.provisioner, "existing_workspaces", lambda ref: {""})
    assert omain._resource_name("test121-req-2026-0128", "oci-service-vm",
                                "REQ-2026-0128", "oci-service-vm") == \
        "test121-req-2026-0128"
