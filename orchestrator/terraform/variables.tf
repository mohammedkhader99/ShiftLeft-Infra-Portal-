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
variable "instance_name" {
  type    = string
  default = ""
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
