"""Increment 2.6a checks: the provisioner mode switch and safety guards.

Terraform itself is not invoked here — we check the mode default, the config
guard, and the plan-summary parsing. Real plan runs are exercised by the
reviewer against the OCI sandbox.
"""

import pytest

from orchestrator import provisioner


def test_default_mode_is_mock(monkeypatch):
    monkeypatch.delenv("PROVISION_MODE", raising=False)
    assert provisioner.provision_mode() == "mock"


def test_plan_requires_oci_config(monkeypatch):
    for var in ("OCI_TENANCY_OCID", "OCI_COMPARTMENT_OCID", "OCI_REGION"):
        monkeypatch.delenv(var, raising=False)
    # Guard fires before any filesystem work (no /tfstate needed).
    with pytest.raises(provisioner.ProvisionError):
        provisioner.terraform_plan("REQ-1", "some-bucket", {})


def test_per_request_workdir_is_isolated(tmp_path, monkeypatch):
    # Each reference gets its own directory, seeded with the module .tf files.
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    wd_a = provisioner._workdir("REQ-A")
    wd_b = provisioner._workdir("REQ-B")
    assert wd_a != wd_b
    assert (wd_a / "main.tf").exists()
    assert (wd_b / "variables.tf").exists()


def test_plan_summary_parsing():
    out = "Refreshing...\nPlan: 1 to add, 0 to change, 0 to destroy.\nDone."
    assert provisioner._plan_summary(out) == "Plan: 1 to add, 0 to change, 0 to destroy."
    assert provisioner._plan_summary("No changes. Your infrastructure matches.") == "No changes."
    assert provisioner._plan_summary("nothing recognisable") == "plan generated"
