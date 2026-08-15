"""First-boot configuration for VM-based technologies (GAP-ANALYSIS step 4).

Terraform makes a bare VM; this turns it into a working service via cloud-init,
because the VMs are private-only and cannot be reached over SSH.

HONEST LIMIT: these tests prove the rendering and the wiring. They do NOT prove a
VM boots and installs anything — that needs a real VM, which is still blocked on
the IAM grant. Package names are image-dependent. Treat first-boot configuration
as UNVERIFIED until a real machine has been booted and checked.
"""

import json

import pytest

import orchestrator.main as omain
from orchestrator import configure, provisioner

_COMPONENTS = [{"technology_code": "nginx", "size": "medium"},
               {"technology_code": "redis7", "size": "small"}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CONFIG_ENABLED", "CONFIG_OS_FAMILY", "CONFIG_PACKAGE_MAP",
              "OCI_COMPUTE_USER_DATA"):
        monkeypatch.delenv(k, raising=False)


def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")


# --- Off by default ----------------------------------------------------------

def test_disabled_by_default_renders_nothing():
    """Existing deployments must behave exactly as before until opted in."""
    assert configure.enabled() is False
    assert configure.render(_COMPONENTS) == ""


def test_enabled_but_no_matching_technology_renders_nothing(monkeypatch):
    _on(monkeypatch)
    assert configure.render([{"technology_code": "kafka", "size": "medium"}]) == ""
    assert configure.render([]) == ""


# --- Rendering ---------------------------------------------------------------

def test_renders_cloud_config_installing_and_enabling_services(monkeypatch):
    _on(monkeypatch)
    out = configure.render(_COMPONENTS)
    assert out.startswith("#cloud-config")
    assert "dnf install -y nginx redis" in out
    assert "systemctl enable --now nginx" in out
    assert "systemctl enable --now redis" in out
    # Names both technologies it configured, for diagnosis.
    assert "nginx" in out and "redis7" in out


def test_install_failure_does_not_abort_the_rest_of_boot(monkeypatch):
    """A failed install must leave a diagnosable machine, not a silent dead VM."""
    _on(monkeypatch)
    out = configure.render(_COMPONENTS)
    assert "|| echo 'PORTAL FAILURE: package install did not complete'" in out
    assert "/var/log/infra-portal.log" in out


def test_marker_file_does_not_overclaim(monkeypatch):
    _on(monkeypatch)
    out = configure.render(_COMPONENTS)
    assert "/etc/infra-portal-configured" in out
    # It is written BEFORE the install, so it must not imply success.
    assert "does not prove success" in out


def test_packages_are_deduplicated(monkeypatch):
    _on(monkeypatch)
    out = configure.render([{"technology_code": "nginx"}, {"technology_code": "nginx"}])
    assert out.count(" nginx") == 1 or out.count("install -y nginx") == 1


def test_os_family_switches_the_package_manager(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("CONFIG_OS_FAMILY", "debian")
    assert "apt-get install -y" in configure.render(_COMPONENTS)
    monkeypatch.setenv("CONFIG_OS_FAMILY", "nonsense")  # unknown -> safe default
    assert "dnf install -y" in configure.render(_COMPONENTS)


def test_package_names_can_be_overridden_without_code_change(monkeypatch):
    """Package names depend on the customer's image, so they must be data."""
    _on(monkeypatch)
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       json.dumps({"nginx": {"packages": ["nginx-core"], "services": ["nginx"]}}))
    out = configure.render([{"technology_code": "nginx"}])
    assert "nginx-core" in out


def test_malformed_override_is_ignored_not_fatal(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("CONFIG_PACKAGE_MAP", "{not json")
    assert "nginx" in configure.render([{"technology_code": "nginx"}])


def test_override_can_add_a_new_technology(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       json.dumps({"kafka": {"packages": ["kafka"], "services": ["kafka"]}}))
    assert "kafka" in configure.configurable_codes()
    assert "systemctl enable --now kafka" in configure.render([{"technology_code": "kafka"}])


# --- Wiring into provisioning ------------------------------------------------

def test_compute_spec_carries_the_rendered_user_data(monkeypatch):
    _on(monkeypatch)
    payload = {"policy_input": {"components": _COMPONENTS}}
    spec = omain._compute_spec(payload)
    assert spec["user_data"].startswith("#cloud-config")
    assert "ocpus" in spec and "image_ocid" in spec


def test_provisioner_prefers_rendered_user_data_over_the_env_default(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_USER_DATA", "#cloud-config\n# static default\n")
    v = provisioner._oci_vars("env", {}, "oci-instance", sizing={"user_data": "#cloud-config\n# rendered\n"})
    assert "# rendered" in v["user_data"]


def test_provisioner_falls_back_to_the_env_default(monkeypatch):
    """No rendered config (feature off) must not lose an operator's static value."""
    monkeypatch.setenv("OCI_COMPUTE_USER_DATA", "#cloud-config\n# static default\n")
    v = provisioner._oci_vars("env", {}, "oci-instance", sizing={"user_data": ""})
    assert "# static default" in v["user_data"]


# --- The honesty guard -------------------------------------------------------

def test_only_what_was_actually_booted_is_claimed_verified():
    """VERIFIED_CODES is a claim that someone booted a VM and asked the service
    whether it worked. The tripwire against claiming it without doing it."""
    # Exactly the two booted and checked on 11 Aug 2026 — see the evidence
    # recorded beside the set. Anything else appearing here means someone claimed
    # a technology works without booting one.
    assert configure.VERIFIED_CODES == {"nginx", "redis7"}
