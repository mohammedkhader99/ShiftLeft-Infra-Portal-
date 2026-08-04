output "instance_ocids" {
  description = "OCIDs of the created instances."
  value       = oci_core_instance.service[*].id
}

output "private_ips" {
  description = "Private IPs. These are how the service is reached: no public IP is assigned by default."
  value       = oci_core_instance.service[*].private_ip
}

output "service_ports" {
  description = "Ports the service was configured to listen on."
  value       = var.service_ports
}

output "configured" {
  description = "Whether first-boot configuration was supplied. False means a bare VM was created and no service was installed."
  value       = var.user_data != ""
}
