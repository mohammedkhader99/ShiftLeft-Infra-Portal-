"""Blueprint discovery — the directory is the registry (F-CAT-10).

Adding a build recipe should be adding a manifest file, not editing code in
several places. These tests pin that, plus the two failure behaviours that
matter: a broken manifest must not hide the working ones, and discovery must
never fail closed (which the portal would read as 'no automation available').
"""

import textwrap

import pytest

from orchestrator import blueprint_registry


def _write(tmp_path, name, body):
    (tmp_path / name).write_text(textwrap.dedent(body), encoding="utf-8")


# --- The shipped manifests ---------------------------------------------------

def test_the_real_directory_describes_the_shipped_recipes():
    """Asserts the PROPERTIES of what ships, not an exact list — pinning the exact
    set would break on every blueprint added, which is the opposite of the point.
    """
    found = {b["ref"]: b for b in blueprint_registry.discover()}
    # The recipes the execution layer genuinely has must all be present.
    assert {"oci/compute-instance", "oci/object-storage", "oci/postgres",
            "aws/s3", "oci/apache-httpd"} <= set(found)
    assert set(found["oci/compute-instance"]["builds"]) == {"compute-vm", "rhel9", "win2019"}
    assert found["oci/postgres"]["resource_kind"] == "oci-postgres"
    assert found["aws/s3"]["target"] == "aws"
    assert found["oci/apache-httpd"]["builds"] == ["apache"]
    # Every shipped manifest must carry the fields the portal relies on.
    for ref, bp in found.items():
        assert bp["version"], f"{ref} has no version for an admin to pin"
        assert bp["target"] and bp["resource_kind"], f"{ref} is incomplete"
        assert bp["builds"], f"{ref} builds nothing"


def test_a_blueprint_module_directory_that_is_named_actually_exists():
    """A manifest pointing at a module that isn't there would certify a recipe
    that cannot run."""
    from pathlib import Path
    tf_root = Path(blueprint_registry.BLUEPRINT_DIR).parent / "terraform"
    for bp in blueprint_registry.discover():
        module = (tf_root / bp["module"]).resolve()
        assert module.is_dir(), f"{bp['ref']} names module '{bp['module']}' which does not exist"
        assert list(module.glob("*.tf")), f"{bp['ref']} module '{bp['module']}' has no .tf files"


def test_no_manifest_is_malformed():
    """A shipped manifest that fails to parse would silently remove a capability."""
    assert not [b for b in blueprint_registry.discover() if b.get("error")]


def test_lookup_by_resource_kind():
    assert blueprint_registry.for_resource_kind("oci-postgres")["ref"] == "oci/postgres"
    assert blueprint_registry.for_resource_kind("nonexistent") is None


# --- Adding a blueprint is adding a file -------------------------------------

def test_a_new_manifest_is_discovered_without_touching_code(tmp_path):
    _write(tmp_path, "oci-kafka.yaml", """
        ref: oci/kafka-strimzi
        target: oci
        resource_kind: oci-kafka
        module: oci-kafka
        version: 1.2.0
        builds: [kafka]
        description: Kafka via the Strimzi operator.
    """)
    found = blueprint_registry.discover(tmp_path)
    assert len(found) == 1
    assert found[0]["ref"] == "oci/kafka-strimzi"
    assert found[0]["builds"] == ["kafka"]
    assert found[0]["version"] == "1.2.0"


def test_manifests_declare_their_own_preconditions(tmp_path, monkeypatch):
    """So the portal can say 'certified but not configured' instead of the
    request failing at apply time."""
    _write(tmp_path, "gated.yaml", """
        ref: oci/thing
        target: oci
        resource_kind: oci-thing
        version: 1.0.0
        builds: [thing]
        enable_flag: THING_ENABLED
        requires_env: [THING_SUBNET]
    """)
    monkeypatch.delenv("THING_ENABLED", raising=False)
    monkeypatch.delenv("THING_SUBNET", raising=False)
    bp = blueprint_registry.discover(tmp_path)[0]
    assert bp["ready"] is False
    assert set(bp["missing_config"]) == {"THING_ENABLED", "THING_SUBNET"}

    monkeypatch.setenv("THING_ENABLED", "true")
    monkeypatch.setenv("THING_SUBNET", "ocid1.subnet...")
    bp = blueprint_registry.discover(tmp_path)[0]
    assert bp["ready"] is True and bp["missing_config"] == []


# --- Failure behaviour -------------------------------------------------------

def test_a_broken_manifest_does_not_hide_the_good_ones(tmp_path):
    _write(tmp_path, "good.yaml", """
        ref: oci/good
        target: oci
        resource_kind: oci-good
        version: 1.0.0
        builds: [good]
    """)
    (tmp_path / "broken.yaml").write_text("ref: [unclosed\n", encoding="utf-8")
    found = blueprint_registry.discover(tmp_path)
    refs = {b["ref"] for b in found}
    assert "oci/good" in refs                       # the working one still surfaces
    assert any(b.get("error") for b in found)       # and the broken one is reported


def test_an_incomplete_manifest_is_reported_not_silently_used(tmp_path):
    _write(tmp_path, "partial.yaml", """
        ref: oci/partial
        target: oci
    """)
    bp = blueprint_registry.discover(tmp_path)[0]
    assert bp.get("error") and "missing" in bp["error"]
    assert bp["builds"] == []                       # cannot be certified for anything


def test_a_missing_directory_returns_empty_rather_than_raising(tmp_path):
    """Discovery must never fail closed — the portal would read an exception as
    'the orchestrator ships nothing'."""
    assert blueprint_registry.discover(tmp_path / "does-not-exist") == []


# --- Dispatch: the manifest decides which module runs ------------------------

def test_a_blueprint_with_its_own_directory_is_dispatched_to_it():
    """The point of pluggable blueprints: adding a recipe is adding a folder and
    a manifest, not editing dispatch code."""
    from orchestrator import provisioner
    module = provisioner._module_dir("oci", "oci-apache")
    assert module.name == "apache-httpd"
    assert (module / "main.tf").is_file()


def test_legacy_kinds_still_use_the_shared_module():
    """Existing provisioning must be untouched by the new dispatch."""
    from orchestrator import provisioner
    assert provisioner._module_dir("oci", "oci-bucket") == provisioner.MODULE_DIR
    assert provisioner._module_dir("oci", "oci-instance") == provisioner.MODULE_DIR
    assert provisioner._module_dir("oci", "oci-postgres") == provisioner.MODULE_DIR
    assert provisioner._module_dir("aws", "aws-bucket") == provisioner.MODULE_DIR / "aws"
    # An unknown kind falls back rather than failing.
    assert provisioner._module_dir("oci", "no-such-kind") == provisioner.MODULE_DIR


def test_manifest_vars_are_merged_over_the_standard_set(monkeypatch):
    """A recipe's own inputs come from its manifest, so the provisioner needs to
    know nothing about them."""
    from orchestrator import provisioner
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    v = provisioner._cloud_vars("oci", "web", {"env": "uat"}, "oci-apache", {"ocpus": 2})
    assert v["create_nsg"] is False          # from the manifest
    assert v["compartment_ocid"] == "c"      # standard set still present


# --- Naming: the manifest says which variable carries the name ---------------

def _oci_env(monkeypatch):
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")


def test_a_blueprint_names_its_resource_from_the_manifest(monkeypatch):
    """REQ-2026-0092: Apache planned with instance_name = "" and failed its own
    name validation, because the name was hard-coded to the compute kind. Which
    variable holds the name is the module's business, so the manifest says."""
    _oci_env(monkeypatch)
    v = provisioner_vars(monkeypatch, "oci", "web-uat", "oci-apache")
    assert v["instance_name"] == "web-uat"


def test_every_shipped_blueprint_names_its_resource(monkeypatch):
    """The guard: a new manifest without name_var would repeat REQ-2026-0092 —
    a plan that fails on an empty name, only at apply time."""
    _oci_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "me-south-1")
    for bp in blueprint_registry.discover():
        assert bp["name_var"], f"{bp['ref']} does not say which variable carries the name"
        v = provisioner_vars(monkeypatch, bp["target"], "env-name", bp["resource_kind"])
        assert v[bp["name_var"]] == "env-name", f"{bp['ref']} did not receive the name"


def provisioner_vars(monkeypatch, cloud, name, kind, sizing=None):
    from orchestrator import provisioner
    return provisioner._cloud_vars(cloud, name, {"env": "uat"}, kind, sizing or {})


# --- Images: whose choice wins ------------------------------------------------

def test_a_blueprint_that_resolves_its_own_image_ignores_the_shared_default(monkeypatch):
    """Apache's first-boot script is yum/systemd/firewalld, so it needs a Red Hat
    family image and looks one up itself. Handing it the shared default (a custom
    Ubuntu image) would build a VM with no web server on it — a success that
    isn't one."""
    _oci_env(monkeypatch)
    monkeypatch.setenv("OCI_COMPUTE_IMAGE_OCID", "ocid1.image..ubuntu-custom")
    assert provisioner_vars(monkeypatch, "oci", "web", "oci-apache")["image_ocid"] == ""
    # ...while the shared compute module still gets it.
    assert provisioner_vars(
        monkeypatch, "oci", "vm", "oci-instance")["image_ocid"] == "ocid1.image..ubuntu-custom"


def test_a_deliberate_per_technology_image_beats_the_blueprint_default(monkeypatch):
    """An admin who maps an image for apache in OCI_COMPUTE_IMAGE_MAP means it."""
    _oci_env(monkeypatch)
    monkeypatch.setenv("OCI_COMPUTE_IMAGE_OCID", "ocid1.image..ubuntu-custom")
    v = provisioner_vars(monkeypatch, "oci", "web", "oci-apache",
                         {"image_ocid_explicit": "ocid1.image..chosen"})
    assert v["image_ocid"] == "ocid1.image..chosen"


def test_a_dedicated_module_is_copied_whole_including_templates(tmp_path, monkeypatch):
    """A module using templatefile() would otherwise arrive without the file it
    renders — a failure that only appears at apply time."""
    from orchestrator import provisioner
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    workdir = provisioner._workdir("REQ-TEST", "oci", "oci-apache")
    assert (workdir / "main.tf").is_file()
    assert (workdir / "templates").is_dir()
    assert list((workdir / "templates").glob("*.tftpl"))


def test_the_legacy_workdir_does_not_drag_in_blueprint_subfolders(tmp_path, monkeypatch):
    """The shared module's directory now contains per-blueprint folders; copying
    them into every workspace would be wrong and slow."""
    from orchestrator import provisioner
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    workdir = provisioner._workdir("REQ-LEGACY", "oci", "oci-bucket")
    assert (workdir / "main.tf").is_file()
    assert not (workdir / "oci").exists()
    assert not (workdir / "aws").exists()


def test_a_blueprint_that_is_not_configured_refuses_with_the_missing_setting(monkeypatch):
    """Better than reaching Terraform and failing with a provider error."""
    from orchestrator import provisioner
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.delenv("OCI_COMPUTE_SUBNET_OCID", raising=False)
    monkeypatch.delenv("OCI_COMPUTE_IMAGE_OCID", raising=False)
    with pytest.raises(provisioner.ProvisionError) as exc:
        provisioner._require_cloud("oci", "oci-apache", creating=True)
    assert "OCI_COMPUTE_SUBNET_OCID" in str(exc.value)
    # ...and destroying is never blocked by a spend/config gate.
    provisioner._require_cloud("oci", "oci-apache", creating=False)
