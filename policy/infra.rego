# Policy-as-code gate for infrastructure requests (F-GOV-03).
# Evaluated by OPA at request time. Returns `allow` plus human-readable
# `violations`. This is the policy authority, independent of the app code
# (ARCHITECTURE.md §3/§9). The API re-runs this before it will submit a request.
package infra.authz

import rego.v1

# Deny by default; allow only when nothing is violated.
default allow := false

allow if count(violations) == 0

# Request types that provision or change resources (carry a target + components).
provisioning_types := {"create", "add", "resize"}

# --- Data residency (F-CAT-06) ----------------------------------------------
# Restricted data may not leave on-premises (sovereign requirement).
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.deployment_target != "onprem"
	msg := "Restricted data must be deployed on-premises (data residency policy)."
}

# --- Allowed deployment targets ---------------------------------------------
allowed_target if input.deployment_target in {"onprem", "azure", "oci"}

violations contains msg if {
	input.request_type in provisioning_types
	not allowed_target
	msg := sprintf("Deployment target '%v' is not an allowed region/target.", [input.deployment_target])
}

# --- Naming standard (F-CAT-04) ---------------------------------------------
valid_name if {
	is_string(input.environment_name)
	regex.match(`^[a-z0-9-]{3,40}$`, input.environment_name)
}

violations contains msg if {
	input.request_type == "create"
	not valid_name
	msg := "Environment name must follow the naming standard: lowercase letters, digits and hyphens, 3-40 characters."
}

# --- Mandatory tagging (F-CAT-05) -------------------------------------------
# A tag counts as present only when it is a non-empty string (guards null/"").
present(value) if {
	is_string(value)
	value != ""
}

violations contains msg if {
	input.request_type in provisioning_types
	not present(input.cost_centre_code)
	msg := "A cost centre tag is required (mandatory tagging policy)."
}

violations contains msg if {
	input.request_type in provisioning_types
	not present(input.data_classification)
	msg := "A data classification tag is required (mandatory tagging policy)."
}
