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
}

output "bucket_name" {
  value = oci_objectstorage_bucket.env.name
}
