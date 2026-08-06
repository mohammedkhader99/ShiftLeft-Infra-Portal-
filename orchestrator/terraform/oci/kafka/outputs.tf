output "instance_ocids" {
  description = "OCIDs of the broker instances."
  value       = oci_core_instance.kafka[*].id
}

output "instance_display_names" {
  description = "Display names of the brokers, as they appear in the OCI console."
  value       = oci_core_instance.kafka[*].display_name
}

output "private_ips" {
  description = "Private IPs of the brokers. No public IP is assigned."
  value       = oci_core_instance.kafka[*].private_ip
}

output "hostname_labels" {
  description = "DNS hostname labels, read from the VNIC rather than recomputed."
  value       = [for i in oci_core_instance.kafka : i.create_vnic_details[0].hostname_label]
}

output "bootstrap_servers" {
  description = "The connection string an application needs. Names, not IPs: a rebuilt broker keeps its name but not its address."
  value       = local.bootstrap_servers
}

output "controller_quorum_voters" {
  description = "The KRaft quorum every node was configured with. Useful when diagnosing a cluster that will not elect a controller."
  value       = local.quorum_voters
}

output "cluster_id" {
  description = "The KRaft cluster id the storage was formatted with. Adding a node later must reuse this exact value."
  value       = local.cluster_id
}

output "kafka_source_url" {
  description = "Where the archive was fetched from. Recorded because a cluster that fails to install almost always failed here."
  value       = local.source_url
}
