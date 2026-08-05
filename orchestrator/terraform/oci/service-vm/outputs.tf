output "instance_ocids" {
  description = "OCIDs of the created instances."
  value       = oci_core_instance.service[*].id
}

output "private_ips" {
  description = "Private IPs. These are how the service is reached: no public IP is assigned by default."
  value       = oci_core_instance.service[*].private_ip
}

output "instance_display_names" {
  description = "Display names of the created instances, as they appear in the OCI console."
  value       = oci_core_instance.service[*].display_name
}

output "hostname_labels" {
  description = "DNS hostname labels — what the instances answer to inside the VCN. This module strips hyphens, so it does not resemble the display name. Read from the VNIC rather than recomputed."
  value       = [for i in oci_core_instance.service : i.create_vnic_details[0].hostname_label]
}

output "service_ports" {
  description = "Ports the service was configured to listen on."
  value       = var.service_ports
}

output "configured" {
  description = "Whether first-boot configuration was supplied. False means a bare VM was created and no service was installed."
  value       = var.user_data != ""
}
