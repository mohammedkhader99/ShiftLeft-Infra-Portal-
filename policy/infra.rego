# METADATA
# title: Infrastructure request policy
# description: The governance rules every provisioning request is judged against. Blocking rules stop a submission; advisory rules guide without blocking.
# scope: package
# related_resources:
#   - https://github.com/emaratech/infra-portal/blob/main/ARCHITECTURE.md
package infra.authz

# Policy-as-code gate for infrastructure requests (F-GOV-03).
# Evaluated by OPA at request time. Returns `allow` plus human-readable
# `violations`. This is the policy authority, independent of the app code
# (ARCHITECTURE.md §3/§9). The API re-runs this before it will submit a request.
#
# Each rule below carries an OPA METADATA block. Those annotations are what the
# admin console's Policies page renders, read from the LOADED policy via OPA's
# API — so a description can never drift from the rule it describes, and the page
# always shows what is actually enforcing, not what someone believes is.

import rego.v1

# Deny by default; allow only when nothing is violated.
default allow := false

allow if count(violations) == 0

# Request types that provision or change resources (carry a target + components).
provisioning_types := {"create", "add", "resize"}

# METADATA
# title: Data residency
# description: Restricted data may not leave on-premises. A sovereign-data requirement.
# custom:
#   feature: F-CAT-06
#   effect: block
#   applies_to: provisioning requests classified restricted
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.deployment_target != "onprem"
	msg := "Restricted data must be deployed on-premises (data residency policy)."
}

allowed_target if input.deployment_target in {"onprem", "azure", "oci"}

# METADATA
# title: Approved deployment targets
# description: An environment may only be placed on an approved target. Keeps workloads inside estates the organisation actually governs.
# custom:
#   feature: F-CAT-06
#   effect: block
#   applies_to: all provisioning requests
violations contains msg if {
	input.request_type in provisioning_types
	not allowed_target
	msg := sprintf("Deployment target '%v' is not an allowed region/target.", [input.deployment_target])
}

valid_name if {
	is_string(input.environment_name)
	regex.match(`^[a-z0-9-]{3,40}$`, input.environment_name)
}

# METADATA
# title: Environment naming standard
# description: Environment names must be lowercase letters, digits and hyphens, 3 to 40 characters. Keeps names predictable across cloud, DNS and billing.
# custom:
#   feature: F-CAT-04
#   effect: block
#   applies_to: create requests
violations contains msg if {
	input.request_type == "create"
	not valid_name
	msg := "Environment name must follow the naming standard: lowercase letters, digits and hyphens, 3-40 characters."
}

# A field counts as provided only when it resolves to a non-empty string. Uses
# object.get so an ABSENT key is handled the same as null/"" — the API strips
# null fields before sending, and `not present(input.missing)` does NOT negate
# to true for an undefined reference, so a missing field would slip through.
provided(field) if {
	value := object.get(input, field, "")
	is_string(value)
	value != ""
}

# METADATA
# title: Mandatory tag - cost centre
# description: Every provisioned environment must name a cost centre, so spend can always be charged back to an owner.
# custom:
#   feature: F-CAT-05
#   effect: block
#   applies_to: all provisioning requests
violations contains msg if {
	input.request_type in provisioning_types
	not provided("cost_centre_code")
	msg := "A cost centre tag is required (mandatory tagging policy)."
}

# METADATA
# title: Mandatory tag - data classification
# description: Every provisioned environment must declare how sensitive its data is. Several other rules depend on this answer.
# custom:
#   feature: F-CAT-05
#   effect: block
#   applies_to: all provisioning requests
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

encrypted_at_rest if input.advanced_options.encryption in {"at-rest", "at-rest-and-in-transit"}

# METADATA
# title: Encryption at rest for sensitive data on cloud
# description: Confidential or restricted data placed on a cloud target must be encrypted at rest.
# custom:
#   feature: F-SEC-02
#   effect: block
#   applies_to: confidential or restricted data on Azure or OCI
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification in sensitive_data
	input.deployment_target in cloud_targets
	not encrypted_at_rest
	msg := "Confidential or restricted data deployed to cloud must be encrypted at rest (set encryption to 'at-rest' or 'at-rest-and-in-transit')."
}

# METADATA
# title: No public endpoint for restricted data
# description: Restricted data may not be reachable from a public endpoint.
# custom:
#   feature: F-SEC-02
#   effect: block
#   applies_to: restricted data
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.advanced_options.public_endpoint == true
	msg := "Restricted data may not be exposed on a public endpoint."
}

# METADATA
# title: No public network for restricted data
# description: Restricted data must run on a private or isolated network, never a public one.
# custom:
#   feature: F-SEC-02
#   effect: block
#   applies_to: restricted data
violations contains msg if {
	input.request_type in provisioning_types
	input.data_classification == "restricted"
	input.advanced_options.network_type == "public"
	msg := "Restricted data may not run on a public network (use a private or isolated network)."
}

critical_ok if input.business_criticality in critical_tiers

# METADATA
# title: XLarge restricted to higher environments
# description: XLarge components are only permitted in preprod, prod or dr. Stops the most expensive sizing appearing in a sandbox.
# custom:
#   feature: F-FIN-08
#   effect: block
#   applies_to: requests containing an xlarge component
violations contains msg if {
	has_size("xlarge")
	input.environment_tier in lower_tiers
	msg := "XLarge components are only permitted in preprod, prod or dr environments."
}

# METADATA
# title: XLarge requires high business criticality
# description: XLarge components require a business criticality of tier1 or tier2, so the largest spend is tied to the most important systems.
# custom:
#   feature: F-FIN-08
#   effect: block
#   applies_to: requests containing an xlarge component
violations contains msg if {
	has_size("xlarge")
	not critical_ok
	msg := "XLarge components require a business criticality of tier1 or tier2."
}

# METADATA
# title: Right-sizing floor for dev and test
# description: Large and xlarge components are not permitted in dev or test environments.
# custom:
#   feature: F-FIN-08
#   effect: block
#   applies_to: dev and test environments
violations contains msg if {
	input.environment_tier in {"dev", "test"}
	some component in input.components
	component.size in {"large", "xlarge"}
	msg := sprintf("'%v' size is not permitted in a dev or test environment (right-sizing policy).", [component.size])
}

# METADATA
# title: Name an application owner
# description: Advisory. Environments without a named application owner become orphaned when people move on.
# custom:
#   feature: F-LCM-10
#   effect: advise
#   applies_to: all provisioning requests
warnings contains msg if {
	input.request_type in provisioning_types
	not provided("application_owner")
	msg := "No application owner named — add one so the environment has a clear owner."
}

# METADATA
# title: Name a technical owner
# description: Advisory. A technical owner is who gets contacted when the environment needs operational escalation.
# custom:
#   feature: F-LCM-10
#   effect: advise
#   applies_to: all provisioning requests
warnings contains msg if {
	input.request_type in provisioning_types
	not provided("technical_owner")
	msg := "No technical owner named — add one for operational escalation."
}

# METADATA
# title: Name a business owner for production
# description: Advisory. Production should have an accountable business owner, not only a technical one.
# custom:
#   feature: F-LCM-10
#   effect: advise
#   applies_to: production environments
warnings contains msg if {
	input.environment_tier == "prod"
	not provided("business_owner")
	msg := "Production environment without a business owner — name an accountable business owner."
}

prod_resilient if input.advanced_options.high_availability == true

prod_resilient if input.advanced_options.disaster_recovery in {"backup-restore", "warm-standby", "active-active"}

# METADATA
# title: Production resilience posture
# description: Advisory. Production without high availability or any disaster-recovery strategy is a single point of failure.
# custom:
#   feature: F-GOV-03
#   effect: advise
#   applies_to: production environments
warnings contains msg if {
	input.environment_tier == "prod"
	not prod_resilient
	msg := "Production without high availability or a disaster-recovery posture — consider enabling HA or a DR strategy."
}

enhanced_monitoring if input.advanced_options.monitoring_level == "enhanced"

# METADATA
# title: Enhanced monitoring for production
# description: Advisory. Production is worth monitoring at the enhanced level so problems surface before users report them.
# custom:
#   feature: F-INT-06
#   effect: advise
#   applies_to: production environments
warnings contains msg if {
	input.environment_tier == "prod"
	not enhanced_monitoring
	msg := "Production without enhanced monitoring — consider monitoring_level='enhanced'."
}

no_backup if input.advanced_options.backup_retention == "none"

no_backup if not input.advanced_options.backup_retention

# METADATA
# title: Backup retention for sensitive data
# description: Advisory. Confidential or restricted data with no backup retention cannot be recovered after loss.
# custom:
#   feature: F-LCM-06
#   effect: advise
#   applies_to: confidential or restricted data
warnings contains msg if {
	input.request_type in provisioning_types
	input.data_classification in sensitive_data
	no_backup
	msg := "Sensitive data without a backup retention period — set backup_retention (7, 30 or 90 days)."
}
