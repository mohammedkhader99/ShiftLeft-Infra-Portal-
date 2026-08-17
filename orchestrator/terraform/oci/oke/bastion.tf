resource "oci_core_instance" "bastion" {
  compartment_id      = var.compartment_ocid
  freeform_tags  = var.tags
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name         = "${local.label_prefix}-bastion"
  shape                = var.bastion_shape

  shape_config {
    ocpus         = var.bastion_ocpus
    memory_in_gbs = var.bastion_memory_gb
  }

  create_vnic_details {
    subnet_id                = var.bastion_subnet_id
    # NO PUBLIC IP. This asked for one, and OCI refused the VNIC outright:
    # "Public IP addresses are prohibited in this subnet" — the whole apply died,
    # and the visible symptom was "2 nodes register timeout", which sent the
    # investigation to NSGs, security lists and route tables. All three were
    # correct. The bastion was the fault.
    #
    # The module used to build its own subnet and could make it public. It now
    # consumes subnets the network team provisioned, and every one of them is
    # private — so a bastion that demands a public IP can never launch in this
    # VCN. Reached from inside instead, which is what OCI_OKE_BASTION_CIDR
    # (10.56.39.0/24) already describes, or through OCI's managed Bastion
    # service.
    assign_public_ip          = false
    nsg_ids                   = [oci_core_network_security_group.nsg_bastion.id]
    display_name              = "${local.label_prefix}-bastion-vnic"
  }

  source_details {
    source_type = "image"
    # The provider's attribute is source_id; the original module had image_id
    # here, which fails validation and so could never have been applied.
    source_id = data.oci_core_images.bastion_image.images[0].id
  }

  metadata = {
    ssh_authorized_keys = var.ssh_authorized_key
  }
}
