###############################################################################
# The adapter between the portal's variable contract and the module's own names.
#
# Everything the original module referred to as var.<x> now resolves here, so the
# resource files stay as the platform owner wrote them.
###############################################################################

locals {
  # --- Naming ---------------------------------------------------------------
  # The environment's name prefixes every resource, so two clusters in one
  # compartment can be told apart. The original defaulted both of these to fixed
  # strings ("oke", "oke-cluster"), which meant a second request produced a
  # second cluster with the same name.
  label_prefix = var.instance_name
  cluster_name = var.instance_name

  # A VCN DNS label is alphanumeric and at most 15 characters, while the
  # environment name may be longer and carry hyphens. Truncating is safe here
  # because instance_name is capped at 24 characters and starts with a letter.

  # --- Cluster --------------------------------------------------------------
  kubernetes_version = var.oke_kubernetes_version != "" ? var.oke_kubernetes_version : var.default_kubernetes_version
  cluster_type       = var.oke_cluster_type != "" ? var.oke_cluster_type : "BASIC_CLUSTER"

  # --- Node pool ------------------------------------------------------------
  # Sized from the request rather than from module defaults, so a "small"
  # environment does not quietly build the same cluster as an "xlarge" one.
  node_pool_shape     = var.instance_shape
  node_pool_ocpus     = var.instance_ocpus
  node_pool_memory_gb = var.instance_memory_gb
  node_pool_size      = var.node_count

  # --- Networking -----------------------------------------------------------
  # Read from the VCN this cluster was GIVEN. The module used to carve five
  # subnet ranges out of a VCN it created; it now consumes subnets the network
  # team allocated and never computes an address itself.
  operator_cidr = data.oci_core_vcn.provided.cidr_block
  bastion_allowed_cidr = var.oke_bastion_allowed_cidr
}
