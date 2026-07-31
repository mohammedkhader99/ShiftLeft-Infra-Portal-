# The request's real cloud resource. The module provisions exactly one thing,
# branching on var.resource_kind:
#   - oci-bucket   : an Object Storage bucket (the original, safe default).
#   - oci-instance : a private-only Compute VM, sized from the request — the
#                    stoppable resource the control plane can start/stop.

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

# --- Object storage (resource_kind = oci-bucket) -----------------------------

# Read-only lookup of the Object Storage namespace (no resource created).
data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.compartment_ocid
}

resource "oci_objectstorage_bucket" "env" {
  count          = var.resource_kind == "oci-bucket" ? 1 : 0
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

# --- Compute VM (resource_kind = oci-instance) -------------------------------

# Availability domains are listed at the tenancy level; used to place the VM.
data "oci_identity_availability_domains" "ads" {
  count          = var.resource_kind == "oci-instance" ? 1 : 0
  compartment_id = var.tenancy_ocid
}

resource "oci_core_instance" "env" {
  count               = var.resource_kind == "oci-instance" ? 1 : 0
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads[0].availability_domains[0].name
  display_name        = var.instance_name
  shape               = "VM.Standard.E4.Flex"

  # Flex shape sized from the request (1 OCPU ~ 2 vCPUs on x86).
  shape_config {
    ocpus         = var.instance_ocpus
    memory_in_gbs = var.instance_memory_gb
  }

  # Private-only: placed in the supplied subnet, no public IP.
  create_vnic_details {
    subnet_id        = var.subnet_ocid
    assign_public_ip = false
  }

  source_details {
    source_type = "image"
    source_id   = var.image_ocid
  }

  metadata = {
    ssh_authorized_keys = var.ssh_authorized_key
  }

  freeform_tags = var.tags
}

# `try(...)` returns "" for the resource that wasn't created (count = 0) instead
# of erroring on an out-of-range index.
output "bucket_name" {
  value = try(oci_objectstorage_bucket.env[0].name, "")
}

output "instance_ocid" {
  value = try(oci_core_instance.env[0].id, "")
}

output "instance_name" {
  value = try(oci_core_instance.env[0].display_name, "")
}
