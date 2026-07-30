# Rego unit tests for the request policy. Run with: opa test /policies
package infra.authz

import rego.v1

compliant := {
	"request_type": "create",
	"deployment_target": "onprem",
	"environment_name": "egate-uat",
	"cost_centre_code": "IMD-1001",
	"data_classification": "internal",
}

test_compliant_request_is_allowed if {
	allow with input as compliant
}

test_restricted_data_to_cloud_is_blocked if {
	not allow with input as object.union(compliant, {"deployment_target": "azure", "data_classification": "restricted"})
}

test_disallowed_target_is_blocked if {
	not allow with input as object.union(compliant, {"deployment_target": "gcp"})
}

test_bad_environment_name_is_blocked if {
	not allow with input as object.union(compliant, {"environment_name": "BAD NAME"})
}

test_missing_cost_centre_is_blocked if {
	not allow with input as object.union(compliant, {"cost_centre_code": null})
}

# Absent (not just null) keys must behave the same — the API strips null fields
# before calling OPA, so a missing tag/owner arrives as an absent key.
test_absent_cost_centre_is_blocked if {
	not allow with input as {
		"request_type": "create",
		"deployment_target": "onprem",
		"environment_name": "egate-uat",
		"data_classification": "internal",
	}
}

test_absent_owners_warn if {
	count(warnings) > 0 with input as {
		"request_type": "create",
		"deployment_target": "onprem",
		"environment_name": "egate-uat",
		"cost_centre_code": "IMD-1001",
		"data_classification": "internal",
	}
}

# --- F-GOV-03 depth: hard blocks ---------------------------------------------

prov := {
	"request_type": "create",
	"deployment_target": "onprem",
	"environment_name": "egate-uat",
	"cost_centre_code": "IMD-1001",
	"data_classification": "internal",
	"environment_tier": "uat",
	"application_owner": "a@x.com",
	"technical_owner": "t@x.com",
	"components": [{"technology_code": "postgres16", "size": "medium"}],
}

test_confidential_on_cloud_without_encryption_blocked if {
	not allow with input as object.union(prov, {"deployment_target": "azure", "data_classification": "confidential"})
}

test_confidential_on_cloud_with_encryption_allowed if {
	allow with input as object.union(prov, {
		"deployment_target": "azure",
		"data_classification": "confidential",
		"advanced_options": {"encryption": "at-rest"},
	})
}

test_restricted_public_endpoint_blocked if {
	not allow with input as object.union(prov, {
		"data_classification": "restricted",
		"advanced_options": {"public_endpoint": true},
	})
}

test_restricted_public_network_blocked if {
	not allow with input as object.union(prov, {
		"data_classification": "restricted",
		"advanced_options": {"network_type": "public"},
	})
}

test_xlarge_in_uat_blocked if {
	not allow with input as object.union(prov, {"components": [{"size": "xlarge"}]})
}

test_xlarge_in_prod_tier1_allowed if {
	allow with input as object.union(prov, {
		"environment_tier": "prod",
		"business_criticality": "tier1",
		"business_owner": "b@x.com",
		"components": [{"size": "xlarge"}],
	})
}

test_xlarge_without_critical_tier_blocked if {
	not allow with input as object.union(prov, {
		"environment_tier": "preprod",
		"business_criticality": "tier3",
		"components": [{"size": "xlarge"}],
	})
}

test_large_in_dev_blocked if {
	not allow with input as object.union(prov, {"environment_tier": "dev", "components": [{"size": "large"}]})
}

test_medium_in_dev_allowed if {
	allow with input as object.union(prov, {"environment_tier": "dev", "components": [{"size": "medium"}]})
}

# --- F-GOV-03 depth: advisory warnings (guide, never block) ------------------

test_missing_owners_warn_but_allow if {
	inp := object.union(prov, {"application_owner": null, "technical_owner": null})
	count(warnings) > 0 with input as inp
	allow with input as inp
}

test_prod_without_ha_or_dr_warns if {
	count(warnings) > 0 with input as object.union(prov, {"environment_tier": "prod", "business_owner": "b@x.com"})
}

test_fully_governed_prod_has_no_warnings_or_blocks if {
	inp := object.union(prov, {
		"environment_tier": "prod",
		"business_criticality": "tier1",
		"business_owner": "b@x.com",
		"advanced_options": {
			"high_availability": true,
			"monitoring_level": "enhanced",
			"backup_retention": "30",
		},
	})
	allow with input as inp
	count(warnings) == 0 with input as inp
}
