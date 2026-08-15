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

# Availability domains in the compartment/region.
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

# Latest matching platform image when no explicit image_ocid is supplied.
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

  # NSGs attached to every instance: the one we create (if any) + caller-supplied extras.
  nsg_ocids = concat(
    var.create_nsg ? [oci_core_network_security_group.apache[0].id] : [],
    var.extra_nsg_ocids,
  )

  http_cidr  = var.allowed_http_cidr != "" ? var.allowed_http_cidr : var.allowed_cidr
  https_cidr = var.allowed_https_cidr != "" ? var.allowed_https_cidr : var.allowed_cidr
  ssh_cidr   = var.allowed_ssh_cidr != "" ? var.allowed_ssh_cidr : var.allowed_cidr

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    enable_https   = var.enable_https
    server_name    = var.server_name
    index_html_b64 = base64encode(var.index_html_content)
    # Decides every package name, path and firewall command in the template.
    os_family = var.os_family
    # Where the finished machine reports what it actually has.
    boot_report_url = var.boot_report_url
    # Resolved HERE, not with a directive inside the template's literal
    # block — see the note in the template.
    apache_package = var.os_family == "debian" ? "apache2" : "httpd"
    apache_service = var.os_family == "debian" ? "apache2" : "httpd"
    # How the machine asks its OWN firewall whether :80 is open.
    # This blueprint opens the firewall by SERVICE on Red Hat
    # (--add-service=http) and by PORT on Debian, so the check has to ask
    # the same way it was opened. Querying only the port would have
    # reported CLOSED on every correctly-configured Oracle Linux machine —
    # a false alarm is as corrosive to trust as a missed failure.
    firewall_query = var.os_family == "debian" ? (
      "iptables-save 2>/dev/null | grep -q -- '--dport 80 '") : (
      "firewall-cmd --query-service=http || firewall-cmd --query-port=80/tcp")
  }))
}

###############################################################################
# Network Security Group (optional)
###############################################################################

resource "oci_core_network_security_group" "apache" {
  count          = var.create_nsg ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_ocid
  display_name   = "${var.instance_name}-nsg"
  freeform_tags  = var.tags
  defined_tags   = var.defined_tags
}

resource "oci_core_network_security_group_security_rule" "http_ingress" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.apache[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = local.http_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Allow inbound HTTP"

  tcp_options {
    destination_port_range {
      min = 80
      max = 80
    }
  }
}

resource "oci_core_network_security_group_security_rule" "https_ingress" {
  count                     = var.create_nsg && var.enable_https ? 1 : 0
  network_security_group_id = oci_core_network_security_group.apache[0].id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = local.https_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Allow inbound HTTPS"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ssh_ingress" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.apache[0].id
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

resource "oci_core_network_security_group_security_rule" "egress_all" {
  count                     = var.create_nsg ? 1 : 0
  network_security_group_id = oci_core_network_security_group.apache[0].id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "Allow all outbound"
}

###############################################################################
# Compute instances
###############################################################################

resource "oci_core_instance" "apache" {
  count               = var.instance_count
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  fault_domain        = var.fault_domain != "" ? var.fault_domain : null
  display_name        = format("%s-%02d", var.instance_name, count.index + 1)
  shape               = var.instance_shape

  freeform_tags = var.tags
  defined_tags  = var.defined_tags

  # Flex shapes require a shape_config; omitted entirely for fixed shapes.
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
    hostname_label   = format("%s%02d", var.instance_name, count.index + 1)
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_ocid
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  metadata = {
    ssh_authorized_keys = var.ssh_authorized_key
    user_data           = local.user_data
  }

  lifecycle {
    # OCI stamps Oracle-Tags.CreatedBy and CreatedOn on every resource it
    # creates. This module does not set them, so Terraform sees them as
    # unwanted and proposes removing them on EVERY plan — which made the drift
    # check report a change forever and the triage flag a healthy environment as
    # failing. Ignoring these two keys specifically leaves a deliberate change to
    # any other defined tag still detectable.
    ignore_changes = [
      defined_tags["Oracle-Tags.CreatedBy"],
      defined_tags["Oracle-Tags.CreatedOn"],
    ]
  }
}
