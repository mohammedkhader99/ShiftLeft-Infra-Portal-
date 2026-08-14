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


# --- Excluding a variant ------------------------------------------------------
#
# Oracle publishes Oracle-Linux-9.8-aarch64-... and Oracle-Linux-9.8-Gen2-GPU-...
# beside the plain x86 Oracle-Linux-9.8-... . Offering an ARM image for an AMD
# shape fails at apply time, and an include-only filter cannot exclude them
# without naming the release date — which goes stale on the next publish.

_VARIANTS = [
    {"ocid": "ocid1.image..x86", "name": "Oracle-Linux-9.8-2026.07.20-0",
     "os": "Oracle Linux", "os_version": "9"},
    {"ocid": "ocid1.image..arm", "name": "Oracle-Linux-9.8-aarch64-2026.07.20-0",
     "os": "Oracle Linux", "os_version": "9"},
    {"ocid": "ocid1.image..gpu", "name": "Oracle-Linux-9.8-Gen2-GPU-2026.07.20-0",
     "os": "Oracle Linux", "os_version": "9"},
]


def test_a_leading_minus_excludes_a_variant(monkeypatch):
    monkeypatch.setattr(cloud_catalogue, "_MOCK_IMAGES", _VARIANTS)
    monkeypatch.setenv("OCI_IMAGE_FILTER", "Oracle-Linux-9,-aarch64,-GPU")
    names = [i["name"] for i in cloud_catalogue.fetch()["images"]]
    assert names == ["Oracle-Linux-9.8-2026.07.20-0"]


def test_without_the_exclusions_every_variant_is_offered(monkeypatch):
    """The other half — otherwise the test above would pass with a filter that
    simply matched one thing by luck."""
    monkeypatch.setattr(cloud_catalogue, "_MOCK_IMAGES", _VARIANTS)
    monkeypatch.setenv("OCI_IMAGE_FILTER", "Oracle-Linux-9")
    assert len(cloud_catalogue.fetch()["images"]) == 3


def test_exclusions_alone_do_not_open_the_gate(monkeypatch):
    """Fail-closed still applies: a filter with only exclusions has said nothing
    about what IS wanted, so it offers nothing rather than everything-but."""
    monkeypatch.setattr(cloud_catalogue, "_MOCK_IMAGES", _VARIANTS)
    monkeypatch.setenv("OCI_IMAGE_FILTER", "-aarch64,-GPU")
    assert cloud_catalogue.fetch()["images"] == []


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


@pytest.fixture()
def _single_page(monkeypatch):
    """Stand in for the SDK's paging helper.

    The `oci` package is installed in the orchestrator container, not in the test
    environment, so `_all_pages` cannot import it here. These tests are about
    what we do with the rows, not about paging — the dedicated paging test below
    asserts the helper is actually used.
    """
    monkeypatch.setattr(cloud_catalogue, "_all_pages",
                        lambda list_call, **kwargs: list_call(**kwargs).data)


class _FakeMemoryOptions:
    """The SHAPE of a real oci.core.models.ShapeMemoryOptions.

    Copied from a live tenancy listing on 14 Aug 2026. Note the two spellings on
    ONE object: `max_in_g_bs` but `max_per_ocpu_in_gbs`. That inconsistency is
    the whole reason this class exists.
    """
    min_in_g_bs = 1.0
    max_in_g_bs = 1760.0
    default_per_ocpu_in_g_bs = 16.0
    min_per_ocpu_in_gbs = 1.0
    max_per_ocpu_in_gbs = 64.0
    max_per_numa_node_in_gbs = 1024.0


class _FakeOcpuOptions:
    min = 1.0
    max = 114.0
    max_per_numa_node = 64.0


class _FakeShape:
    shape = "VM.Standard.E4.Flex"
    ocpu_options = _FakeOcpuOptions()
    memory_options = _FakeMemoryOptions()
    ocpus = 1.0
    memory_in_gbs = 16.0  # the DEFAULT PER OCPU — not a maximum


class _FakeClient:
    def list_shapes(self, compartment_id):
        return type("R", (), {"data": [_FakeShape()]})()


def test_the_real_sdk_memory_attribute_names_are_read(_single_page):
    """THE bug this file gained a fixture for.

    The OCI SDK spells it `max_in_g_bs`. Reading `max_in_gbs` returned None and
    fell through to `memory_in_gbs` — 16 — so a shape that goes to 1760 GB looked
    like it capped at 16, and the portal would have refused every request over
    16 GB as unbuildable. Mock data could not catch it: our mock used our own
    spelling, so it agreed with the bug.
    """
    shape = cloud_catalogue._live_shapes(_FakeClient(), "ocid1.compartment..x")[0]
    assert shape["max_memory_gb"] == 1760, "read the SDK's max_in_g_bs, not the default"
    assert shape["min_memory_gb"] == 1
    assert shape["max_ocpus"] == 114
    assert shape["max_memory_per_ocpu"] == 64, "the per-OCPU cap must be captured"


def test_a_fixed_shape_with_no_options_still_reports_its_size(_single_page):
    """A non-flex shape carries no ocpu_options/memory_options at all."""
    class Fixed:
        shape = "VM.Standard2.1"
        ocpu_options = None
        memory_options = None
        ocpus = 1.0
        memory_in_gbs = 15.0

    class Client:
        def list_shapes(self, compartment_id):
            return type("R", (), {"data": [Fixed()]})()

    shape = cloud_catalogue._live_shapes(Client(), "ocid1.compartment..x")[0]
    assert (shape["min_ocpus"], shape["max_ocpus"]) == (1, 1)
    assert (shape["min_memory_gb"], shape["max_memory_gb"]) == (15, 15)


def test_the_live_listings_page_through_every_result(monkeypatch):
    """The second bug found by listing a real tenancy.

    `list_images` returns 100 rows by default; this tenancy has 212. The first
    page came back entirely Windows, so an "Oracle-Linux-9" filter matched
    nothing and the portal offered NO images at all — and nothing anywhere
    reported an error, because a short list is not an error. A three-item mock
    can never surface a paging bug, so assert the paging helper is USED.
    """
    calls = []

    def fake_all_pages(list_call, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(cloud_catalogue, "_all_pages", fake_all_pages)

    class Client:
        """Both methods raise: if either listing is called DIRECTLY instead of
        through the paging helper, this test fails loudly rather than quietly
        reading page one."""
        def list_images(self, **kwargs):
            raise AssertionError("list_images was called directly — page 1 only")

        def list_shapes(self, **kwargs):
            raise AssertionError("list_shapes was called directly — page 1 only")

    client = Client()
    cloud_catalogue._live_images(client, "ocid1.compartment..x")
    cloud_catalogue._live_shapes(client, "ocid1.compartment..x")
    assert len(calls) == 2, "both listings must page; one of them reads only page 1"
    assert all(c["compartment_id"] == "ocid1.compartment..x" for c in calls)


def test_the_mock_shapes_carry_the_same_keys_as_a_live_one(_single_page):
    """Mock data that disagrees in SHAPE with live data is how the bug above
    survived a full test suite. Whatever _live_shapes produces, the mock must
    produce too."""
    live = set(cloud_catalogue._live_shapes(_FakeClient(), "x")[0])
    for mock in cloud_catalogue._MOCK_SHAPES:
        assert set(mock) == live, f"{mock['name']} does not match the live shape"


# --- It can only read ---------------------------------------------------------

def test_the_adapter_only_ever_lists():
    """A structural check, not a behavioural one: this module reaches the cloud,
    and nothing in it should be able to create, change or destroy. If a write
    call is ever added, this fails and the reviewer has to justify it.

    Matched as whole call names. An earlier version looked for the bare substring
    ".start" and flagged `.startswith(...)` — a guard that cries wolf gets
    weakened or deleted, so it has to be precise about what it forbids.
    """
    import inspect
    import re
    source = inspect.getsource(cloud_catalogue)
    forbidden = re.compile(
        r"\.(?:create|delete|terminate|update|launch|attach|detach|"
        r"instance_action|start_instance|stop_instance)\w*\s*\(")
    found = sorted({m.group(0).strip("(. ") for m in forbidden.finditer(source)})
    assert not found, f"the read-only catalogue adapter gained a write call: {found}"


def test_the_read_only_guard_would_actually_catch_a_write():
    """Proves the regex above matches what it claims to. A guard nobody has seen
    fail is a guard nobody knows works."""
    import re
    pattern = re.compile(
        r"\.(?:create|delete|terminate|update|launch|attach|detach|"
        r"instance_action|start_instance|stop_instance)\w*\s*\(")
    assert pattern.search("client.terminate_instance(instance_id=x)")
    assert pattern.search("client.launch_instance(details)")
    assert pattern.search("client.instance_action(id, 'STOP')")
    # ...and does not fire on ordinary string handling.
    assert not pattern.search('name.startswith("-")')
    assert not pattern.search('term.endswith("x")')
