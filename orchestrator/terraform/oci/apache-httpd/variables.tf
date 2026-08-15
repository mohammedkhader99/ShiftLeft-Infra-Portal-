###############################################################################
# Provider credentials — passed by the orchestrator, never held in this module.
###############################################################################

variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "region" { type = string }

###############################################################################
# Required inputs
###############################################################################

variable "compartment_ocid" {
  description = "OCID of the compartment in which to create the Apache instances and NSG."
  type        = string
}

variable "subnet_ocid" {
  description = "OCID of the subnet the Apache instances attach to."
  type        = string
}

variable "ssh_authorized_key" {
  description = "SSH public key (OpenSSH format) injected for the default OS user (e.g. 'opc' on Oracle Linux)."
  type        = string
}

###############################################################################
# Naming / tagging
###############################################################################

variable "instance_name" {
  description = "Prefix applied to the names of all created resources."
  type        = string
  default     = "apache"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,30}$", var.instance_name))
    error_message = "instance_name must start with a letter and contain only letters, digits and hyphens (max 31 chars)."
  }
}

variable "tags" {
  description = "Freeform tags applied to all created resources."
  type        = map(string)
  default     = {}
}

variable "defined_tags" {
  description = "Defined tags (namespace.key => value) applied to all created resources."
  type        = map(string)
  default     = {}
}

###############################################################################
# Instance sizing / placement
###############################################################################

variable "instance_count" {
  description = "Number of Apache HTTP Server instances to create."
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 25
    error_message = "instance_count must be between 1 and 25."
  }
}

variable "availability_domain" {
  description = "Availability domain name to place instances in. Leave empty to use the first AD in the compartment/region."
  type        = string
  default     = ""
}

variable "fault_domain" {
  description = "Optional fault domain (e.g. 'FAULT-DOMAIN-1'). Leave empty to let OCI choose."
  type        = string
  default     = ""
}

variable "instance_shape" {
  description = "Compute shape for the Apache instances. Flex shapes honour ocpus/memory_in_gbs."
  type        = string
  default     = "VM.Standard.E4.Flex"
}

variable "instance_ocpus" {
  description = "OCPUs per instance (only used by Flex shapes)."
  type        = number
  default     = 1
}

variable "instance_memory_gb" {
  description = "Memory in GBs per instance (only used by Flex shapes)."
  type        = number
  default     = 8
}

variable "boot_volume_size_in_gbs" {
  description = "Boot volume size in GBs."
  type        = number
  default     = 50
}

###############################################################################
# Image selection
###############################################################################

variable "image_ocid" {
  description = "OCID of the OS image to boot. Leave empty to auto-select the latest matching platform image."
  type        = string
  default     = ""
}

variable "operating_system" {
  description = "OS to look up when image_ocid is empty."
  type        = string
  default     = "Oracle Linux"
}

variable "operating_system_version" {
  description = "OS version to look up when image_ocid is empty."
  type        = string
  default     = "9"
}

###############################################################################
# Networking
###############################################################################

variable "assign_public_ip" {
  description = "Assign a public IP to each instance's primary VNIC (requires a public subnet). Off by default: a web server is reached through a load balancer or the VCN, not by a public IP on the instance. Turn on deliberately for an internet-facing site."
  type        = bool
  default     = false
}

variable "create_nsg" {
  description = "Create a Network Security Group with HTTP/HTTPS/SSH ingress rules for the instances."
  type        = bool
  default     = true
}

variable "vcn_ocid" {
  description = "OCID of the VCN. Required only when create_nsg = true."
  type        = string
  default     = ""

  validation {
    condition     = !var.create_nsg || length(var.vcn_ocid) > 0
    error_message = "vcn_ocid must be set when create_nsg = true."
  }
}

variable "extra_nsg_ocids" {
  description = "Additional pre-existing NSG OCIDs to attach to each instance."
  type        = list(string)
  default     = []
}

variable "allowed_cidr" {
  description = "Default CIDR the ingress rules allow when not overridden. Private space, so a blueprint is never internet-exposed by accident."
  type        = string
  default     = "10.0.0.0/8"
}

variable "allowed_http_cidr" {
  description = "CIDR allowed to reach TCP/80. Empty falls back to allowed_cidr."
  type        = string
  default     = ""
}

variable "allowed_https_cidr" {
  description = "CIDR allowed to reach TCP/443 (used only when enable_https = true). Empty falls back to allowed_cidr."
  type        = string
  default     = ""
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to reach TCP/22. Empty falls back to allowed_cidr — never the internet by default."
  type        = string
  default     = ""
}

###############################################################################
# Apache configuration
###############################################################################

variable "enable_https" {
  description = "Install mod_ssl and serve HTTPS on 443 with a generated self-signed certificate."
  type        = bool
  default     = true
}

variable "server_name" {
  description = "ServerName directive for Apache. Leave empty to omit."
  type        = string
  default     = ""
}

variable "index_html_content" {
  description = "Contents of the default /var/www/html/index.html landing page."
  type        = string
  default     = "<html><head><title>Apache on OCI</title></head><body><h1>Apache HTTP Server is running on OCI.</h1><p>Provisioned by the Orchestrator via Terraform.</p></body></html>\n"
}

# OS family of the image this instance boots: "rhel" or "debian".
#
# Package names, the systemd unit, the config path, the firewall tool and the
# TLS certificate locations all follow it. Defaults to rhel, which is what every
# request made before the portal offered a choice of image implied — and what
# this module's own image lookup still selects when no image is supplied.
variable "os_family" {
  type    = string
  default = "rhel"

  validation {
    # Refuse rather than silently install nothing. A family this template has no
    # branch for would fall through to the Red Hat path and ask apt for a package
    # named httpd.
    condition     = contains(["rhel", "debian"], var.os_family)
    error_message = "os_family must be rhel or debian - this blueprint has no recipe for anything else."
  }
}

# Where this machine PUTs its own evidence at the end of first boot. A
# write-only pre-authenticated request; empty means no report is emitted and the
# boot is unchanged. See orchestrator/boot_reports.py.
variable "boot_report_url" {
  type    = string
  default = ""
}
