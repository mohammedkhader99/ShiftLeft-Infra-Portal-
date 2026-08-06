###############################################################################
# Provider
###############################################################################

provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

###############################################################################
# Data sources
###############################################################################

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

# The subnet's DNS domain. The controller quorum is addressed by hostname, so
# this is read rather than assumed — a subnet without a DNS label returns an
# empty domain, and the check in locals turns that into a clear failure instead
# of a cluster that never forms a quorum.
data "oci_core_subnet" "this" {
  subnet_id = var.subnet_ocid
}

# Latest matching platform image when no explicit image_ocid is supplied,
# filtered by shape so the pair cannot be incompatible.
data "oci_core_images" "platform" {
  count                    = var.image_ocid == "" ? 1 : 0
  compartment_id           = var.compartment_ocid
  operating_system         = var.operating_system
  operating_system_version = var.operating_system_version
  shape                    = var.instance_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

locals {
  is_flex = can(regex("Flex", var.instance_shape))

  availability_domain = var.availability_domain != "" ? var.availability_domain : data.oci_identity_availability_domains.ads.availability_domains[0].name

  image_ocid = var.image_ocid != "" ? var.image_ocid : data.oci_core_images.platform[0].images[0].id

  ssh_cidr = var.allowed_ssh_cidr != "" ? var.allowed_ssh_cidr : var.allowed_cidr

  nsg_ocids = concat(
    var.create_nsg ? [oci_core_network_security_group.kafka[0].id] : [],
    var.extra_nsg_ocids,
  )

  # Hostnames are derived, not read back, because Terraform must write the
  # quorum voter list BEFORE the instances exist. This must stay in step with
  # hostname_label on the VNIC below.
  hostnames = [
    for i in range(var.broker_count) : format("%s%02d", var.instance_name, i + 1)
  ]

  subnet_domain = data.oci_core_subnet.this.subnet_domain_name
  fqdns         = [for h in local.hostnames : "${h}.${local.subnet_domain}"]

  # node_id@host:port, one per node. Every node is given the same list.
  quorum_voters = join(",", [
    for i in range(var.broker_count) :
    "${i + 1}@${local.fqdns[i]}:${var.controller_port}"
  ])

  bootstrap_servers = join(",", [for f in local.fqdns : "${f}:${var.client_port}"])

  # Every node must format its storage with the SAME cluster id, so it is derived
  # from the environment name rather than generated per node. md5 gives 32 hex
  # characters; 16 of them base64-encoded is the 22-character id Kafka expects.
  cluster_id = var.cluster_id != "" ? var.cluster_id : replace(
    base64encode(substr(md5(var.instance_name), 0, 16)), "=", "")

  # A single node replicates nothing; three or more should survive losing one.
  replication_factor = var.broker_count >= 3 ? 3 : 1
  min_isr            = var.broker_count >= 3 ? 2 : 1

  # Empty means "derive the public Apache CDN URL" — see the variable's note
  # about subnets that have only a service gateway.
  archive_name = "kafka_${var.scala_version}-${var.kafka_version}"
  source_url = var.kafka_source_url != "" ? var.kafka_source_url : (
    "https://dlcdn.apache.org/kafka/${var.kafka_version}/${local.archive_name}.tgz"
  )
}

###############################################################################
# Network Security Group (optional)
###############################################################################

resource "oci_core_network_security_group" "kafka" {
  count          = var.create_nsg ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_ocid
  display_name   = "${var.instance_name}-nsg"
  freeform_tags  = var.tags
  defined_tags   = var.defined_tags
}

resource "oci_core_network_security_group_security_rule" "client_ingress" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.kafka[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = var.allowed_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Kafka clients"

  tcp_options {
    destination_port_range {
      min = var.client_port
      max = var.client_port
    }
  }
}

# Controller traffic is cluster-internal: the source is the cluster's own subnet,
# never the client range. Nothing outside the cluster has any business here.
resource "oci_core_network_security_group_security_rule" "controller_ingress" {
  count                     = var.create_nsg && var.broker_count > 1 ? 1 : 0
  network_security_group_id = oci_core_network_security_group.kafka[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = data.oci_core_subnet.this.cidr_block
  source_type               = "CIDR_BLOCK"
  description               = "Kafka controller quorum (cluster-internal)"

  tcp_options {
    destination_port_range {
      min = var.controller_port
      max = var.controller_port
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ssh_ingress" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.kafka[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = local.ssh_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Allow inbound SSH"

  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

# Required, not optional: the JDK comes from the OS repositories and the Kafka
# archive from wherever kafka_source_url points.
resource "oci_core_network_security_group_security_rule" "egress_all" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.kafka[0].id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "Allow all outbound"
}

###############################################################################
# Brokers
###############################################################################

resource "oci_core_instance" "kafka" {
  count               = var.broker_count
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = format("%s-%02d", var.instance_name, count.index + 1)
  shape               = var.instance_shape

  freeform_tags = var.tags
  defined_tags  = var.defined_tags

  dynamic "shape_config" {
    for_each = local.is_flex ? [1] : []
    content {
      ocpus         = var.instance_ocpus
      memory_in_gbs = var.instance_memory_gb
    }
  }

  create_vnic_details {
    subnet_id        = var.subnet_ocid
    assign_public_ip = var.assign_public_ip
    nsg_ids          = local.nsg_ocids
    display_name     = format("%s-%02d-vnic", var.instance_name, count.index + 1)
    # Must match local.hostnames, which the quorum voter list is built from.
    hostname_label = local.hostnames[count.index]
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_ocid
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  # Rendered per node: every broker needs its own node.id and its own advertised
  # address, while sharing one quorum voter list and one cluster id.
  metadata = {
    ssh_authorized_keys = var.ssh_authorized_key
    user_data = base64encode(templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
      node_id            = count.index + 1
      advertised_host    = local.fqdns[count.index]
      quorum_voters      = local.quorum_voters
      cluster_id         = local.cluster_id
      client_port        = var.client_port
      controller_port    = var.controller_port
      java_package       = var.java_package
      source_url         = local.source_url
      archive_name       = local.archive_name
      heap_opts          = var.kafka_heap_opts
      num_partitions     = var.num_partitions
      replication_factor = local.replication_factor
      min_isr            = local.min_isr
    }))
  }

  lifecycle {
    # A subnet with no DNS label cannot host a multi-node cluster: the brokers
    # would have no names to address each other by, so the quorum never forms.
    # A precondition BLOCKS the plan; a `check` block only warns, and a warning
    # scrolling past in a log is not a safeguard.
    precondition {
      condition     = var.broker_count == 1 || local.subnet_domain != ""
      error_message = "The subnet has no DNS label, so brokers cannot resolve each other and the KRaft quorum cannot form. Use a subnet with a DNS label, or set broker_count = 1."
    }

    # OCI stamps these on every resource it creates; this module does not set
    # them, so without this every plan proposes removing them and the drift check
    # reports a healthy cluster as changed.
    ignore_changes = [
      defined_tags["Oracle-Tags.CreatedBy"],
      defined_tags["Oracle-Tags.CreatedOn"],
    ]
  }
}
