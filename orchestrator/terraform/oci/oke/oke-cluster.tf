resource "oci_containerengine_cluster" "oke_cluster" {
  compartment_id     = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id             = var.vcn_id
  name               = local.cluster_name
  kubernetes_version = local.kubernetes_version
  type               = local.cluster_type

  endpoint_config {
    is_public_ip_enabled = false
    subnet_id             = var.api_subnet_id
    nsg_ids                = [oci_core_network_security_group.nsg_api_endpoint.id]
  }

  cluster_pod_network_options {
    cni_type = "OCI_VCN_IP_NATIVE"
  }

  options {
    service_lb_subnet_ids = [var.lb_subnet_id]

    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }

    admission_controller_options {
      is_pod_security_policy_enabled = false
    }

    kubernetes_network_config {
      pods_cidr     = var.pods_cidr
      services_cidr = var.services_cidr
    }
  }
}
