variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "region" { type = string }
variable "compartment_ocid" { type = string }
variable "bucket_name" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}

# Optional OCI Vault key OCID for customer-managed encryption (F-SEC-04). Empty
# (the default) uses Oracle-managed encryption.
variable "kms_key_id" {
  type    = string
  default = ""
}
