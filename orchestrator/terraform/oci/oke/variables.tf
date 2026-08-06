###############################################################################
# OCI Container Engine for Kubernetes (OKE).
#
# Adapted from the module at terraform-oci-oke supplied by the platform owner.
# The cluster, node pool, networking and NSGs are theirs unchanged; what differs
# is the INPUT CONTRACT — the portal sends one fixed set of variables to every
# blueprint, so the names it sends are declared here and the module's own names
# are derived in locals.tf.
#
# Unlike every other OCI blueprint, this one builds its own VCN, subnets and
# gateways rather than attaching to an existing subnet. A Kubernetes cluster
# needs several purpose-shaped subnets, so borrowing one would not work.
###############################################################################

###############################################################################
# Provider — supplied by the portal
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

variable "compartment_ocid" {
  description = "Compartment everything is created in. The portal's name for what this module originally called compartment_id."
  type        = string
  default     = ""
}

###############################################################################
# Identity — supplied by the portal
###############################################################################

variable "instance_name" {
  description = <<-EOT
    The environment's name. Both the cluster name and the resource-name prefix
    derive from it.

    Capped at 24 characters because the VCN's DNS label is this name with the
    hyphens removed, and OCI limits a VCN DNS label to 15 characters — the label
    is truncated in locals.tf, and this bound keeps the truncation from colliding
    between two similarly-named environments.
  EOT
  type = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,23}$", var.instance_name))
    error_message = "instance_name must start with a letter, contain only letters, digits and hyphens, and be at most 24 characters."
  }
}

variable "tags" {
  description = "Freeform tags applied to every resource, so a cluster can be traced back to the request that asked for it."
  type        = map(string)
  default     = {}
}

variable "ssh_authorized_key" {
  description = "Public key installed on the worker nodes and the bastion. The portal's name for what this module originally called ssh_public_key. Optional — without it the bastion cannot be logged into."
  type        = string
  default     = ""
}

###############################################################################
# Sizing — supplied by the portal from the request's size
###############################################################################

variable "instance_ocpus" {
  description = "OCPUs per worker node."
  type        = number
  default     = 2
}

variable "instance_memory_gb" {
  description = "Memory per worker node, in GB."
  type        = number
  default     = 32
}

variable "node_count" {
  description = "Worker nodes in the pool. Comes from the request's size, so what an approver priced is what gets built."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 20
    error_message = "node_count must be between 1 and 20."
  }
}

variable "instance_shape" {
  description = "Compute shape for worker nodes."
  type        = string
  default     = "VM.Standard.E4.Flex"
}

variable "node_pool_boot_volume_gb" {
  type    = number
  default = 50
}

variable "max_pods_per_node" {
  description = "Maximum pods schedulable per worker node (VCN-native networking)."
  type        = number
  default     = 31
}

###############################################################################
# Cluster options — set through the portal's environment
###############################################################################

variable "oke_kubernetes_version" {
  description = "Kubernetes version, e.g. v1.29.1. Empty uses the module default."
  type        = string
  default     = ""
}

variable "oke_cluster_type" {
  description = "BASIC_CLUSTER (free control plane) or ENHANCED_CLUSTER (billed hourly). Empty uses BASIC."
  type        = string
  default     = ""
}

variable "default_kubernetes_version" {
  description = "Used when oke_kubernetes_version is empty."
  type        = string
  default     = "v1.29.1"
}

variable "pods_cidr" {
  description = "Logical CIDR OKE reports for pods (informational; must not overlap the VCN)."
  type        = string
  default     = "10.244.0.0/16"
}

variable "services_cidr" {
  description = "CIDR for Kubernetes internal service IPs."
  type        = string
  default     = "10.96.0.0/16"
}

###############################################################################
# Networking — this blueprint builds its own VCN
###############################################################################

variable "oke_vcn_cidr" {
  description = <<-EOT
    CIDR for the VCN this cluster creates. Empty uses 10.0.0.0/16.

    KNOWN LIMITATION: every cluster built with the same value gets an overlapping
    VCN. Two clusters that must talk to each other, or to the same on-premises
    network, need distinct ranges — there is no allocator here, so a second
    concurrent cluster needs this changed before it is requested.
  EOT
  type    = string
  default = ""
}

variable "default_vcn_cidr" {
  description = <<-EOT
    Used when oke_vcn_cidr is empty.

    NOT 10.0.0.0/16, which is the OCI default and therefore the collision. Three
    VCNs in this tenancy already sit on it — Codeium-POC-VCN,
    oke-vcn-quick-OKE_cluster_A10 and llama-vllm-ray-vcn — so that default would
    have created a fourth. Overlapping CIDRs can never be routed to each other or
    to on-premises over a DRG.

    10.56.64.0/18 is clear of everything observed: 10.56.0.0/20 (AI-SC-POC-VCN),
    10.56.6.0/24 (Codeium secondary) and 10.56.32.0/19 (AI-ShiftLeft-DEV-VCN).

    This is a safer default, not an allocation. Nothing here checks the range is
    still free, and a second cluster still needs a different value. Only an IPAM
    allocator fixes that properly.
  EOT
  type    = string
  default = "10.56.64.0/18"
}

variable "oke_bastion_allowed_cidr" {
  description = <<-EOT
    Public source range allowed to SSH to the bastion — an office or VPN range.

    Required, and deliberately without a default. The bastion holds a public IP
    and is the only way in to a private Kubernetes API endpoint, so an unset or
    wide-open value would publish the door to the cluster. The blueprint declares
    OCI_OKE_BASTION_CIDR as required config, so the portal refuses the request
    rather than building something exposed.
  EOT
  type    = string
  default = ""

  validation {
    condition     = var.oke_bastion_allowed_cidr == "" || can(cidrhost(var.oke_bastion_allowed_cidr, 0))
    error_message = "oke_bastion_allowed_cidr must be a valid CIDR block."
  }
}

variable "bastion_shape" {
  type    = string
  default = "VM.Standard.E4.Flex"
}

variable "bastion_ocpus" {
  type    = number
  default = 1
}

variable "bastion_memory_gb" {
  type    = number
  default = 4
}
