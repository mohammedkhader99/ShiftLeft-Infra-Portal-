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

# Latest matching platform image when no explicit image_ocid is supplied. Filtered
# by shape, so the pair can never be incompatible.
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
  # Flex shapes require shape_config; fixed shapes must not receive it.
  is_flex = can(regex("Flex", var.instance_shape))

  availability_domain = var.availability_domain != "" ? var.availability_domain : data.oci_identity_availability_domains.ads.availability_domains[0].name

  image_ocid = var.image_ocid != "" ? var.image_ocid : data.oci_core_images.platform[0].images[0].id

  ssh_cidr = var.allowed_ssh_cidr != "" ? var.allowed_ssh_cidr : var.allowed_cidr

  nsg_ocids = concat(
    var.create_nsg ? [oci_core_network_security_group.service[0].id] : [],
    var.extra_nsg_ocids,
  )

  # Ports are keyed by value so adding or removing one does not renumber the
  # others and force unrelated rules to be destroyed and recreated.
  service_ports = { for p in var.service_ports : tostring(p) => p }
}

###############################################################################
# Network Security Group (optional)
###############################################################################

resource "oci_core_network_security_group" "service" {
  count          = var.create_nsg ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_ocid
  display_name   = "${var.instance_name}-nsg"
  freeform_tags  = var.tags
  defined_tags   = var.defined_tags
}

resource "oci_core_network_security_group_security_rule" "service_ingress" {
  for_each                  = var.create_nsg ? local.service_ports : {}
  network_security_group_id = oci_core_network_security_group.service[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = var.allowed_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Allow inbound TCP ${each.key}"

  tcp_options {
    destination_port_range {
      min = each.value
      max = each.value
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ssh_ingress" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.service[0].id
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

# Egress is required, not optional: the first-boot install fetches packages.
resource "oci_core_network_security_group_security_rule" "egress_all" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.service[0].id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "Allow all outbound"
}

###############################################################################
# Compute instances
###############################################################################

resource "oci_core_instance" "service" {
  count               = var.instance_count
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  fault_domain        = var.fault_domain != "" ? var.fault_domain : null
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
    hostname_label   = format("%s%02d", replace(var.instance_name, "-", ""), count.index + 1)
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_ocid
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  # Oracle Cloud Agent plugins, stated explicitly rather than inherited from
  # whichever image was chosen. Run Command is how the portal ASKS a machine
  # whether its software really installed — the only check needing no inbound
  # access to a private-only VM.
  #
  # RUN COMMAND IS ENABLED WHERE IT EXISTS, WHICH IS NOT EVERYWHERE.
  #
  # Measured on 14 Aug 2026, two machines launched a minute apart in the same
  # subnet: the Oracle Linux agent offers 16 plugins including this one; the
  # Ubuntu 24.04 agent offers 11 and does NOT include it. Asking for it there is
  # accepted by OCI, recorded on the instance as ENABLED, and silently never
  # delivered — the agent has no such plugin to start.
  #
  # It is still requested for every family: on Red Hat it is what makes a machine
  # interrogable, and on Ubuntu it is a harmless no-op. What must NOT happen is
  # the portal assuming the request succeeded — a Debian machine cannot be
  # verified this way and needs another route, which is why nginx-on-Ubuntu
  # remains unproven and `debian` is absent from configure.VERIFIED_FAMILIES.
  agent_config {
    is_monitoring_disabled = false
    is_management_disabled = false

    plugins_config {
      name          = "Compute Instance Run Command"
      desired_state = "ENABLED"
    }
  }

  # user_data is omitted entirely when empty rather than sent as "", so a VM with
  # no configuration is visibly a bare VM instead of one that ran nothing.
  metadata = merge(
    var.ssh_authorized_key != "" ? { ssh_authorized_keys = var.ssh_authorized_key } : {},
    var.user_data != "" ? { user_data = base64encode(var.user_data) } : {},
  )

  lifecycle {
    # OCI stamps these two on every resource it creates; this module does not set
    # them, so without this every plan proposes removing them and the drift check
    # reports a healthy environment as changed. See the same block in
    # oci/apache-httpd, where it was first diagnosed.
    ignore_changes = [
      defined_tags["Oracle-Tags.CreatedBy"],
      defined_tags["Oracle-Tags.CreatedOn"],
    ]
  }
}

# --- service data, on its own volume (C8) ------------------------------------
#
# PARAVIRTUALIZED, not iSCSI. An iSCSI attachment needs the machine to run
# `iscsiadm` with the target's own IQN and portal address before the disk
# appears at all — several commands, each able to fail silently at first boot,
# on a machine nobody can log in to. A paravirtualized attachment appears as a
# device without the machine doing anything, which is one less thing that can
# half-work.
resource "oci_core_volume" "data" {
  count               = var.data_volume_gb > 0 ? var.instance_count : 0
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = format("%s-%02d-data", var.instance_name, count.index + 1)
  size_in_gbs         = var.data_volume_gb

  freeform_tags = var.tags
  defined_tags  = var.defined_tags
}

resource "oci_core_volume_attachment" "data" {
  count = var.data_volume_gb > 0 ? var.instance_count : 0
  # `paravirtualized` is what makes the device show up on its own; see above.
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.service[count.index].id
  volume_id       = oci_core_volume.data[count.index].id
  display_name    = format("%s-%02d-data-attach", var.instance_name, count.index + 1)
}
