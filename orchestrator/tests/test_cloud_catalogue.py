"""The read-only cloud catalogue adapter (shapes + OS images).

This is the only new code path that talks to OCI, so the tests are about two
things: that it cannot do anything but read, and that an unconfigured portal
offers NOTHING rather than everything. A tenancy carries bare-metal and GPU
shapes costing thousands a month; a dropdown that lists them because nobody set
a variable is a much worse failure than a dropdown that is empty.
"""

import pytest

from orchestrator import cloud_catalogue


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("OCI_CATALOGUE_MODE", "OCI_SHAPE_ALLOWLIST", "OCI_IMAGE_FILTER",
                "OCI_COMPARTMENT_OCID", "OCI_COMPUTE_COMPARTMENT_OCID"):
        monkeypatch.delenv(var, raising=False)


# --- Fails closed -------------------------------------------------------------

def test_nothing_is_offered_until_it_is_allow_listed():
    """THE guard. Unset means offer none, not offer all."""
    out = cloud_catalogue.fetch()
    assert out["shapes"] == []
    assert out["images"] == []
    # ...but it reports what it COULD have offered, so an admin can tell an empty
    # allowlist apart from an unreachable tenancy.
    assert out["shapes_available"] > 0
    assert out["allowlist_set"] is False


def test_an_allow_listed_shape_is_offered(monkeypatch):
    monkeypatch.setenv("OCI_SHAPE_ALLOWLIST", "VM.Standard.E4.Flex")
    out = cloud_catalogue.fetch()
    assert [s["name"] for s in out["shapes"]] == ["VM.Standard.E4.Flex"]
    assert out["allowlist_set"] is True


def test_a_shape_absent_from_the_allowlist_is_not_offered(monkeypatch):
    """The expensive ones stay out unless named."""
    monkeypatch.setenv("OCI_SHAPE_ALLOWLIST", "VM.Standard.E4.Flex")
    names = [s["name"] for s in cloud_catalogue.fetch()["shapes"]]
    assert "VM.Standard.A1.Flex" not in names
    assert "VM.Standard2.1" not in names


def test_images_are_filtered_by_name(monkeypatch):
    monkeypatch.setenv("OCI_IMAGE_FILTER", "Oracle-Linux-9")
    out = cloud_catalogue.fetch()
    assert [i["os_version"] for i in out["images"]] == ["9"]
    assert out["images_available"] > 1, "it filtered rather than found only one"


def test_the_image_filter_also_fails_closed():
    assert cloud_catalogue.fetch()["images"] == []


def test_several_filters_can_be_given(monkeypatch):
    monkeypatch.setenv("OCI_IMAGE_FILTER", "Oracle-Linux-9, Ubuntu")
    names = [i["name"] for i in cloud_catalogue.fetch()["images"]]
    assert len(names) == 2


# --- Shape data is usable -----------------------------------------------------

def test_a_shape_carries_the_limits_the_portal_validates_against(monkeypatch):
    monkeypatch.setenv("OCI_SHAPE_ALLOWLIST", "VM.Standard.E4.Flex")
    shape = cloud_catalogue.fetch()["shapes"][0]
    assert shape["min_ocpus"] >= 1
    assert shape["max_ocpus"] > shape["min_ocpus"]
    assert shape["max_memory_gb"] > shape["min_memory_gb"]


def test_a_shape_repeated_per_availability_domain_is_returned_once(monkeypatch):
    """list_shapes returns one row per availability domain, so the same shape
    arrives three times in a three-AD region."""
    monkeypatch.setenv("OCI_SHAPE_ALLOWLIST", "VM.Standard.E4.Flex")
    duplicated = cloud_catalogue._MOCK_SHAPES + cloud_catalogue._MOCK_SHAPES
    monkeypatch.setattr(cloud_catalogue, "_MOCK_SHAPES", duplicated)
    assert len(cloud_catalogue.fetch()["shapes"]) == 1


# --- Live mode ----------------------------------------------------------------

def test_live_mode_without_a_compartment_says_so(monkeypatch):
    """A clear message beats an SDK stack trace an infra lead has to decode."""
    monkeypatch.setenv("OCI_CATALOGUE_MODE", "live")
    with pytest.raises(cloud_catalogue.CloudCatalogueUnavailable) as exc:
        cloud_catalogue.fetch()
    assert "OCI_COMPARTMENT_OCID" in str(exc.value)


def test_a_failing_sdk_call_becomes_the_documented_exception(monkeypatch):
    """So the API's refresh treats it as 'keep the cache' rather than crashing."""
    monkeypatch.setenv("OCI_CATALOGUE_MODE", "live")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment..x")

    def boom():
        raise RuntimeError("401 NotAuthenticated")

    monkeypatch.setattr(cloud_catalogue, "_compute_client", boom)
    with pytest.raises(cloud_catalogue.CloudCatalogueUnavailable):
        cloud_catalogue.fetch()


def test_mock_is_the_default():
    assert cloud_catalogue.mode() == "mock"
    assert cloud_catalogue.is_live() is False


# --- It can only read ---------------------------------------------------------

def test_the_adapter_only_ever_lists():
    """A structural check, not a behavioural one: this module reaches the cloud,
    and nothing in it should be able to create, change or destroy. If a write
    call is ever added, this fails and the reviewer has to justify it."""
    import inspect
    source = inspect.getsource(cloud_catalogue)
    forbidden = ("create_", "delete_", "terminate_", "update_", "launch_",
                 "attach_", "detach_", ".stop", ".start")
    found = [w for w in forbidden if w in source]
    assert not found, f"the read-only catalogue adapter gained a write call: {found}"
