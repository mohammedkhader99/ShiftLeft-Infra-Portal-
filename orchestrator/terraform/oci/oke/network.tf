## --- VCN --------------------------------------------------------------------

resource "oci_core_vcn" "oke_vcn" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  cidr_blocks    = [local.vcn_cidr]
  display_name   = "${local.label_prefix}-vcn"
  dns_label      = local.vcn_dns_label
}

## --- Gateways -----------------------------------------------------------------

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = oci_core_vcn.oke_vcn.id
  display_name   = "${local.label_prefix}-igw"
  enabled        = true
}

resource "oci_core_nat_gateway" "nat_gw" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = oci_core_vcn.oke_vcn.id
  display_name   = "${local.label_prefix}-nat-gw"
}

resource "oci_core_service_gateway" "svc_gw" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = oci_core_vcn.oke_vcn.id
  display_name   = "${local.label_prefix}-svc-gw"

  services {
    service_id = data.oci_core_services.all_oci_services.services[0].id
  }
}

## --- Route tables -------------------------------------------------------------

# Public route table: used by the worker-node subnet and the load-balancer
# subnet — default route to the Internet Gateway.
resource "oci_core_route_table" "public_rt" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = oci_core_vcn.oke_vcn.id
  display_name   = "${local.label_prefix}-public-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

# Private route table: used by the API-endpoint subnet and the pod subnet —
# egress via NAT Gateway, OCI service traffic via Service Gateway.
resource "oci_core_route_table" "private_rt" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = oci_core_vcn.oke_vcn.id
  display_name   = "${local.label_prefix}-private-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.nat_gw.id
  }

  route_rules {
    destination       = data.oci_core_services.all_oci_services.services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.svc_gw.id
  }
}
