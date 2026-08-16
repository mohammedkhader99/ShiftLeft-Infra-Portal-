## ---------------------------------------------------------------------------
## Network Security Groups implementing OCI's documented required rules for
## OKE clusters with: private API endpoint, public worker nodes, VCN-native
## pod networking, public load balancer subnet.
## Reference: https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengnetworkconfig.htm
## ---------------------------------------------------------------------------

resource "oci_core_network_security_group" "nsg_api_endpoint" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = var.vcn_id
  display_name   = "${local.label_prefix}-nsg-api-endpoint"
}

resource "oci_core_network_security_group" "nsg_nodes" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = var.vcn_id
  display_name   = "${local.label_prefix}-nsg-nodes"
}

resource "oci_core_network_security_group" "nsg_pods" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = var.vcn_id
  display_name   = "${local.label_prefix}-nsg-pods"
}

resource "oci_core_network_security_group" "nsg_lb" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = var.vcn_id
  display_name   = "${local.label_prefix}-nsg-lb"
}

resource "oci_core_network_security_group" "nsg_bastion" {
  compartment_id = var.compartment_ocid
  freeform_tags  = var.tags
  vcn_id         = var.vcn_id
  display_name   = "${local.label_prefix}-nsg-bastion"
}

## --- API endpoint NSG rules --------------------------------------------------

resource "oci_core_network_security_group_security_rule" "api_ingress_kube_api_from_operator" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = local.operator_cidr
  source_type               = "CIDR_BLOCK"
  description                = "Allow operator/bastion access to Kubernetes API"
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_kube_api_from_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_nodes.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow worker nodes to reach Kubernetes API"
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_ocwp_from_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_nodes.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow worker nodes to send control-plane management traffic (OKE)"
  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_to_nodes_kubelet" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = oci_core_network_security_group.nsg_nodes.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow API server to reach kubelet on worker nodes"
  tcp_options {
    destination_port_range {
      min = 10250
      max = 10250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_ocwp_to_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = oci_core_network_security_group.nsg_nodes.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow control-plane management traffic to worker nodes (OKE)"
  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_kube_api_from_bastion" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_bastion.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow bastion host to reach Kubernetes API"
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_to_oci_services" {
  network_security_group_id = oci_core_network_security_group.nsg_api_endpoint.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = data.oci_core_services.all_oci_services.services[0].cidr_block
  destination_type           = "SERVICE_CIDR_BLOCK"
  description                = "Allow API endpoint to reach OCI services via Service Gateway"
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

## --- Worker node NSG rules -----------------------------------------------

resource "oci_core_network_security_group_security_rule" "nodes_ingress_self" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.nsg_nodes.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow worker nodes to communicate with each other"
}

resource "oci_core_network_security_group_security_rule" "nodes_ingress_kubelet_from_api" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_api_endpoint.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow API server to reach kubelet"
  tcp_options {
    destination_port_range {
      min = 10250
      max = 10250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "nodes_ingress_ssh_from_operator" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = local.operator_cidr
  source_type               = "CIDR_BLOCK"
  description                = "Allow SSH access to worker nodes from operator CIDR"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "nodes_ingress_ssh_from_bastion" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_bastion.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow SSH access to worker nodes from the bastion host"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "nodes_ingress_nodeport_from_lb" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.nsg_lb.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow load balancer to reach NodePort services"
  tcp_options {
    destination_port_range {
      min = 30000
      max = 32767
    }
  }
}

resource "oci_core_network_security_group_security_rule" "nodes_ingress_icmp_path_mtu" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "INGRESS"
  protocol                  = "1" # ICMP
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description                = "Allow path MTU discovery"
  icmp_options {
    type = 3
    code = 4
  }
}

resource "oci_core_network_security_group_security_rule" "nodes_egress_all" {
  network_security_group_id = oci_core_network_security_group.nsg_nodes.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination                = "0.0.0.0/0"
  destination_type           = "CIDR_BLOCK"
  description                = "Allow worker nodes outbound access (image pulls, OKE control plane, internet)"
}

## --- Pod NSG rules (VCN-native pod networking) -----------------------------

resource "oci_core_network_security_group_security_rule" "pods_ingress_self" {
  network_security_group_id = oci_core_network_security_group.nsg_pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.nsg_pods.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow pod-to-pod communication"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_from_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.nsg_nodes.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow worker nodes (kubelet probes, kube-proxy) to reach pods"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_from_api" {
  network_security_group_id = oci_core_network_security_group.nsg_pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.nsg_api_endpoint.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow control plane to reach pods (webhooks, metrics-server, exec/logs)"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_from_lb" {
  network_security_group_id = oci_core_network_security_group.nsg_pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.nsg_lb.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description                = "Allow load balancer to reach pod backends directly (VCN-native)"
}

resource "oci_core_network_security_group_security_rule" "pods_egress_all" {
  network_security_group_id = oci_core_network_security_group.nsg_pods.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination                = "0.0.0.0/0"
  destination_type           = "CIDR_BLOCK"
  description                = "Allow pod outbound access (via NAT Gateway)"
}

## --- Load balancer NSG rules -----------------------------------------------

resource "oci_core_network_security_group_security_rule" "lb_ingress_http" {
  network_security_group_id = oci_core_network_security_group.nsg_lb.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description                = "Allow public HTTP access"
  tcp_options {
    destination_port_range {
      min = 80
      max = 80
    }
  }
}

resource "oci_core_network_security_group_security_rule" "lb_ingress_https" {
  network_security_group_id = oci_core_network_security_group.nsg_lb.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description                = "Allow public HTTPS access"
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "lb_egress_to_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_lb.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = oci_core_network_security_group.nsg_nodes.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow load balancer to reach NodePort services"
  tcp_options {
    destination_port_range {
      min = 30000
      max = 32767
    }
  }
}

resource "oci_core_network_security_group_security_rule" "lb_egress_to_pods" {
  network_security_group_id = oci_core_network_security_group.nsg_lb.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination                = oci_core_network_security_group.nsg_pods.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow load balancer to reach pod backends directly (VCN-native)"
}

## --- Bastion NSG rules -------------------------------------------------------

resource "oci_core_network_security_group_security_rule" "bastion_ingress_ssh" {
  # The bastion carries a public IP and is the only route to a private
  # Kubernetes API endpoint, so the range allowed to reach it is the single most
  # security-relevant input here. Blocked outright when unset or opened to the
  # world: an unreachable bastion is a nuisance, a publicly reachable one is a
  # door into the cluster.
  lifecycle {
    precondition {
      condition     = local.bastion_allowed_cidr != "" && local.bastion_allowed_cidr != "0.0.0.0/0"
      error_message = "OCI_OKE_BASTION_CIDR must be set to the office or VPN range allowed to SSH to the bastion, and must not be 0.0.0.0/0."
    }
  }

  network_security_group_id = oci_core_network_security_group.nsg_bastion.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = local.bastion_allowed_cidr
  source_type               = "CIDR_BLOCK"
  description                = "Allow SSH to the bastion host from the allowed operator CIDR"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "bastion_egress_ssh_to_nodes" {
  network_security_group_id = oci_core_network_security_group.nsg_bastion.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = oci_core_network_security_group.nsg_nodes.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow bastion to SSH into worker nodes"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "bastion_egress_kube_api" {
  network_security_group_id = oci_core_network_security_group.nsg_bastion.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination                = oci_core_network_security_group.nsg_api_endpoint.id
  destination_type           = "NETWORK_SECURITY_GROUP"
  description                = "Allow bastion to reach the Kubernetes API endpoint (for kubectl / port-forward)"
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "bastion_egress_internet" {
  network_security_group_id = oci_core_network_security_group.nsg_bastion.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination                = "0.0.0.0/0"
  destination_type           = "CIDR_BLOCK"
  description                = "Allow bastion outbound access (OS updates, oci-cli, kubectl download)"
}
