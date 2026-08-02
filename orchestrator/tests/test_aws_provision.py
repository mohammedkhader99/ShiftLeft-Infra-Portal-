"""AWS provisioning path (multi-cloud): cloud dispatch + gated real apply.

Terraform itself isn't invoked here — we check the cloud routing, the AWS config
guard, and the AWS var shape. A real AWS apply needs AWS credentials + apply mode
(exercised by the reviewer against an AWS account, like OCI).
"""

import pytest

from orchestrator import provisioner


def test_cloud_of_maps_aws_and_oci():
    assert provisioner._cloud_of("aws-bucket") == "aws"
    assert provisioner._cloud_of("oci-bucket") == "oci"
    assert provisioner._cloud_of("oci-instance") == "oci"


def test_aws_module_is_its_own_subdir():
    assert provisioner._module_dir("aws").name == "aws"
    assert provisioner._module_dir("oci") == provisioner.MODULE_DIR


def test_require_aws_missing_config(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(provisioner.ProvisionError):
        provisioner._require_aws()


def test_require_aws_passes_when_set(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "me-central-1")
    provisioner._require_aws()  # no raise


def test_aws_vars_shape(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "me-central-1")
    monkeypatch.setenv("AWS_KMS_KEY_ARN", "arn:aws:kms:me-central-1:1:key/abc")
    v = provisioner._aws_vars("egate-uat-bucket", {"reference": "REQ-1"}, "aws-bucket")
    assert v["region"] == "me-central-1" and v["resource_kind"] == "aws-bucket"
    assert v["bucket_name"] == "egate-uat-bucket"
    assert v["kms_key_arn"].startswith("arn:aws:kms")


def test_aws_plan_refuses_without_creds(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    # The AWS guard fires before any terraform/filesystem work.
    with pytest.raises(provisioner.ProvisionError):
        provisioner.terraform_plan("REQ-AWS", "some-bucket", {}, "aws-bucket")


def test_aws_apply_refuses_without_apply_mode(monkeypatch):
    monkeypatch.setenv("PROVISION_MODE", "plan")  # not apply
    with pytest.raises(provisioner.ProvisionError):
        provisioner.terraform_apply("REQ-AWS", "some-bucket", {}, "aws-bucket")


def test_aws_workdir_seeds_the_aws_module(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    wd = provisioner._workdir("REQ-AWS", "aws")
    assert (wd / "main.tf").exists() and (wd / "variables.tf").exists()
    assert "aws_s3_bucket" in (wd / "main.tf").read_text()
