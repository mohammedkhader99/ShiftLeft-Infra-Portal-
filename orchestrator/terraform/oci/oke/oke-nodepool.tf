locals {
  # CHOOSE THE WORKER IMAGE BY NAME, NEVER BY POSITION.
  #
  # This used to take sources[length - 1] — "the last one, which must be the
  # newest". OCI returns 120 images in an order that looks sorted and is not.
  # The last one is `Oracle-Linux-7.9-2026.07.20-0`: not just old, but not an
  # OKE image at all (no -OKE- suffix). Oracle Linux 7 is cgroup v1 only, and
  # every kubelet from Kubernetes 1.32 refuses to start on cgroup v1:
  #
  #   kubelet: "command failed" err="failed to validate kubelet configuration,
  #   error: kubelet is configured to not run on a host using cgroup v1"
  #
  # systemd restarted kubelet every 10 seconds, the node never registered, and
  # OKE reaped the pool at 20 minutes with "N nodes(s) register timeout. First,
  # confirm that network prerequisites have been met." The network prerequisites
  # were met. Three builds were spent checking them.
  #
  # Same defect as orchestrator/kubernetes_versions.py:70 — trusting list order
  # from OCI. Fixed there for versions, missed here for images.
  k8s_plain = replace(local.kubernetes_version, "v", "")

  # Only images actually built for THIS Kubernetes version, and only for the
  # architecture the node shape uses. An aarch64 image on an x86 shape fails
  # later and less clearly.
  # GPU is excluded deliberately: sorting by name put
  # "Oracle-Linux-9.7-Gen2-GPU-..." last (G sorts after a digit), so the first
  # version of this fix picked a GPU image for an ordinary VM.Standard shape.
  # Specialised images must be opted into, never fallen into by alphabet.
  oke_images = [
    for s in data.oci_containerengine_node_pool_option.node_pool_option.sources : s
    if strcontains(s.source_name, "-OKE-${local.k8s_plain}-")
    && !strcontains(s.source_name, "aarch64")
    && !strcontains(s.source_name, "GPU")
  ]

  # Newest by NAME, so Oracle-Linux-9.x wins over 8.x and a later build date
  # wins within an OS. Sorting is explicit rather than positional.
  oke_image_names = sort([for s in local.oke_images : s.source_name])
  chosen_image_name = length(local.oke_image_names) > 0 ? element(local.oke_image_names, length(local.oke_image_names) - 1) : ""
  node_image_id = one([
    for s in local.oke_images : s.image_id if s.source_name == local.chosen_image_name
  ])
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
        subnet_id            = var.node_subnet_id
      }
    }

    nsg_ids = [oci_core_network_security_group.nsg_nodes.id]

    node_pool_pod_network_option_details {
      cni_type       = "OCI_VCN_IP_NATIVE"
      pod_subnet_ids = [var.pod_subnet_id]
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

    # Refuse to build rather than build something that cannot register. Without
    # this, an unmatched image yields a null id and the failure surfaces 20
    # minutes later as "nodes register timeout" — a message that sends everyone
    # to the network configuration, which is where three builds were lost.
    precondition {
      condition     = local.node_image_id != null && local.node_image_id != ""
      error_message = "No OKE worker image is published for Kubernetes ${local.k8s_plain}. OCI offers images named like Oracle-Linux-9.7-<date>-OKE-<version>-<build>; none matched this version. Pick a Kubernetes version OCI still ships worker images for."
    }
  }
}
