output "cluster_id" {
  description = "OCID of the OKE cluster"
  value       = oci_containerengine_cluster.oke_cluster.id
}

output "vcn_id" {
  description = "OCID of the VCN"
  value       = oci_core_vcn.oke_vcn.id
}

output "api_endpoint_subnet_id" {
  value = oci_core_subnet.api_endpoint_subnet.id
}

output "node_subnet_id" {
  value = oci_core_subnet.node_subnet.id
}

output "pod_subnet_id" {
  value = oci_core_subnet.pod_subnet.id
}

output "lb_subnet_id" {
  value = oci_core_subnet.lb_subnet.id
}

output "node_pool_id" {
  value = oci_containerengine_node_pool.oke_node_pool.id
}

output "bastion_public_ip" {
  description = "Public IP of the bastion host"
  value       = oci_core_instance.bastion.public_ip
}

output "bastion_ssh_command" {
  description = "SSH to the bastion host"
  value       = "ssh opc@${oci_core_instance.bastion.public_ip}"
}

output "kubeconfig_command" {
  description = "Run this from the bastion host (oci-cli must be installed and authenticated there) to fetch kubeconfig for the private API endpoint"
  value       = "oci ce cluster create-kubeconfig --cluster-id ${oci_containerengine_cluster.oke_cluster.id} --file $HOME/.kube/config --region ${var.region} --token-version 2.0.0"
}
