"""Managed PostgreSQL provisioning (GAP-ANALYSIS.md step 2).

The first catalogue technology delivered as the thing actually requested rather
than a placeholder bucket. Because this deployment can run autonomously and a
managed DB system is billable, provisioning is DOUBLE-gated: an explicit
OCI_PSQL_ENABLED opt-in *and* the infrastructure inputs. These tests pin that
gate, the tfvars the module receives, and — critically — that the admin password
is never passed as a value. No real Terraform or cloud is touched.
"""

import pytest

from orchestrator import provisioner

_PSQL_ENV = ("OCI_PSQL_ENABLED", "OCI_PSQL_SUBNET_OCID", "OCI_PSQL_ADMIN_SECRET_OCID",
             "OCI_PSQL_COMPARTMENT_OCID", "OCI_PSQL_SHAPE", "OCI_PSQL_VERSION",
             "OCI_PSQL_ADMIN_USERNAME", "OCI_PSQL_ADMIN_SECRET_VERSION",
             "OCI_PSQL_STORAGE_IOPS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _PSQL_ENV:
        monkeypatch.delenv(k, raising=False)


def _configure(monkeypatch, enabled=True):
    if enabled:
        monkeypatch.setenv("OCI_PSQL_ENABLED", "true")
    monkeypatch.setenv("OCI_PSQL_SUBNET_OCID", "ocid1.subnet.oc1..db")
    monkeypatch.setenv("OCI_PSQL_ADMIN_SECRET_OCID", "ocid1.vaultsecret.oc1..s")


# --- The double gate ---------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    """Nothing is billable by accident: absent the opt-in, a real apply refuses."""
    _configure(monkeypatch, enabled=False)
    assert provisioner.psql_enabled() is False
    with pytest.raises(provisioner.ProvisionError, match="disabled"):
        provisioner._require_psql()


def test_enabled_but_unconfigured_refuses(monkeypatch):
    monkeypatch.setenv("OCI_PSQL_ENABLED", "true")
    with pytest.raises(provisioner.ProvisionError, match="not configured") as exc:
        provisioner._require_psql()
    # The message names both missing inputs so the operator knows what to supply.
    assert "OCI_PSQL_SUBNET_OCID" in str(exc.value)
    assert "OCI_PSQL_ADMIN_SECRET_OCID" in str(exc.value)


def test_missing_only_the_secret_still_refuses(monkeypatch):
    monkeypatch.setenv("OCI_PSQL_ENABLED", "true")
    monkeypatch.setenv("OCI_PSQL_SUBNET_OCID", "ocid1.subnet.oc1..db")
    with pytest.raises(provisioner.ProvisionError, match="OCI_PSQL_ADMIN_SECRET_OCID"):
        provisioner._require_psql()


def test_fully_configured_passes(monkeypatch):
    _configure(monkeypatch)
    provisioner._require_psql()  # does not raise


def test_spend_gate_is_create_only_so_teardown_is_never_blocked(monkeypatch):
    """The opt-in switch exists to stop something billable being CREATED. If it
    also blocked destroy, turning it off after provisioning — the natural thing to
    do — would strand an expensive database: removable only by hand in the cloud
    console, outside the audit trail. Destroying only ever stops cost."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment.oc1..c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    # Switch OFF (and unconfigured) — the state after someone disables it again.
    assert provisioner.psql_enabled() is False

    # Creating is still blocked: the protection is intact.
    with pytest.raises(provisioner.ProvisionError, match="disabled"):
        provisioner._require_cloud("oci", "oci-postgres", creating=True)

    # Destroying and drift-checking are allowed: the safe directions.
    provisioner._require_cloud("oci", "oci-postgres", creating=False)


def test_create_only_gate_still_requires_credentials(monkeypatch):
    """Skipping the spend gate must not skip the credential check — you cannot
    destroy a cloud resource without being able to talk to the cloud."""
    for k in ("OCI_TENANCY_OCID", "OCI_COMPARTMENT_OCID", "OCI_REGION"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(provisioner.ProvisionError, match="OCI not configured"):
        provisioner._require_cloud("oci", "oci-postgres", creating=False)


def test_compute_gate_is_create_only_too(monkeypatch):
    """Same trap, same fix: a VM must be destroyable after the image/subnet
    settings that created it have been removed."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment.oc1..c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    for k in ("OCI_COMPUTE_SUBNET_OCID", "OCI_COMPUTE_IMAGE_OCID", "OCI_COMPUTE_IMAGE_MAP"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(provisioner.ProvisionError, match="not configured"):
        provisioner._require_cloud("oci", "oci-instance", creating=True)
    provisioner._require_cloud("oci", "oci-instance", creating=False)


def test_gate_is_wired_into_require_cloud(monkeypatch):
    """The routing must actually reach the Postgres gate for oci-postgres."""
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment.oc1..c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    with pytest.raises(provisioner.ProvisionError, match="disabled"):
        provisioner._require_cloud("oci", "oci-postgres")
    # A bucket on the same cloud is unaffected by the Postgres gate.
    provisioner._require_cloud("oci", "oci-bucket")


# --- tfvars the module receives ----------------------------------------------

def test_vars_reference_the_secret_by_ocid_never_a_password(monkeypatch):
    """The admin password must never appear as a Terraform value — only the vault
    secret OCID — so it can't land in the plan file or state."""
    _configure(monkeypatch)
    v = provisioner._oci_vars("egate-db", {"env": "uat"}, "oci-postgres")
    assert v["db_admin_secret_ocid"] == "ocid1.vaultsecret.oc1..s"
    joined = " ".join(str(x).lower() for x in v.keys())
    assert "password" not in joined  # no password-carrying variable exists at all


def test_vars_carry_the_db_identity_and_defaults(monkeypatch):
    _configure(monkeypatch)
    v = provisioner._oci_vars("egate-db", {}, "oci-postgres")
    assert v["resource_kind"] == "oci-postgres"
    assert v["db_name"] == "egate-db"
    # NO INVENTED VERSION. This asserted "14" — a hard-coded default that was
    # wrong: the catalogue sells postgres16, so a postgres16 request built
    # PostgreSQL 14. With nothing named and OCI unreachable the answer is "I was
    # not told", not a guess. The module fails loudly on an empty version.
    assert v["db_version"] == ""
    assert v["db_instance_count"] == 1
    assert v["db_admin_username"] == "pgadmin"
    assert v["db_subnet_ocid"] == "ocid1.subnet.oc1..db"
    # Other kinds' name fields stay empty so their resources aren't created.
    assert v["bucket_name"] == "" and v["instance_name"] == ""


def test_shape_and_version_are_configurable(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("OCI_PSQL_SHAPE", "PostgreSQL.VM.Standard.E4.Flex.4.64GB")
    monkeypatch.setenv("OCI_PSQL_VERSION", "15")
    v = provisioner._oci_vars("db", {}, "oci-postgres")
    assert v["db_shape"].endswith("4.64GB") and v["db_version"] == "15"


def test_sizing_can_override_the_shape(monkeypatch):
    _configure(monkeypatch)
    v = provisioner._oci_vars("db", {}, "oci-postgres",
                              sizing={"db_shape": "PostgreSQL.VM.Standard.E4.Flex.8.128GB"})
    assert v["db_shape"].endswith("8.128GB")


def test_bucket_vars_unaffected_by_postgres_config(monkeypatch):
    _configure(monkeypatch)
    v = provisioner._oci_vars("egate-bucket", {}, "oci-bucket")
    assert v["bucket_name"] == "egate-bucket" and v["db_name"] == ""


def test_the_catalogue_name_decides_the_version(monkeypatch):
    """postgres16 must build PostgreSQL 16.

    The requester chose a catalogue entry called postgres16 and Jira approved
    that name. An environment default that quietly built 14 delivered something
    other than what was approved.
    """
    _configure(monkeypatch)
    v = provisioner._oci_vars("db", {}, "oci-postgres",
                              sizing={"build": "postgres16"})
    assert v["db_version"] == "16", v["db_version"]


def test_a_size_maps_to_a_shape_that_exists(monkeypatch):
    """The portal's sizes must resolve to real published shapes.

    "small" in the portal is 2 vCPU / 4 GB. No managed PostgreSQL is that small
    — the service floor is 16 GB — so the size has to be mapped, not passed
    through. Driven against a stub so the test states the contract without
    needing OCI.
    """
    from orchestrator import postgres_shapes

    class _Stub:
        def list_shapes(self, compartment_id=None):
            items = [type("S", (), {"id": i}) for i in (
                "PostgreSQL.VM.Standard.E5.Flex.2.32GB",
                "PostgreSQL.VM.Standard.E5.Flex.4.64GB",
                "PostgreSQL.VM.Standard.E5.Flex.16.256GB")]
            return type("D", (), {"data": type("R", (), {"items": items})})

        def list_default_configurations(self):
            items = [type("C", (), {"db_version": v}) for v in ("15", "16")]
            return type("D", (), {"data": type("R", (), {"items": items})})

    stub = _Stub()
    shape, why = postgres_shapes.resolve_shape("small", stub)
    assert shape == "PostgreSQL.VM.Standard.E5.Flex.2.32GB", shape
    assert "32 GB" in why

    # And a size nothing satisfies must be refused, never rounded down.
    postgres_shapes.SIZE_FLOORS["enormous"] = (999, 999)
    try:
        shape, why = postgres_shapes.resolve_shape("enormous", stub)
        assert shape == ""
        assert "publishes no PostgreSQL shape" in why
    finally:
        postgres_shapes.SIZE_FLOORS.pop("enormous", None)


def test_a_version_oci_does_not_offer_is_refused_not_swapped():
    """Substitution is the defect this whole module exists to prevent."""
    from orchestrator import postgres_shapes

    class _Stub:
        def list_shapes(self, compartment_id=None):
            items = [type("S", (), {"id": "PostgreSQL.VM.Standard.E5.Flex.2.32GB"})]
            return type("D", (), {"data": type("R", (), {"items": items})})

        def list_default_configurations(self):
            items = [type("C", (), {"db_version": v}) for v in ("15", "16")]
            return type("D", (), {"data": type("R", (), {"items": items})})

    version, why = postgres_shapes.resolve_version("14", _Stub())
    assert version == ""
    assert "does not offer PostgreSQL 14" in why
    assert "15, 16" in why


def test_the_shape_is_chosen_from_the_published_list_never_assembled():
    """REQ-2026-0155 shipped `.1.4GB` to OCI and was rejected.

    The old code built the id with an f-string: family + ocpus + memory. String
    building cannot fail, so an empty family and a below-floor size still
    produced a plausible name — and OCI rejected every one. A shape must be a
    member of the list OCI publishes, or nothing.
    """
    from orchestrator import postgres_shapes

    published = ["PostgreSQL.VM.Standard.E5.Flex.2.32GB",
                 "PostgreSQL.VM.Standard.E5.Flex.4.64GB"]

    class _Stub:
        def list_shapes(self, compartment_id=None):
            items = [type("S", (), {"id": i}) for i in published]
            return type("D", (), {"data": type("R", (), {"items": items})})

        def list_default_configurations(self):
            items = [type("C", (), {"db_version": "16"})]
            return type("D", (), {"data": type("R", (), {"items": items})})

    stub = _Stub()
    # The portal's "small" — 1 OCPU / 4 GB — is below every published shape.
    # It must round UP to a real one, not fabricate a matching name.
    shape, why = postgres_shapes.shape_for_spec(1, 4, stub)
    assert shape in published, shape
    assert shape == "PostgreSQL.VM.Standard.E5.Flex.2.32GB"
    assert "rounded up" in why

    # A size larger than anything published is refused, not truncated.
    shape, why = postgres_shapes.shape_for_spec(999, 9999, stub)
    assert shape == ""
    assert "no PostgreSQL shape" in why


def test_psql_shape_never_returns_a_fabricated_name(monkeypatch):
    """The orchestrator's helper must not concatenate, whatever the env says.

    Emptying OCI_PSQL_SHAPE_FAMILY used to yield ".1.4GB" — the regression that
    made REQ-2026-0155's failure so confusing.
    """
    from orchestrator import main as orch_main

    monkeypatch.setenv("OCI_PSQL_SHAPE_FAMILY", "")
    monkeypatch.delenv("OCI_PSQL_SHAPE", raising=False)
    shape = orch_main._psql_shape({"ocpus": 1, "memory_gb": 4})
    assert not shape.startswith("."), shape
    assert "GB" not in shape or shape.startswith("PostgreSQL."), shape


def test_psql_version_follows_the_catalogue_name(monkeypatch):
    """postgres16 must mean 16, whatever OCI_PSQL_VERSION says."""
    from orchestrator import main as orch_main

    monkeypatch.setenv("OCI_PSQL_VERSION", "14")
    assert orch_main._psql_version([{"technology": "postgres16"}]) == "16"
    # Nothing PostgreSQL in the request: fall back rather than invent.
    assert orch_main._psql_version([{"technology": "nginx"}]) == "14"
