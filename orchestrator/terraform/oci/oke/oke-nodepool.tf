locals {
  # Pick the newest compatible OKE worker image offered for this cluster/k8s version.
  node_image_id = data.oci_containerengine_node_pool_option.node_pool_option.sources[
    length(data.oci_containerengine_node_pool_option.node_pool_option.sources) - 1
  ].image_id
}

resource "oci_containerengine_node_pool" "oke_node_pool" {
  compartment_id     = var.compartment_ocid
  freeform_tags  = var.tags
  cluster_id         = oci_containerengine_cluster.oke_cluster.id
  name               = "${local.label_prefix}-node-pool"
  kubernetes_version = local.kubernetes_version
  node_shape         = local.node_pool_shape

  node_shape_config {
    ocpus         = local.node_pool_ocpus
    memory_in_gbs = local.node_pool_memory_gb
  }

  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.node_image_id
    boot_volume_size_in_gbs = var.node_pool_boot_volume_gb
  }

  node_config_details {
    size = local.node_pool_size

    dynamic "placement_configs" {
      for_each = data.oci_identity_availability_domains.ads.availability_domains
      content {
        availability_domain = placement_configs.value.name
        subnet_id            = oci_core_subnet.node_subnet.id
      }
    }

    nsg_ids = [oci_core_network_security_group.nsg_nodes.id]

    node_pool_pod_network_option_details {
      cni_type       = "OCI_VCN_IP_NATIVE"
      pod_subnet_ids = [oci_core_subnet.pod_subnet.id]
      pod_nsg_ids     = [oci_core_network_security_group.nsg_pods.id]
      max_pods_per_node = var.max_pods_per_node
    }
  }

  ssh_public_key = var.ssh_authorized_key

  initial_node_labels {
    key   = "cluster"
    value = local.cluster_name
  }

  lifecycle {
    ignore_changes = [
      node_config_details[0].size, # allow cluster autoscaler to manage node count
    ]
  }
}
