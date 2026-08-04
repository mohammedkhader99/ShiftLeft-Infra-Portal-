"""The generic "service on a VM" blueprint (F-CAT-10, GAP-ANALYSIS step 6).

Most of the catalogue is not a distinct cloud resource — it is a package on a
machine. Those technologies share one blueprint, and what differs between them
(package, systemd unit, port) is data in `configure.TEMPLATES`.

The tests that matter here are the ones guarding SILENT success, because that is
how this blueprint fails: a VM that builds fine with nothing installed on it, or
a service running correctly behind a port nobody opened. Both report success.
"""

import pytest

from orchestrator import blueprint_registry, configure, provisioner

MANIFEST_REF = "oci/service-vm"
KIND = "oci-service-vm"


def _manifest():
    return blueprint_registry.for_resource_kind(KIND)


def _oci_env(monkeypatch):
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.setenv("OCI_COMPUTE_SUBNET_OCID", "ocid1.subnet..x")


# --- The blueprint is wired up -----------------------------------------------

def test_the_blueprint_ships_and_dispatches_to_its_own_module():
    bp = _manifest()
    assert bp and bp["ref"] == MANIFEST_REF
    module = provisioner._module_dir("oci", KIND)
    assert module.name == "service-vm"
    assert (module / "main.tf").is_file()


def test_every_technology_it_claims_has_a_recipe():
    """The guard that keeps the catalogue honest: a blueprint listing a
    technology it has no profile for would advertise automation and then build a
    machine with nothing on it."""
    for code in _manifest()["builds"]:
        prof = configure.profile_for(code)
        assert prof, f"{code} is in the blueprint's builds but has no profile"
        assert prof["packages"], f"{code} has a profile that installs nothing"


# --- Silent success guard 1: an unconfigured build ---------------------------

def test_it_refuses_to_build_when_first_boot_configuration_is_off(monkeypatch):
    """With CONFIG_ENABLED off, render() returns "" and this blueprint would
    create a bare VM and report success. Refusing names the setting instead."""
    _oci_env(monkeypatch)
    monkeypatch.delenv("CONFIG_ENABLED", raising=False)
    with pytest.raises(provisioner.ProvisionError) as exc:
        provisioner._require_cloud("oci", KIND, creating=True)
    assert "CONFIG_ENABLED" in str(exc.value)

    monkeypatch.setenv("CONFIG_ENABLED", "true")
    provisioner._require_cloud("oci", KIND, creating=True)  # no longer refused


def test_destroying_is_never_blocked_by_the_configuration_gate(monkeypatch):
    """Turning the switch off after provisioning must not trap the resource."""
    _oci_env(monkeypatch)
    monkeypatch.delenv("CONFIG_ENABLED", raising=False)
    provisioner._require_cloud("oci", KIND, creating=False)


# --- Silent success guard 2: a service behind a closed port ------------------

def test_the_ports_opened_are_the_ports_declared(monkeypatch):
    """One declaration drives both the network rules and the OS firewall. If they
    could drift, a correctly installed service would simply be unreachable."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    components = [{"technology_code": "nginx"}]

    declared = configure.ports_for(components)
    assert declared == [80]

    user_data = configure.render(components)
    assert "--add-port=80/tcp" in user_data
    assert "firewall-cmd --reload" in user_data

    # ...and the same list reaches Terraform.
    _oci_env(monkeypatch)
    v = provisioner._cloud_vars("oci", "web", {"env": "uat"}, KIND,
                                {"user_data": user_data, "service_ports": declared})
    assert v["service_ports"] == [80]
    assert v["instance_name"] == "web"
    assert v["user_data"] == user_data


def test_a_technology_with_no_declared_port_opens_none(monkeypatch):
    """Redis has no port on purpose: it ships without authentication, so opening
    6379 to the subnet would publish an unauthenticated data store. Installed and
    running locally is the intended outcome, not a half-finished one."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    components = [{"technology_code": "redis7"}]
    assert configure.ports_for(components) == []
    user_data = configure.render(components)
    assert "--add-port" not in user_data
    # ...but it is still genuinely installed and started.
    assert "redis" in user_data
    assert "systemctl enable --now redis" in user_data


def test_ports_from_several_technologies_are_merged_without_duplicates(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    assert configure.ports_for([{"technology_code": "nginx"},
                                {"technology_code": "apache"},
                                {"technology_code": "redis7"}]) == [80]


def test_an_admin_can_correct_a_port_without_a_code_change(monkeypatch):
    """Package and port names are image-dependent; CONFIG_PACKAGE_MAP already
    overrides the former, and must carry the latter with it."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       '{"nginx": {"packages": ["nginx"], "services": ["nginx"], "ports": [8080]}}')
    assert configure.ports_for([{"technology_code": "nginx"}]) == [8080]


# --- Nothing is claimed as proven --------------------------------------------

def test_no_technology_claims_to_be_verified_without_a_real_boot():
    """VERIFIED_CODES is a claim that a human booted the VM and checked the
    service answered. Generating a blueprint does not earn an entry in it."""
    assert configure.VERIFIED_CODES == set(), (
        "a code was marked verified — that claim needs a real boot test behind it")
