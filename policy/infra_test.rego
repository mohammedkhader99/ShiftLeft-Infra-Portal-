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
