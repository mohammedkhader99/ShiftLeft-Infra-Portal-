variable "region" {
  type        = string
  description = "AWS region for the resource."
}

variable "resource_kind" {
  type    = string
  default = "aws-bucket"
}

variable "bucket_name" {
  type    = string
  default = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "kms_key_arn" {
  type    = string
  default = ""
}
