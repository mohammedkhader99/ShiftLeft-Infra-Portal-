## --- API endpoint subnet (private, regional) ----------------------------

resource "oci_core_subnet" "api_endpoint_subnet" {
  compartment_id             = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id                     = oci_core_vcn.oke_vcn.id
  cidr_block                 = local.api_endpoint_subnet_cidr
  display_name               = "${local.label_prefix}-api-endpoint-subnet"
  dns_label                  = "apiendpoint"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private_rt.id
  security_list_ids          = [oci_core_vcn.oke_vcn.default_security_list_id]
}

## --- Worker node subnet (public, regional) --------------------------------

resource "oci_core_subnet" "node_subnet" {
  compartment_id             = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id                     = oci_core_vcn.oke_vcn.id
  cidr_block                 = local.node_subnet_cidr
  display_name               = "${local.label_prefix}-node-subnet"
  dns_label                  = "nodes"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public_rt.id
  security_list_ids          = [oci_core_vcn.oke_vcn.default_security_list_id]
}

## --- Pod subnet (private, regional) - VCN-native pod networking -----------

resource "oci_core_subnet" "pod_subnet" {
  compartment_id             = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id                     = oci_core_vcn.oke_vcn.id
  cidr_block                 = local.pod_subnet_cidr
  display_name               = "${local.label_prefix}-pod-subnet"
  dns_label                  = "pods"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private_rt.id
  security_list_ids          = [oci_core_vcn.oke_vcn.default_security_list_id]
}

## --- Load balancer subnet (public, regional) ------------------------------

resource "oci_core_subnet" "lb_subnet" {
  compartment_id             = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id                     = oci_core_vcn.oke_vcn.id
  cidr_block                 = local.lb_subnet_cidr
  display_name               = "${local.label_prefix}-lb-subnet"
  dns_label                  = "lb"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public_rt.id
  security_list_ids          = [oci_core_vcn.oke_vcn.default_security_list_id]
}

## --- Bastion subnet (public, regional, small) -----------------------------

resource "oci_core_subnet" "bastion_subnet" {
  compartment_id             = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id                     = oci_core_vcn.oke_vcn.id
  cidr_block                 = local.bastion_subnet_cidr
  display_name               = "${local.label_prefix}-bastion-subnet"
  dns_label                  = "bastion"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public_rt.id
  security_list_ids          = [oci_core_vcn.oke_vcn.default_security_list_id]
}
