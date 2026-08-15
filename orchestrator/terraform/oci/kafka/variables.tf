###############################################################################
# Apache Kafka on OCI Compute, in KRaft mode.
#
# KRaft (Kafka 3.3+) removes the ZooKeeper dependency: the brokers elect their
# own controller quorum. Each node runs as both broker and controller, which is
# the documented layout for small clusters and keeps this to one machine type.
#
# Nodes find each other by DNS name, not IP, because Terraform cannot know an
# instance's private IP before creating it — but it CAN know its hostname, which
# is derived from the environment name. That requires the VCN and subnet to have
# DNS labels; without them the quorum cannot form.
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
  description = "Compartment the cluster is created in."
  type        = string
  default     = ""
}

variable "subnet_ocid" {
  description = "Subnet for the brokers. Must have a DNS label: the controller quorum is addressed by hostname."
  type        = string
}

variable "availability_domain" {
  description = "AD name. Empty selects the first in the region."
  type        = string
  default     = ""
}

###############################################################################
# Identity
###############################################################################

variable "instance_name" {
  description = "Base name for the brokers; a two-digit index is appended."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,28}$", var.instance_name))
    error_message = "instance_name must start with a letter, contain only letters, digits and hyphens, and be at most 29 characters (two are reserved for the broker index)."
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
# Cluster shape
###############################################################################

variable "broker_count" {
  description = "Number of Kafka nodes. 1 for development. 3 is the smallest cluster that tolerates losing a node — a KRaft quorum needs a majority, so an even count buys nothing."
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 3, 5], var.broker_count)
    error_message = "broker_count must be 1, 3 or 5: a controller quorum requires an odd number of voters."
  }
}

variable "instance_shape" {
  type    = string
  default = "VM.Standard.E4.Flex"
}

variable "instance_ocpus" {
  type    = number
  default = 2
}

variable "instance_memory_gb" {
  type    = number
  default = 16
}

variable "boot_volume_size_in_gbs" {
  description = "Boot volume size. Kafka's log directory lives on it, so this is the cluster's storage. A production cluster should use separate block volumes; this module deliberately does not create them."
  type        = number
  default     = 100
}

variable "kafka_heap_opts" {
  description = "JVM heap for the broker. Kafka relies heavily on the OS page cache, so a large heap is counter-productive — leave most of the memory to the kernel."
  type        = string
  default     = "-Xmx4g -Xms4g"
}

###############################################################################
# Image
###############################################################################

variable "image_ocid" {
  description = "OCID of the OS image. Empty looks up the latest platform image matching operating_system and the shape."
  type        = string
  default     = ""
}

variable "operating_system" {
  description = "OS to look up when image_ocid is empty. The install script uses dnf and systemd, so this must stay a Red Hat family image."
  type        = string
  default     = "Oracle Linux"
}

variable "operating_system_version" {
  type    = string
  default = "9"
}

variable "java_package" {
  description = "JDK package to install. Kafka needs a JVM; Oracle Linux 9 carries this one in its base repositories, so it is reachable through a service gateway."
  type        = string
  default     = "java-21-openjdk-headless"
}

###############################################################################
# Kafka distribution
###############################################################################

variable "kafka_version" {
  description = <<-EOT
    Apache Kafka version to install.

    4.x removes ZooKeeper entirely and requires KRaft, which is what this module
    builds, so it is the right family here. It needs Java 17 or newer — the
    java_package default satisfies that.

    Was 3.9.0, which the Apache CDN no longer carries: it only serves current
    releases, so that default had started returning 404. Verify a version is
    still published before pinning it.
  EOT
  type    = string
  default = "4.3.1"
}

variable "scala_version" {
  description = "Scala build of the Kafka distribution, which forms part of the archive name."
  type        = string
  default     = "2.13"
}

variable "kafka_source_url" {
  description = <<-EOT
    Where to fetch the Kafka archive. Empty derives the public Apache CDN URL,
    which needs a NAT or internet gateway.

    A subnet with only a SERVICE gateway cannot reach the public internet. Stage
    the archive in OCI Object Storage and put a pre-authenticated request URL
    here: Object Storage IS reachable through a service gateway. Getting this
    wrong produces a running machine with no Kafka on it.
  EOT
  type        = string
  default     = ""
}

variable "cluster_id" {
  description = "KRaft cluster id. Every node must share it, so it is derived from the environment name when empty rather than generated per node."
  type        = string
  default     = ""
}

###############################################################################
# Topic defaults
###############################################################################

variable "num_partitions" {
  description = "Default partition count for auto-created topics."
  type        = number
  default     = 3
}

###############################################################################
# Access
###############################################################################

variable "client_port" {
  description = "Listener clients connect to."
  type        = number
  default     = 9092
}

variable "controller_port" {
  description = "Listener the controller quorum uses between nodes. Cluster-internal only."
  type        = number
  default     = 9093
}

variable "allowed_cidr" {
  description = "Source range allowed to reach the client port. Defaults to RFC1918 private space, never the internet. Kafka is configured PLAINTEXT with no authentication, so this range is the only thing protecting it."
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
  description = "Off by default. An unauthenticated Kafka broker must never be reachable from the internet."
  type        = bool
  default     = false
}

variable "create_nsg" {
  description = "Create a network security group for the cluster. Requires vcn_ocid. Off by default, in which case the subnet's own rules govern access."
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

variable "ssh_authorized_key" {
  description = "Public key for the default OS user. Optional."
  type        = string
  default     = ""
}

variable "boot_report_url" {
  description = <<-EOT
    Where each node PUTs its own self-report at the end of first boot. Empty
    disables reporting entirely, which leaves the boot exactly as it was.
  EOT
  type        = string
  default     = ""
}
