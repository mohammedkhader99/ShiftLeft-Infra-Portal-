## When user_ocid/fingerprint/private_key_path are left null (the default),
## the provider falls back to OCI Resource Manager's auto-injected resource
## principal auth. Set them (e.g. via terraform.tfvars) for local CLI use with
## an API signing key instead.
provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}
