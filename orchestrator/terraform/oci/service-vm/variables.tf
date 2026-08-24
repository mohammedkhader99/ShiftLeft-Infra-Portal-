###############################################################################
# Generic "service on a VM" blueprint.
#
# One module, many technologies. What differs between nginx and Redis is a
# package list, a systemd unit and a port — data, not Terraform. That data comes
# from the portal (orchestrator/configure.py) as rendered cloud-init plus the
# ports to open, so adding a technology here is a data change, not a new module.
#
# What this module does NOT do: decide what to install. It receives user_data
# already rendered and applies it. Keeping the recipe in one place means the OS
# firewall and the network rules can never disagree about which ports are open.
###############################################################################

###############################################################################
# Provider
###############################################################################

variable "tenancy_ocid" { type = string }

variable "user_ocid" {
  type    = string
  default = ""
}

variable "fingerprint" {
  type    = string
  default = ""
}

variable "private_key_path" {
  type    = string
  default = ""
}

variable "region" {
  type    = string
  default = ""
}

###############################################################################
# Placement
###############################################################################

variable "compartment_ocid" {
  description = "Compartment the instance is created in."
  type        = string
  default     = ""
}

variable "subnet_ocid" {
  description = "Subnet for the instance's primary VNIC. Private by design."
  type        = string
}

variable "availability_domain" {
  description = "AD name. Empty selects the first in the region."
  type        = string
  default     = ""
}

variable "fault_domain" {
  description = "Fault domain. Empty lets OCI choose."
  type        = string
  default     = ""
}

###############################################################################
# Identity
###############################################################################

variable "instance_name" {
  description = "Base name for the instance; a two-digit index is appended."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,30}$", var.instance_name))
    error_message = "instance_name must start with a letter and contain only letters, digits and hyphens (max 31 chars)."
  }
}

variable "instance_count" {
  description = "How many identical instances to create."
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 10
    error_message = "instance_count must be between 1 and 10."
  }
}

variable "tags" {
  description = "Freeform tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "defined_tags" {
  description = "Defined tags applied to every resource."
  type        = map(string)
  default     = {}
}

###############################################################################
# Shape and image
###############################################################################

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

variable "boot_volume_size_in_gbs" {
  type    = number
  default = 50
}

variable "image_ocid" {
  description = "OCID of the OS image. Empty looks up the latest platform image matching operating_system and the shape."
  type        = string
  default     = ""
}

variable "operating_system" {
  description = "OS to look up when image_ocid is empty. The first-boot script uses dnf/systemctl/firewall-cmd, so this must stay a Red Hat family image unless CONFIG_OS_FAMILY is changed to match."
  type        = string
  default     = "Oracle Linux"
}

variable "operating_system_version" {
  type    = string
  default = "9"
}

###############################################################################
# First-boot configuration
###############################################################################

variable "user_data" {
  description = "Plain-text cloud-init, rendered by the portal from the request's technologies. Base64-encoded here. Empty means the VM installs nothing — which is why the blueprint requires CONFIG_ENABLED."
  type        = string
  default     = ""
}

variable "ssh_authorized_key" {
  description = "Public key for the default OS user. Optional: a private VM with cloud-init needs no interactive login."
  type        = string
  default     = ""
}

###############################################################################
# Access
###############################################################################

variable "service_ports" {
  description = "TCP ports the installed service listens on, from the technology's profile. Drives the network rules; the same list drives the OS firewall through cloud-init, so the two cannot drift."
  type        = list(number)
  default     = []
}

variable "allowed_cidr" {
  description = "Source range allowed to reach service_ports. Defaults to RFC1918 private space, never the internet."
  type        = string
  default     = "10.0.0.0/8"

  validation {
    condition     = can(cidrhost(var.allowed_cidr, 0))
    error_message = "allowed_cidr must be a valid CIDR block."
  }
}

variable "allowed_ssh_cidr" {
  description = "Source range allowed to reach SSH. Empty falls back to allowed_cidr."
  type        = string
  default     = ""
}

variable "assign_public_ip" {
  description = "Off by default: a service is reached from inside the VCN or through a load balancer, not by a public IP on the instance."
  type        = bool
  default     = false
}

variable "create_nsg" {
  description = "Create a network security group for this service. Requires vcn_ocid. Off by default, in which case the subnet's own rules govern access."
  type        = bool
  default     = false
}

variable "vcn_ocid" {
  description = "VCN the NSG belongs to. Required only when create_nsg is true."
  type        = string
  default     = ""

  validation {
    condition     = !var.create_nsg || var.vcn_ocid != ""
    error_message = "vcn_ocid is required when create_nsg is true."
  }
}

variable "extra_nsg_ocids" {
  description = "Existing NSGs to attach in addition to any created here."
  type        = list(string)
  default     = []
}

# A SEPARATE BLOCK VOLUME FOR SERVICE DATA (C8).
#
# Zero means none, and zero is the default, so every technology certified on
# this module before container support existed renders exactly the Terraform it
# rendered yesterday. That is not a courtesy — nginx, redis7, java21, python312,
# nodejs20, keycloak and vault are all certified against this module, and a
# change that quietly altered their plan would invalidate evidence bought with
# real machines.
#
# WHY SEPARATE, and not simply a larger boot volume: a container's data outlives
# the container, and often the machine. On its own volume it can be backed up on
# its own schedule, resized without touching the OS, and detached and reattached
# to a rebuilt host. On the boot volume it is entangled with the operating
# system, and a rebuild takes the data with it.
variable "data_volume_gb" {
  description = "Size of a separate block volume for service data. 0 creates none."
  type        = number
  default     = 0
}
