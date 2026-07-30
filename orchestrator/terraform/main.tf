# Smallest, safest "first real resource": one OCI Object Storage bucket in the
# sandbox compartment. Free/near-free and trivially reversible. Increment 2.6a
# only ever runs `terraform plan` against this — nothing is created.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
}

provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

# Read-only lookup of the Object Storage namespace (no resource created).
data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.compartment_ocid
}

resource "oci_objectstorage_bucket" "env" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.ns.namespace
  name           = var.bucket_name
  freeform_tags  = var.tags

  # Data protection: retain prior object versions (F-SEC-04 hardening) — clears
  # the scanner's versioning-disabled finding and allows recovery from overwrite.
  versioning = "Enabled"

  # Customer-managed encryption key when one is configured (OCI_KMS_KEY_OCID),
  # otherwise Oracle-managed encryption (null = default). Supplying a Vault key
  # clears the scanner's no-CMK finding for restricted/confidential data.
  kms_key_id = var.kms_key_id != "" ? var.kms_key_id : null
}

output "bucket_name" {
  value = oci_objectstorage_bucket.env.name
}
