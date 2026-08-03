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
