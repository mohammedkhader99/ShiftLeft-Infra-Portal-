data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# CIDR block representing "All <region> Services In Oracle Services Network",
# used to route Service Gateway traffic (needed by the private API endpoint
# and the private pod subnet to reach OCI services such as OCIR, Object Storage).
data "oci_core_services" "all_oci_services" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

# Compatible node images/shapes for this specific cluster and k8s version.
data "oci_containerengine_node_pool_option" "node_pool_option" {
  node_pool_option_id = oci_containerengine_cluster.oke_cluster.id
  compartment_id      = var.compartment_ocid
}

# Latest Oracle Linux image for the bastion host.
data "oci_core_images" "bastion_image" {
  compartment_id           = var.compartment_ocid
  shape                    = var.bastion_shape
  operating_system         = "Oracle Linux"
  operating_system_version = "8"
  sort_by                  = "TIMECREATED"
  sort_order                = "DESC"
}

# The VCN is given, so its address range is a fact to be read rather than a
# number this module picks. local.operator_cidr is derived from it.
data "oci_core_vcn" "provided" {
  vcn_id = var.vcn_id
}
