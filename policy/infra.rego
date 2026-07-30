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
# A field counts as provided only when it resolves to a non-empty string. Uses
# object.get so an ABSENT key is handled the same as null/"" — the API strips
# null fields before sending, and `not present(input.missing)` does NOT negate
# to true for an undefined reference, so a missing field would slip through.
provided(field) if {
	value := object.get(input, field, "")
	is_string(value)
	value != ""
}

violations contains msg if {
	input.request_type in provisioning_types
	not provided("cost_centre_code")
	msg := "A cost centre tag is required (mandatory tagging policy)."
}

violations contains msg if {
	input.request_type in provisioning_types
	not provided("data_classification")
	msg := "A data classification tag is required (mandatory tagging policy)."
}

# ============================================================================
# F-GOV-03 — policy-as-code depth
# Deeper governance guardrails (hard blocks) plus advisory `warnings` that guide
# without blocking. The blocks are scoped to high-risk requests (sensitive data,
# production, xlarge, oversized non-prod), so a routine internal dev/uat request
# is unaffected.
# ============================================================================

cloud_targets := {"azure", "oci"}

sensitive_data := {"confidential", "restricted"}

lower_tiers := {"dev", "test", "sit", "uat"} # not preprod/prod/dr

critical_tiers := {"tier1", "tier2"}

has_size(wanted) if {
	some component in input.components
	component.size == wanted
}

# --- Sensitive-data encryption at rest (F-SEC-02) ---------------------------
encrypted_at_rest if input.advanced_options.encryption in {"at-rest", "at-rest-and-in-transit"}

violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification in sensitive_data
	input.deployment_target in cloud_targets
	not encrypted_at_rest
	msg := "Confidential or restricted data deployed to cloud must be encrypted at rest (set encryption to 'at-rest' or 'at-rest-and-in-transit')."
}

# --- Restricted data must not be publicly exposed (F-SEC-02) -----------------
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.advanced_options.public_endpoint == true
	msg := "Restricted data may not be exposed on a public endpoint."
}

violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.advanced_options.network_type == "public"
	msg := "Restricted data may not run on a public network (use a private or isolated network)."
}

# --- XLarge guardrail (F-FIN-08) --------------------------------------------
critical_ok if input.business_criticality in critical_tiers

violations contains msg if {
	has_size("xlarge")
	input.environment_tier in lower_tiers
	msg := "XLarge components are only permitted in preprod, prod or dr environments."
}

violations contains msg if {
	has_size("xlarge")
	not critical_ok
	msg := "XLarge components require a business criticality of tier1 or tier2."
}

# --- Right-sizing floor for low environments (F-FIN-08) ----------------------
violations contains msg if {
	input.environment_tier in {"dev", "test"}
	some component in input.components
	component.size in {"large", "xlarge"}
	msg := sprintf("'%v' size is not permitted in a dev or test environment (right-sizing policy).", [component.size])
}

# --- Advisory warnings (never block a submission; guidance only) -------------
warnings contains msg if {
	input.request_type in provisioning_types
	not provided("application_owner")
	msg := "No application owner named — add one so the environment has a clear owner."
}

warnings contains msg if {
	input.request_type in provisioning_types
	not provided("technical_owner")
	msg := "No technical owner named — add one for operational escalation."
}

warnings contains msg if {
	input.environment_tier == "prod"
	not provided("business_owner")
	msg := "Production environment without a business owner — name an accountable business owner."
}

prod_resilient if input.advanced_options.high_availability == true

prod_resilient if input.advanced_options.disaster_recovery in {"backup-restore", "warm-standby", "active-active"}

warnings contains msg if {
	input.environment_tier == "prod"
	not prod_resilient
	msg := "Production without high availability or a disaster-recovery posture — consider enabling HA or a DR strategy."
}

enhanced_monitoring if input.advanced_options.monitoring_level == "enhanced"

warnings contains msg if {
	input.environment_tier == "prod"
	not enhanced_monitoring
	msg := "Production without enhanced monitoring — consider monitoring_level='enhanced'."
}

no_backup if input.advanced_options.backup_retention == "none"

no_backup if not input.advanced_options.backup_retention

warnings contains msg if {
	input.request_type in provisioning_types
	input.data_classification in sensitive_data
	no_backup
	msg := "Sensitive data without a backup retention period — set backup_retention (7, 30 or 90 days)."
}
