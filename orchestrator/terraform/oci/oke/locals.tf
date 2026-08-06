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
  vcn_dns_label = substr(replace(lower(var.instance_name), "-", ""), 0, 15)

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
  vcn_cidr = var.oke_vcn_cidr != "" ? var.oke_vcn_cidr : var.default_vcn_cidr

  # Subnet ranges are CARVED FROM the VCN rather than being independent inputs.
  # The original had five separate CIDR variables that all had to be kept inside
  # the VCN by hand; changing the VCN alone produced subnets outside it. These
  # offsets reproduce the original layout exactly for a /16 VCN.
  api_endpoint_subnet_cidr = cidrsubnet(local.vcn_cidr, 12, 0)   # 10.0.0.0/28
  node_subnet_cidr         = cidrsubnet(local.vcn_cidr, 8, 10)   # 10.0.10.0/24
  pod_subnet_cidr          = cidrsubnet(local.vcn_cidr, 4, 1)    # 10.0.16.0/20
  lb_subnet_cidr           = cidrsubnet(local.vcn_cidr, 8, 32)   # 10.0.32.0/24
  bastion_subnet_cidr      = cidrsubnet(local.vcn_cidr, 12, 528) # 10.0.33.0/28

  # Reaching the private Kubernetes API endpoint means coming through the VCN,
  # so the operator range is the VCN itself. The bastion is the way in, and what
  # can reach the bastion is oke_bastion_allowed_cidr.
  operator_cidr        = local.vcn_cidr
  bastion_allowed_cidr = var.oke_bastion_allowed_cidr
}
