variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "region" { type = string }
variable "compartment_ocid" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

# What to provision: oci-bucket (object storage, default) or oci-instance (a
# stoppable compute VM). The module creates exactly one, branching on this.
variable "resource_kind" {
  type    = string
  default = "oci-bucket"
}

# --- Object storage (resource_kind = oci-bucket) -----------------------------
variable "bucket_name" {
  type    = string
  default = ""
}

# Optional OCI Vault key OCID for customer-managed encryption (F-SEC-04). Empty
# (the default) uses Oracle-managed encryption.
variable "kms_key_id" {
  type    = string
  default = ""
}

# --- Compute VM (resource_kind = oci-instance) -------------------------------
# Optional: the compartment the VM (and its subnet/image) live in. Empty falls
# back to compartment_ocid (where buckets go), so compute can use a different
# compartment than object storage.
variable "compute_compartment_ocid" {
  type    = string
  default = ""
}

variable "instance_name" {
  type    = string
  default = ""
}

# The compute shape. A "*.Flex" shape is sized by instance_ocpus/memory below; a
# fixed shape ignores those. Must be compatible with the chosen image.
variable "instance_shape" {
  type    = string
  default = "VM.Standard.E4.Flex"
}

variable "instance_ocpus" {
  type    = number
  default = 1
}

variable "instance_memory_gb" {
  type    = number
  default = 8
}

# An existing subnet the instance is placed in (private-only — no public IP).
variable "subnet_ocid" {
  type    = string
  default = ""
}

# The OS image OCID to boot from.
variable "image_ocid" {
  type    = string
  default = ""
}

# The SSH public key authorised for access (optional).
variable "ssh_authorized_key" {
  type    = string
  default = ""
}

# Optional cloud-init user-data (plain text; base64-encoded by the module). Use
# it to set a password / enable password auth when the image needs it.
variable "user_data" {
  type    = string
  default = ""
}

# --- Managed PostgreSQL (resource_kind = oci-postgres) -----------------------
# Optional: the compartment the DB system lives in. Empty falls back to
# compartment_ocid.
variable "db_compartment_ocid" {
  type    = string
  default = ""
}

variable "db_name" {
  type    = string
  default = ""
}

# Major PostgreSQL version offered by the managed service.
variable "db_version" {
  type    = string
  default = "14"
}

# The managed-PostgreSQL shape, sized from the request.
variable "db_shape" {
  type    = string
  default = "PostgreSQL.VM.Standard.E4.Flex.2.32GB"
}

# 1 = single instance; >1 adds high-availability read replicas.
variable "db_instance_count" {
  type    = number
  default = 1
}

# An existing private subnet for the database endpoint.
variable "db_subnet_ocid" {
  type    = string
  default = ""
}

variable "db_storage_iops" {
  type    = number
  default = 75000
}

variable "db_admin_username" {
  type    = string
  default = "pgadmin"
}

# The OCI Vault SECRET OCID holding the admin password. The password itself is
# never a Terraform value — the service reads it from the vault. Keeping it out
# of variables is what stops it landing in the plan file or state.
variable "db_admin_secret_ocid" {
  type    = string
  default = ""
}

variable "db_admin_secret_version" {
  type    = number
  default = 1
}

# --- DNS record (GAP-ANALYSIS step 5) ----------------------------------------
# Empty dns_name means no record is created, so existing environments are
# unaffected until a DNS request asks for one.
variable "dns_name" {
  type    = string
  default = ""
}

# The DNS zone the record is created in (e.g. internal.example.com).
variable "dns_zone" {
  type    = string
  default = ""
}

variable "dns_type" {
  type    = string
  default = "A"
}

# What the record points at. Empty falls back to the instance's private IP.
variable "dns_value" {
  type    = string
  default = ""
}

variable "dns_ttl" {
  type    = number
  default = 300
}

# Optional: the compartment the DNS zone lives in (empty = compartment_ocid).
variable "dns_compartment_ocid" {
  type    = string
  default = ""
}
