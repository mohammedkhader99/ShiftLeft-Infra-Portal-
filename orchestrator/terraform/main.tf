# The request's real cloud resource. The module provisions exactly one thing,
# branching on var.resource_kind:
#   - oci-bucket   : an Object Storage bucket (the original, safe default).
#   - oci-instance : a private-only Compute VM, sized from the request — the
#                    stoppable resource the control plane can start/stop.
#   - oci-postgres : an OCI Database with PostgreSQL system (managed DBaaS) —
#                    the first technology delivered as the thing actually asked
#                    for, rather than a placeholder (GAP-ANALYSIS.md step 2).

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
  count = var.resource_kind == "oci-instance" ? 1 : 0
  # Compute may live in a different compartment than the buckets; empty falls back.
  compartment_id      = var.compute_compartment_ocid != "" ? var.compute_compartment_ocid : var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads[0].availability_domains[0].name
  display_name        = var.instance_name
  shape               = var.instance_shape

  # Only Flex shapes take a shape_config (ocpus/memory); fixed shapes have set
  # sizing, so omit it there. Sized from the request (1 OCPU ~ 2 vCPUs on x86).
  dynamic "shape_config" {
    for_each = can(regex("Flex", var.instance_shape)) ? [1] : []
    content {
      ocpus         = var.instance_ocpus
      memory_in_gbs = var.instance_memory_gb
    }
  }

  # Private-only: placed in the supplied subnet, no public IP.
  create_vnic_details {
    subnet_id        = var.subnet_ocid
    assign_public_ip = false
  }

  source_details {
    source_type             = "image"
    source_id               = var.image_ocid
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  # Access is image-dependent, so include only what's supplied: an SSH key and/or
  # cloud-init user-data (base64-encoded). A custom image with a baked-in password
  # can need neither.
  metadata = merge(
    var.ssh_authorized_key != "" ? { ssh_authorized_keys = var.ssh_authorized_key } : {},
    var.user_data != "" ? { user_data = base64encode(var.user_data) } : {},
  )

  freeform_tags = var.tags
}

# --- Managed PostgreSQL (resource_kind = oci-postgres) -----------------------
# OCI Database with PostgreSQL: a managed database system, so the requester gets
# the technology they actually asked for with no OS to run and no admin step.
#
# The admin password is NEVER passed as a value. It is referenced by OCI Vault
# secret OCID, so the secret exists only in the vault — not in this repo, the
# orchestrator's environment, the plan, or Terraform state (CLAUDE.md: secrets
# never in code or git; ARCHITECTURE.md P2).

resource "oci_psql_db_system" "env" {
  count          = var.resource_kind == "oci-postgres" ? 1 : 0
  compartment_id = var.db_compartment_ocid != "" ? var.db_compartment_ocid : var.compartment_ocid
  display_name   = var.db_name
  db_version     = var.db_version
  shape          = var.db_shape
  instance_count = var.db_instance_count

  # Private-only by design: placed in the supplied DB subnet, no public endpoint.
  network_details {
    subnet_id = var.db_subnet_ocid
  }

  storage_details {
    system_type        = "OCI_OPTIMIZED_STORAGE"
    is_regionally_durable = true
    iops               = var.db_storage_iops
  }

  credentials {
    username = var.db_admin_username
    password_details {
      password_type = "VAULT_SECRET"
      secret_id     = var.db_admin_secret_ocid
      secret_version = var.db_admin_secret_version
    }
  }

  freeform_tags = var.tags
}

# --- DNS record (GAP-ANALYSIS step 5) ----------------------------------------
# A name for an environment, created in the SAME workspace as the environment it
# names. That is deliberate: the record then shares the environment's lifecycle,
# so tearing the environment down removes its name too, instead of leaving a
# dangling record pointing at nothing.
#
# Created only when a name is supplied, so every existing environment plans
# unchanged until one is requested.

resource "oci_dns_rrset" "env" {
  count           = var.dns_name != "" ? 1 : 0
  zone_name_or_id = var.dns_zone
  domain          = "${var.dns_name}.${var.dns_zone}"
  rtype           = var.dns_type
  compartment_id  = var.dns_compartment_ocid != "" ? var.dns_compartment_ocid : var.compartment_ocid

  items {
    domain = "${var.dns_name}.${var.dns_zone}"
    rtype  = var.dns_type
    ttl    = var.dns_ttl
    # Point at the supplied value, or fall back to the instance's own private IP
    # so the name follows the environment without anyone copying an address.
    rdata = var.dns_value != "" ? var.dns_value : try(oci_core_instance.env[0].private_ip, "")
  }
}

# `try(...)` returns "" for the resource that wasn't created (count = 0) instead
# of erroring on an out-of-range index.
output "bucket_name" {
  value = try(oci_objectstorage_bucket.env[0].name, "")
}

# The instance's private address — what a DNS A record points at.
output "instance_private_ip" {
  value = try(oci_core_instance.env[0].private_ip, "")
}

output "dns_domain" {
  value = try(oci_dns_rrset.env[0].domain, "")
}

output "postgres_ocid" {
  value = try(oci_psql_db_system.env[0].id, "")
}

output "postgres_name" {
  value = try(oci_psql_db_system.env[0].display_name, "")
}

output "instance_ocid" {
  value = try(oci_core_instance.env[0].id, "")
}

output "instance_name" {
  value = try(oci_core_instance.env[0].display_name, "")
}
