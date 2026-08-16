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
    assign_public_ip          = true
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
