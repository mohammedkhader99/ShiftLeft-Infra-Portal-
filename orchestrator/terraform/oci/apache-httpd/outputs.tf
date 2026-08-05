output "instance_ids" {
  description = "OCIDs of the created Apache instances."
  value       = oci_core_instance.apache[*].id
}

output "instance_display_names" {
  description = "Display names of the created Apache instances."
  value       = oci_core_instance.apache[*].display_name
}

output "private_ips" {
  description = "Primary private IP addresses of the instances."
  value       = oci_core_instance.apache[*].private_ip
}

output "hostname_labels" {
  description = "DNS hostname labels — what the instances answer to inside the VCN. Read from the VNIC rather than recomputed, so it cannot drift from what was actually created."
  value       = [for i in oci_core_instance.apache : i.create_vnic_details[0].hostname_label]
}

output "public_ips" {
  description = "Public IP addresses of the instances (empty when assign_public_ip = false)."
  value       = oci_core_instance.apache[*].public_ip
}

output "nsg_id" {
  description = "OCID of the NSG created by this module (null when create_nsg = false)."
  value       = var.create_nsg ? oci_core_network_security_group.apache[0].id : null
}

output "http_urls" {
  description = "HTTP URLs for each instance (uses public IP when assigned, otherwise private IP)."
  value = [
    for inst in oci_core_instance.apache :
    format("http://%s/", coalesce(inst.public_ip, inst.private_ip))
  ]
}

output "https_urls" {
  description = "HTTPS URLs for each instance (empty when enable_https = false)."
  value = var.enable_https ? [
    for inst in oci_core_instance.apache :
    format("https://%s/", coalesce(inst.public_ip, inst.private_ip))
  ] : []
}
